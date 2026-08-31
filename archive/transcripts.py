#!/usr/bin/env python3
"""Чтение архива: транскрипты Claude Code, разбор строк, нарезка эпизодов.

Нижний слой над scrub. Ни двери хранилища, ни схемы записей здесь нет и быть
не должно: читать архив и писать в хранилище — разные поводы меняться.

Раньше это жило в модуле сохранения, и понимание брало разбор транскрипта оттуда:
`from save import TRANSCRIPTS, blocks, result_text`. То есть понимание
зависело от записи и тянуло за собой и дверь, и схему.

Состояния здесь нет. Курсор по файлу приходит аргументом и уходит
результатом: чья это книжка учёта — дело вызывающего, у сохранения и у
понимания они разные.
"""
import json
from pathlib import Path

from infra.scrub import redact
# Разбор времени лежит в infra: он нужен и мере факта, которая ниже архива.
# Имена видны отсюда намеренно — вызывающие берут их у чтения архива.
from infra.timeline import DAYS, parse_time, when

# Имена времени видны отсюда намеренно: вызывающие берут разбор у чтения архива.
__all__ = ["TRANSCRIPTS", "EDIT_TOOLS", "DAYS", "parse_time", "when", "blocks",
           "record_of", "starts_episode", "episodes_and_events",
           "result_text", "records_from_line", "read_new", "episodes_from_file"]

TRANSCRIPTS = Path.home() / ".claude" / "projects"

# Признак отказа берём из самой записи (is_error), а не из текста:
# число 403 внутри прочитанного файла это не отказ.
#
# Инструменты, чья работа означает правку файла.
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


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


def record_of(kind_of, block):
    """Запись из одного блока строки, либо None, если записи в нём нет.

    Правило одно на всех: по нему сохранение нумерует события, а понимание
    считает, какие из них попали в эпизод. Была бы копия в каждом — копии
    разошлись бы молча, и связь эпизод — событие повисла бы на номере, которого
    в хранилище нет.
    """
    said = "user_message" if kind_of == "user" else "agent_response"
    kind = block.get("type")
    if kind == "text":
        out = (said, "реплика", block.get("text", ""), None)
    elif kind == "thinking":
        out = ("agent_response", "размышление", block.get("thinking", ""), None)
    elif kind == "tool_use":
        name = block.get("name", "?")
        out = ("tool_call", "команда %s" % name,
               json.dumps(block.get("input", {}), ensure_ascii=False), name)
    elif kind == "tool_result":
        out = ("tool_result", "результат команды", result_text(block), None)
    else:
        return None
    event_type, role, text, tool = out
    if not str(text).strip():
        return None
    return (event_type, role, redact(text), tool)


def records_from_line(rec):
    """Одна строка транскрипта превращается в записи. Целиком, без обрезки.

    Вид события берётся из схемы, а не выдумывается: по нему хранилище отличает
    сообщение человека от ответа агента и от вызова инструмента.
    """
    kind_of = rec.get("type")
    if kind_of not in ("user", "assistant"):
        return []
    found = [record_of(kind_of, block) for block in blocks(rec.get("message") or {})]
    return [item for item in found if item is not None]


def starts_episode(kind_of, block):
    """Граница эпизода: непустая реплика человека.

    Правило живёт здесь, а не в разборе: по нему режет эпизоды понимание, и по
    нему же считает границы всякий, кому нужно знать, где эпизод начался.
    """
    return (kind_of == "user" and block.get("type") == "text"
            and bool((block.get("text") or "").strip()))


def read_new(path, cursor):
    """Новое с прошлого захода. Курсор внутрь не прячем: он приходит и уходит.

    Читаем побайтно, а не текстом: при errors="replace" любой негодный байт
    становится U+FFFD, тот кодируется тремя байтами обратно, и отметка уезжает
    относительно настоящего места в файле. Следующий заход после такого
    садится в середину строки и теряет или задваивает записи молча.

    Каждая запись несёт отметку, годную сразу после её строки, и признак
    последней в строке. Одна строка даёт несколько записей, и остановиться
    посреди строки нельзя: отметка после неё пропустила бы соседей, а отметка
    до неё выдала бы уже отданное вторым разом.
    """
    stat = path.stat()
    offset = cursor.get("offset", 0)
    if stat.st_size < offset or cursor.get("inode") not in (None, stat.st_ino):
        offset = 0
    seq = cursor.get("seq", 0)
    items = []
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break          # строку ещё дописывают, целой она не считается
            offset += len(raw)
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            here = []
            for event_type, role, text, tool in records_from_line(rec):
                here.append({
                    "seq": seq,
                    "session": rec.get("sessionId") or rec.get("session_id") or "",
                    "at": rec.get("timestamp") or "",
                    "cwd": rec.get("cwd") or "",
                    "branch": rec.get("gitBranch") or "",
                    "event_type": event_type,
                    "tool": tool,
                    "role": role,
                    "text": text,
                    "last_in_line": False,
                    "cursor": None,
                })
                seq += 1
            if here:
                here[-1]["last_in_line"] = True
                here[-1]["cursor"] = {"offset": offset, "inode": stat.st_ino, "seq": seq}
            items.extend(here)
    return items, {"offset": offset, "inode": stat.st_ino, "seq": seq}


def _lines(path):
    """Строки файла по одной, с закрытием файла в конце."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line


def episodes_from_file(path):
    """Режем разговор на эпизоды по сообщениям человека."""
    return episodes_and_events(path)[0]


def episodes_and_events(path):
    """Эпизоды файла и сколько событий у каждого разговора в этом файле.

    Номера событий эпизод несёт в поле `events` — те же порядковые номера, что
    присваивает сохранение, только считанные от начала файла. Абсолютными их
    делает тот, кто знает, сколько событий разговора было в предыдущих файлах,
    см. understand.event_bases_of.

    Считается тем же проходом, что и эпизоды: второй проход по файлу стоил бы
    столько же, сколько первый, а конец хода обходит сотни транскриптов.
    """
    current, out = None, []
    seq, counts = {}, {}
    # Файл закрываем явно и читаем построчно: понимание зовётся в конце
    # каждого хода и обходит сотни транскриптов. Брошенный дескриптор живёт
    # до сборки мусора, а целый файл в памяти это мегабайты на каждый.
    for line in _lines(path):
        if not line.endswith("\n"):
            # Строку ещё дописывают. Сохранение её не берёт (см. read_new), и
            # понимание не должно: разойдясь на одной строке, счётчики событий
            # разъезжаются навсегда — расхождение оседает в отметке файла, и
            # все следующие файлы разговора сдвинуты.
            break
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        talk = rec.get("sessionId") or rec.get("session_id") or ""
        # Разговор без имени зовём «unknown» — тем же словом и по той же
        # причине, что сохранение: половина ключа пустой быть не может, и
        # счётчик обязан класть такие события в ту же корзину.
        who = talk or "unknown"
        kind_of = rec.get("type")
        for block in blocks(rec.get("message") or {}):
            kind = block.get("type")
            # Номер события считаем до всего прочего и для каждого блока, даже
            # если эпизода ещё нет: сохранение нумерует так же, а записи до
            # первой реплики человека — предисловие, эпизода у них нет.
            number = None
            if record_of(kind_of, block) is not None:
                number = seq.get(who, 0)
                seq[who] = number + 1
                counts[who] = seq[who]
            if starts_episode(kind_of, block):
                text = (block.get("text") or "").strip()
                if current:
                    out.append(current)
                current = {
                    "session_id": talk,
                    "number": len(out) + 1,
                    "request": text,
                    "started_at": rec.get("timestamp") or "",
                    "ended_at": rec.get("timestamp") or "",
                    "cwd": rec.get("cwd") or "",
                    "branch": rec.get("gitBranch") or "",
                    "files": [], "commands": [], "replies": [], "errors": [],
                    "events": [],
                }
                if number is not None:
                    current["events"].append(number)
                continue
            if current is None:
                continue
            if number is not None:
                current["events"].append(number)
            if kind == "tool_use":
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
    return out, counts
