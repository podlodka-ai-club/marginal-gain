#!/usr/bin/env python3
"""Модуль 2, Понимание. Разбирает разговор на Episode и Fact из схемы xmemory.

Наивно, без модели: границей эпизода считается сообщение человека, всё, что
агент сделал до следующего сообщения, попадает в этот эпизод. Итог пишется
текстом, из которого xmemory раскладывает узлы графа.
"""
import argparse, json, re
from datetime import datetime
from pathlib import Path

import xmem
from save import TRANSCRIPTS, blocks, result_text
from encoder import redact

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Пути, которые не являются рабочим кодом: наши же записи разговоров,
# состояние инструментов, временные файлы.
NOT_CODE = ("/.claude/", "/.local/state/", "/tmp/", "/.cache/")

# Отказы, которые устроил сам обвес, а не внешний мир. Знанием не являются,
# но повторяются чаще всего настоящего и потому лезут наверх любой меры.
# Темы предпочтений, подтверждённые архивом. Число в скобках это сколько
# просьб нашлось на 2026-08-24. Тем без подтверждения тут быть не должно.
PREF_TOPICS = [
    ("отвечать коротко, длинные ответы человек не читает",
     r"(кратк|короче|много текста|не читаю|сократ|покороче)"),
    ("проверять факты, не выдумывать, не утверждать непроверенное",
     r"(не выдумыв|проверь точно|убедись|перепровер)"),
    ("задавать вопросы по одному за раз",
     r"(по одному|поочеред|один вопрос|за раз)"),
    ("не использовать тире и дефисы в тексте",
     r"(тире|дефис)"),
    ("сначала показать план, править код после согласования",
     r"(сначала план|покажи план|согласу|не трогай пока|дождись)"),
]

HARNESS_NOISE = ("pretooluse", "posttooluse", "blocked:", "exit code",
                 "tool_use_error", "hook error", "command not found",
                 "no such file or directory", "did not complete within")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Признак отказа берём из самой записи (is_error), а не из текста:
# число 403 внутри прочитанного файла это не отказ.


def parse_time(stamp):
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def episodes_from_file(path):
    """Режем разговор на эпизоды по сообщениям человека."""
    current, out = None, []
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        for block in blocks(rec.get("message") or {}):
            kind = block.get("type")
            if kind == "text" and rec.get("type") == "user":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                if current:
                    out.append(current)
                current = {
                    "session_id": rec.get("sessionId") or rec.get("session_id") or "",
                    "number": len(out) + 1,
                    "request": text,
                    "started_at": rec.get("timestamp") or "",
                    "ended_at": rec.get("timestamp") or "",
                    "cwd": rec.get("cwd") or "",
                    "branch": rec.get("gitBranch") or "",
                    "files": [], "commands": [], "replies": [], "errors": [],
                }
            elif current is None:
                continue
            elif kind == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                current["ended_at"] = rec.get("timestamp") or current["ended_at"]
                if name in EDIT_TOOLS:
                    target = inp.get("file_path") or inp.get("notebook_path")
                    if target and target not in current["files"]:
                        current["files"].append(target)
                elif name == "Bash":
                    cmd = (inp.get("command") or "").strip().splitlines()
                    if cmd:
                        current["commands"].append(cmd[0][:200])
            elif kind == "tool_result":
                if block.get("is_error"):
                    current["errors"].append(result_text(block)[:200])
            elif kind == "text" and rec.get("type") == "assistant":
                current["replies"].append((block.get("text") or "").strip())
                current["ended_at"] = rec.get("timestamp") or current["ended_at"]
    if current:
        out.append(current)
    return out


def outcome_of(ep):
    if ep["errors"]:
        return "blocked"
    if ep["replies"] or ep["files"] or ep["commands"]:
        return "done"
    return "abandoned"


def render_episode(ep):
    """Текст, из которого xmemory собирает Episode и связанные Event."""
    started = parse_time(ep["started_at"])
    project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
    title = " ".join(ep["request"].split())[:80]
    lines = [
        "Episode %d of session %s." % (ep["number"], ep["session_id"]),
        "title: %s" % title,
        "project: %s" % project,
        "working_directory: %s" % (ep["cwd"] or "unknown"),
        "git_branch: %s" % (ep["branch"] or "none"),
        "started_at: %s" % (ep["started_at"] or "unknown"),
        "ended_at: %s" % (ep["ended_at"] or "unknown"),
        "outcome: %s" % outcome_of(ep),
    ]
    if started:
        lines.append("hour_of_day: %d" % started.hour)
        lines.append("day_of_week: %s" % DAYS[started.weekday()])
    summary = ["Человек попросил: %s" % " ".join(ep["request"].split())[:600]]
    if ep["files"]:
        summary.append("Правились файлы: %s." % ", ".join(ep["files"][:15]))
    if ep["commands"]:
        summary.append("Запускались команды: %s." % "; ".join(ep["commands"][:10]))
    if ep["errors"]:
        summary.append("Упирались в: %s" % ep["errors"][0])
    if ep["replies"]:
        summary.append("Итог: %s" % " ".join(ep["replies"][-1].split())[:600])
    lines.append("summary: %s" % " ".join(summary))
    return redact("\n".join(lines))


def facts_of(ep):
    """Узлы Fact. Типы берём из схемы: user, preference, project_state, external_resource."""
    project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
    out = []
    request = " ".join(ep["request"].split())
    low = request.lower()
    # Предпочтение это не любая просьба, а попадание в известную тему.
    # Иначе каждое сообщение человека становится отдельным "предпочтением".
    for topic, pat in PREF_TOPICS:
        if re.search(pat, low):
            # Пример просьбы в текст не вшиваем: чужие слова из примера
            # потом находятся поиском и выдаются как знание по чужой теме.
            out.append(("preference", topic, "global",
                        "Пользователь просит: %s" % topic))
    for target in ep["files"][:15]:
        if any(bad in target for bad in NOT_CODE):
            continue
        out.append(("project_state", project, "project",
                    "В проекте %s правился файл %s ради задачи: %s" % (project, target, request[:200])))
    for err in ep["errors"][:1]:
        if any(bad in err.lower() for bad in HARNESS_NOISE):
            continue
        out.append(("project_state", project, "project",
                    "В проекте %s упирались в препятствие: %s" % (project, err[:300])))
    for reply in ep["replies"]:
        for token in ("http://", "https://"):
            idx = reply.find(token)
            if idx >= 0:
                url = reply[idx:].split()[0].strip(".,);")
                out.append(("external_resource", project, "project",
                            "Для проекта %s использовался адрес %s" % (project, url)))
                break
    return [(t, s, sc, redact(c)) for t, s, sc, c in out]


def fact_key(fact_type, subject, scope, content):
    """Чем два факта считаются одним и тем же."""
    if fact_type == "external_resource":
        return ("url", content.split()[-1])
    if fact_type == "preference":
        return ("pref", subject)   # subject это тема, она и есть ключ
    if "правился файл" in content:
        return ("file", subject, content.split("правился файл ")[-1].split(" ради")[0])
    return ("other", subject, content[:60])


def weigh(files):
    """Мера факта: сколько раз подтверждён, когда в последний раз, в скольких проектах.

    Считается по всему архиву, без сети. Порог отсекает то, что встретилось
    один раз и давно: такое чаще шум, чем знание.
    """
    from collections import defaultdict
    seen = defaultdict(lambda: {"n": 0, "last": "", "projects": set()})
    for path in files:
        for ep in episodes_from_file(path):
            project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
            for fact in facts_of(ep):
                rec = seen[fact_key(*fact)]
                rec["n"] += 1
                rec["projects"].add(project)
                if ep["ended_at"] > rec["last"]:
                    rec["last"] = ep["ended_at"]
    return seen


def score_of(rec, newest):
    """Оценка от 0 до 1. Три доли: повторяемость, охват проектов, свежесть."""
    repeat = min(rec["n"], 10) / 10.0
    spread = min(len(rec["projects"]), 3) / 3.0
    fresh = 0.0
    if rec["last"] and newest:
        a, b = parse_time(rec["last"]), parse_time(newest)
        if a and b:
            days = (b - a).days
            fresh = max(0.0, 1.0 - days / 30.0)
    return round(0.5 * repeat + 0.2 * spread + 0.3 * fresh, 3)


def render_fact(fact_type, subject, scope, content, score=None, rec=None):
    lines = ["Fact.", "content: %s" % content, "fact_type: %s" % fact_type,
             "subject: %s" % subject, "scope: %s" % scope]
    if rec is not None:
        lines.append("Подтверждений в архиве: %d, проектов: %d, последний раз: %s."
                     % (rec["n"], len(rec["projects"]), (rec["last"] or "неизвестно")[:10]))
    if score is not None:
        lines.append("Оценка уверенности: %.2f." % score)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Разбор разговоров на Episode и Fact")
    ap.add_argument("--send", dest="dry", action="store_false", default=True)
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--limit", type=int, help="потолок записей за заход")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="порог: факты ниже него не пишем")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    if args.only:
        files = [f for f in files if args.only in str(f)]

    # мера считается по всему архиву, а не по выбранным файлам:
    # факт, встреченный в другом проекте, тоже подтверждение
    weights = weigh(sorted(TRANSCRIPTS.rglob("*.jsonl")))
    newest = max((r["last"] for r in weights.values() if r["last"]), default="")

    eps = fcts = skipped = 0
    for path in files:
        for ep in episodes_from_file(path):
            if args.limit and eps + fcts >= args.limit:
                break
            text = render_episode(ep)
            if not args.dry:
                xmem.write(text)
            eps += 1
            if args.verbose:
                title = text.split("title: ")[1].splitlines()[0]
                print("EPISODE %d %s | %s" % (ep["number"], outcome_of(ep), title[:70]))
            for fact in facts_of(ep):
                if args.limit and eps + fcts >= args.limit:
                    break
                rec = weights[fact_key(*fact)]
                score = score_of(rec, newest)
                if score < args.min_score:
                    skipped += 1
                    continue
                if not args.dry:
                    xmem.write(render_fact(*fact, score=score, rec=rec))
                fcts += 1
                if args.verbose:
                    print("   FACT %.2f [%s/%s] x%d %s"
                          % (score, fact[0], fact[2], rec["n"], fact[3][:80]))
    print("эпизодов %d, фактов %d, отсеяно порогом %d, режим %s"
          % (eps, fcts, skipped, "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
