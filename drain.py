#!/usr/bin/env python3
"""Потребитель очереди. Забирает то, что сложил хук, и доводит до хранилища.

Хук на сообщение человека стоит в горячем пути: он не имеет права ни ходить в
сеть, ни думать. Поэтому он только кладёт запись в очередь. До сих пор на этом
всё и заканчивалось — очередь писалась, и её не читал никто. Запись, которую
никто не забирает, это не запись, а потерянный вход.

Разговор разбирает не этот модуль: очередь называет транскрипты, которых
коснулись, а разбор идёт обычным проходом сохранения. Так у события остаётся
единственный источник номера, и повторный заход ничего не задваивает.
"""
import argparse, contextlib, fcntl, json, os
from pathlib import Path

import save

STATE_DIR = Path.home() / ".local" / "state" / "memory-encoder"

# Тот же замок, под которым работает хук конца хода. Иначе два прохода делят
# один файл отметок: каждый читает его целиком и перезаписывает целиком, и
# проигравший откатывает продвижение победителя.
LOCK = STATE_DIR / "save.lock"

QUEUE = Path(os.environ.get("XMEM_QUEUE_PATH")
             or Path.home() / ".local" / "state" / "memory-encoder" / "queue.jsonl")


def read_queue(path=None):
    """Строки очереди. Битую строку пропускаем: она не повод бросить остальные."""
    target = Path(path) if path else QUEUE
    if not target.exists():
        return []
    out = []
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def transcripts(items):
    """Транскрипты из очереди, по одному разу и в порядке появления."""
    seen, out = set(), []
    for item in items:
        target = item.get("transcript_path")
        if not target or target in seen:
            continue
        seen.add(target)
        if Path(target).exists():
            out.append(Path(target))
    return out


def taken_path(path=None):
    target = Path(path) if path else QUEUE
    return target.with_name(target.name + ".taken")


def take(path=None):
    """Забрать очередь, подменив файл, а не опустошив его.

    Опустошение стирало всё, что хук дописал за время записи: между чтением и
    очисткой проходят секунды, а хук пишет на каждое сообщение человека.
    Подмена атомарна, и хук после неё пишет уже в новый файл.

    Недоеденное с прошлого раза лежит в том же файле подмены и разбирается
    первым: иначе падение записи теряло бы очередь целиком.
    """
    target = Path(path) if path else QUEUE
    taken = taken_path(path)
    if not taken.exists() and target.exists():
        target.rename(taken)
    return taken


@contextlib.contextmanager
def alone():
    """Один проход по архиву за раз. Замок общий с хуком конца хода."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def drain(path=None, limit=None, dry=True, extra=()):
    """Забрать очередь, сохранить названные разговоры, снять подмену.

    Подмена снимается последней и только если разобрано всё. Если запись
    упала или упёрлась в потолок, файл подмены остаётся и его заберёт
    следующий заход: потерять вход хуже, чем записать дважды.
    """
    if dry:
        items = read_queue(path)
        files = transcripts(items) + [Path(p) for p in extra if Path(p).exists()]
        return {"queued": len(items), "transcripts": len(files), "written": 0}
    with alone():
        taken = take(path)
        items = read_queue(taken)
        files = transcripts(items)
        for name in extra:
            target = Path(name)
            if target.exists() and target not in files:
                files.append(target)
        sent = save.ingest(files, limit=limit, dry=False) if files else 0
        # Потолок означает, что разобрано не всё. Файл подмены не трогаем.
        if not (limit and sent >= limit) and taken.exists():
            taken.unlink()
    return {"queued": len(items), "transcripts": len(files), "written": sent}


def main():
    ap = argparse.ArgumentParser(description="Потребитель очереди хука")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="реально писать (по умолчанию холостой прогон)")
    ap.add_argument("--limit", type=int, help="остановиться после стольких записей")
    ap.add_argument("--queue", help="файл очереди, если не путь по умолчанию")
    ap.add_argument("--transcript", action="append", default=[],
                    help="разобрать ещё и этот транскрипт, помимо очереди")
    args = ap.parse_args()
    got = drain(args.queue, limit=args.limit, dry=args.dry, extra=args.transcript)
    print("в очереди %d, разговоров %d, записано %d, режим %s"
          % (got["queued"], got["transcripts"], got["written"],
             "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
