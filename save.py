#!/usr/bin/env python3
"""Модуль 1, Сохранение. Наивно: всё содержимое разговора уходит в xmemory.

Никакого отбора, никакой обрезки. Единственное исключение это затирание
секретов: токены и ключи наружу не уходят никогда.
"""
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

import xmem
from encoder import redact

TRANSCRIPTS = Path.home() / ".claude" / "projects"
STATE = Path.home() / ".local" / "state" / "memory-encoder" / "save-state.json"


def blocks(message):
    body = message.get("content")
    if isinstance(body, str):
        return [{"type": "text", "text": body}]
    return [b for b in body if isinstance(b, dict)] if isinstance(body, list) else []


def result_text(block):
    body = block.get("content")
    if isinstance(body, list):
        return " ".join(p.get("text", "") for p in body if isinstance(p, dict))
    return str(body or "")


def records_from_line(rec):
    """Одна строка транскрипта превращается в записи. Целиком, без обрезки."""
    if rec.get("type") not in ("user", "assistant"):
        return []
    out = []
    for block in blocks(rec.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            out.append(("реплика", block.get("text", "")))
        elif kind == "thinking":
            out.append(("размышление", block.get("thinking", "")))
        elif kind == "tool_use":
            out.append(("команда %s" % block.get("name", "?"),
                        json.dumps(block.get("input", {}), ensure_ascii=False)))
        elif kind == "tool_result":
            out.append(("результат команды", result_text(block)))
    return [(role, redact(text)) for role, text in out if str(text).strip()]


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"files": {}}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def read_new(path, cursor):
    stat = path.stat()
    offset = cursor.get("offset", 0)
    if stat.st_size < offset or cursor.get("inode") not in (None, stat.st_ino):
        offset = 0
    seq = cursor.get("seq", 0)
    items = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith("\n"):
                break
            offset += len(line.encode("utf-8"))
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for role, text in records_from_line(rec):
                items.append({
                    "seq": seq,
                    "session": rec.get("sessionId") or rec.get("session_id") or "",
                    "at": rec.get("timestamp") or "",
                    "cwd": rec.get("cwd") or "",
                    "branch": rec.get("gitBranch") or "",
                    "role": role,
                    "text": text,
                })
                seq += 1
    return items, {"offset": offset, "inode": stat.st_ino, "seq": seq}


def render(item):
    where = Path(item["cwd"]).name if item["cwd"] else "неизвестно"
    head = "Разговор %s, проект %s, ветка %s, время %s. %s:" % (
        item["session"] or "?", where, item["branch"] or "нет",
        item["at"] or "неизвестно", item["role"])
    return "%s\n%s" % (head, item["text"])


def main():
    ap = argparse.ArgumentParser(description="Сохранение разговоров в xmemory")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="реально писать в xmemory (по умолчанию холостой прогон)")
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--reset", action="store_true", help="перечитать выбранные файлы с начала")
    ap.add_argument("--limit", type=int, help="остановиться после стольких записей")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    state = load_state()
    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    if args.only:
        files = [f for f in files if args.only in str(f)]
    if args.reset:
        for f in files:
            state["files"].pop(str(f), None)

    sent, stopped = 0, False
    for path in files:
        before = state["files"].get(str(path), {})
        items, cursor = read_new(path, before)
        done = 0
        for item in items:
            if args.limit and sent >= args.limit:
                stopped = True
                break
            text = render(item)
            if not args.dry:
                xmem.write(text)
            sent += 1
            done += 1
            if args.verbose:
                print("%s #%d %s %d симв." % (path.name, item["seq"], item["role"], len(text)))
        # Отметку о прочитанном двигаем только если файл дочитан до конца.
        # Иначе непосланные записи пропали бы молча.
        state["files"][str(path)] = cursor if done == len(items) else before
        save_state(state)
        if stopped:
            break

    print("файлов %d, записей %d, режим %s" % (len(files), sent, "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
