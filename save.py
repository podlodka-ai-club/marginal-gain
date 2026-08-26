#!/usr/bin/env python3
"""Модуль 1, Сохранение. Всё содержимое разговора уходит в хранилище.

Никакого отбора, никакой обрезки. Единственное исключение это затирание
секретов: токены и ключи наружу не уходят никогда.

Запись структурная: разговор ложится строкой Session, каждая реплика, команда
и результат — строкой Event, между ними ставится связь. Раньше отсюда уходила
сплошная проза, и ключ выводил экстрактор: при промахе записи схлопывались, а
на хранилище без экстрактора не появлялось ни одного Event. Ключ задан здесь.

Текстовый путь остался запасным: консоль структурной записи не умеет.
"""
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

import models
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


DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def when(stamp):
    """Час и день недели из отметки времени. Нужны схеме для поиска по времени."""
    if not stamp:
        return None, None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    return moment.hour, DAYS[moment.weekday()]


def records_from_line(rec):
    """Одна строка транскрипта превращается в записи. Целиком, без обрезки.

    Вид события берётся из схемы, а не выдумывается: по нему хранилище отличает
    сообщение человека от ответа агента и от вызова инструмента.
    """
    kind_of = rec.get("type")
    if kind_of not in ("user", "assistant"):
        return []
    said = "user_message" if kind_of == "user" else "agent_response"
    out = []
    for block in blocks(rec.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            out.append((said, "реплика", block.get("text", ""), None))
        elif kind == "thinking":
            out.append(("agent_response", "размышление", block.get("thinking", ""), None))
        elif kind == "tool_use":
            name = block.get("name", "?")
            out.append(("tool_call", "команда %s" % name,
                        json.dumps(block.get("input", {}), ensure_ascii=False), name))
        elif kind == "tool_result":
            out.append(("tool_result", "результат команды", result_text(block), None))
    return [(event_type, role, redact(text), tool)
            for event_type, role, text, tool in out if str(text).strip()]


def load_state():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state.setdefault("files", {})
    state.setdefault("sessions", {})
    return state


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
            for event_type, role, text, tool in records_from_line(rec):
                items.append({
                    "seq": seq,
                    "session": rec.get("sessionId") or rec.get("session_id") or "",
                    "at": rec.get("timestamp") or "",
                    "cwd": rec.get("cwd") or "",
                    "branch": rec.get("gitBranch") or "",
                    "event_type": event_type,
                    "tool": tool,
                    "role": role,
                    "text": text,
                })
                seq += 1
    return items, {"offset": offset, "inode": stat.st_ino, "seq": seq}


def event_of(item):
    """Запись Event по схеме. Ключ — разговор плюс порядковый номер."""
    hour, day = when(item["at"])
    return models.Event(
        session_id=item["session"] or "unknown",
        sequence_number=item["seq"],
        event_type=item["event_type"],
        content=item["text"],
        tool_name=item["tool"],
        occurred_at=item["at"] or None,
        project=Path(item["cwd"]).name if item["cwd"] else None,
        working_directory=item["cwd"] or None,
        git_branch=item["branch"] or None,
        hour_of_day=hour,
        day_of_week=day)


def session_of(item):
    """Запись Session. Разговор один на много событий, ключ у него один."""
    return models.Session(
        session_id=item["session"] or "unknown",
        project=Path(item["cwd"]).name if item["cwd"] else None,
        working_directory=item["cwd"] or None,
        git_branch=item["branch"] or None,
        started_at=item["at"] or None)


def send(items):
    """Структурная запись пачкой: строки и связи между ними одним вызовом.

    Разговор пишется раньше своих событий: связь ссылается на обе стороны по
    ключу, и порядок в списке мутаций сохраняется.
    """
    records, relations, seen = [], [], {}
    for item in items:
        event = event_of(item)
        session = seen.get(event.session_id)
        if session is None:
            session = seen[event.session_id] = session_of(item)
            records.append(session)
        records.append(event)
        relations.append(models.link("session_events", session=session, event=event))
    if not records:
        return 0
    xmem.write_objects(records, relations)
    return len(records)


def deliver(items):
    """Структурная запись, а при её отсутствии — текст.

    Консоль структурной записи не умеет и падает явной ошибкой. Это не повод
    терять разговор: запасной путь кладёт то же самое прозой, как было раньше.
    """
    try:
        return send(items)
    except xmem.BackendError:
        if xmem.BACKEND != "cli":
            raise      # структурная запись есть у всех, кроме консоли
        for item in items:
            xmem.write(render(item))
        return len(items)


def render(item):
    where = Path(item["cwd"]).name if item["cwd"] else "неизвестно"
    head = "Разговор %s, проект %s, ветка %s, время %s. %s:" % (
        item["session"] or "?", where, item["branch"] or "нет",
        item["at"] or "неизвестно", item["role"])
    return "%s\n%s" % (head, item["text"])


def ingest(files, limit=None, dry=True, reset=False, verbose=False):
    """Проход по файлам архива. Отдельно от разбора доводов, чтобы звать извне.

    Потребителю очереди нужен ровно этот проход, а не командная строка вокруг
    него. Отметка о прочитанном общая, поэтому повторный заход по тому же
    файлу ничего не задваивает.
    """
    state = load_state()
    if reset:
        for path in files:
            state["files"].pop(str(path), None)
        state["sessions"] = {}

    # Номер события уникален в пределах разговора, а не файла. Разговор часто
    # разложен по нескольким файлам архива: 59 из 153 на 2026-08-26. Пока номер
    # считался по файлу, второй файл начинал с нуля и затирал события первого
    # по ключу (session_id, sequence_number).
    sessions = dict(state.get("sessions") or {})
    sent, stopped, batch = 0, False, []
    for path in files:
        before = state["files"].get(str(path), {})
        items, cursor = read_new(path, before)
        done = 0
        for item in items:
            if limit and sent >= limit:
                stopped = True
                break
            talk = item["session"] or "unknown"
            item["seq"] = sessions.get(talk, 0)
            sessions[talk] = item["seq"] + 1
            batch.append(item)
            sent += 1
            done += 1
            if verbose:
                print("%s #%d %s %d симв."
                      % (path.name, item["seq"], item["role"], len(item["text"])))
        # Записи уходят пачкой на файл, и только потом двигается отметка:
        # если запись не удалась, исключение долетит сюда и отметка останется
        # прежней. Иначе разговор считался бы сохранённым, не будучи им.
        if not dry and batch:
            deliver(batch)
            # Счётчики двигаются только после успешной записи: если запись
            # упала, номера не считаются израсходованными.
            state["sessions"] = dict(sessions)
        batch = []
        # Отметку о прочитанном двигаем только если файл дочитан до конца.
        # Иначе непосланные записи пропали бы молча.
        state["files"][str(path)] = cursor if done == len(items) else before
        save_state(state)
        if stopped:
            break
    return sent


def main():
    ap = argparse.ArgumentParser(description="Сохранение разговоров в xmemory")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="реально писать в xmemory (по умолчанию холостой прогон)")
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--reset", action="store_true", help="перечитать выбранные файлы с начала")
    ap.add_argument("--limit", type=int, help="остановиться после стольких записей")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    if args.only:
        files = [f for f in files if args.only in str(f)]
    sent = ingest(files, limit=args.limit, dry=args.dry,
                  reset=args.reset, verbose=args.verbose)

    print("файлов %d, записей %d, режим %s" % (len(files), sent, "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
