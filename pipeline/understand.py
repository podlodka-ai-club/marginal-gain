#!/usr/bin/env python3
"""Модуль 2, Понимание. Разбирает разговор на Episode и Fact из схемы xmemory.

Наивно, без модели: границей эпизода считается сообщение человека, всё, что
агент сделал до следующего сообщения, попадает в этот эпизод. Итог пишется
текстом, из которого xmemory раскладывает узлы графа.
"""
import argparse
from pathlib import Path

from domain import features
from domain.measure import score_of
from storage import port
from archive.transcripts import DAYS, TRANSCRIPTS, episodes_from_file, parse_time
from archive.extract import NOT_CODE, PREF_TOPICS, facts_of, fact_key
from infra.scrub import redact

# Извлечение фактов уехало в archive.extract, чтение — в archive.transcripts.
# остаются видимы отсюда намеренно: стенд research/lab зовёт их через
# `import understand as u`, и ломать его переносом незачем.
__all__ = ["NOT_CODE", "PREF_TOPICS", "facts_of", "fact_key", "episodes_from_file",
           "outcome_of", "render_episode", "weigh", "features_of", "score_of",
           "render_fact", "parse_time"]


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


def features_of(rec):
    """Признаки узла факта. Считаются рядом с мерой и в неё не входят."""
    return features.compute(rec)


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

    door = port.door()
    eps = fcts = skipped = 0
    for path in files:
        for ep in episodes_from_file(path):
            if args.limit and eps + fcts >= args.limit:
                break
            text = render_episode(ep)
            if not args.dry:
                door.write(text)
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
                    door.write(render_fact(*fact, score=score, rec=rec))
                fcts += 1
                if args.verbose:
                    feats = features_of(rec)
                    print("   FACT %.2f [%s/%s] x%d %s %s"
                          % (score, fact[0], fact[2], rec["n"], fact[3][:80],
                             " ".join("%s=%.3f" % (k, v) for k, v in feats.items())))
    print("эпизодов %d, фактов %d, отсеяно порогом %d, режим %s"
          % (eps, fcts, skipped, "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
