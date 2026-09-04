#!/usr/bin/env python3
"""Отчёт по журналу аудита: каждый шаг цепочки, выполнен он или нет, с чем.

Запуск: python3 -m eval.audit_report --db путь/к/memory.db

Разбивка прогона отвечает «сколько», цепочка обрыва (`eval.live.chain`) —
«где порвалось первым». Ни то ни другое не показывает, что случилось на
остальных шагах, которые обрыв не назвал: разметка могла пройти, а факт — нет,
и вопрос «что именно записал маппер» до сих пор решался перечитыванием
транскрипта заново, в отдельном коде (`eval.live.marking`), который сам
приближение, а не то, что случилось на самом деле.

Здесь — построчный отчёт по таблице аудита (`storage.audit`), той самой, куда
конвейер писал ход по факту, без пересчёта задним числом. Порядок шагов —
`storage.audit.STEPS`: это и есть порядок, в котором знание проходит цепочку.
Шаг без единой строки называется явно — «не сработал ни разу», а не
пропускается: тишина шага и отсутствие вопроса о нём выглядят на экране
одинаково, если не сказать о тишине словами.

Ничего не сокращается: `--full` печатает вход и выход каждой строки целиком, в
JSON. Без него — по умолчанию — печатается счёт по шагам и первые несколько
строк на ознакомление; отчёт задачи собирается с `--full`.
"""
import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

from storage import audit


def steps_report(where, run=None, session_id=None):
    """Строки аудита, сгруппированные по шагу, в порядке `audit.STEPS`.

    Шаг без строк несёт пустой список, а не отсутствует в словаре: вызывающий
    не должен гадать, было ли про шаг вообще спрошено.
    """
    out = OrderedDict((step, []) for step in audit.STEPS)
    for row in audit.rows(where=where, run=run, session_id=session_id):
        out.setdefault(row["step"], []).append(row)
    return out


def row_text(row, full=True):
    """Одна строка аудита текстом: время, сессия, успех/отказ, вход и выход."""
    head = "  [%s] сессия=%s %s" % (
        row.get("ts") or "?", row.get("session_id") or "—",
        "успех" if row.get("ok") else "отказ")
    if not full:
        return head
    body = json.dumps({"input": row.get("input"), "output": row.get("output")},
                      ensure_ascii=False, indent=2)
    return "%s\n%s" % (head, "\n".join("    " + line for line in body.splitlines()))


def text(grouped, run=None, full=True, limit=None):
    """Отчёт целиком, строкой. `limit` — сколько строк на шаг без `--full`."""
    lines = ["Отчёт по журналу аудита%s" % (" — прогон %s" % run if run else "")]
    total = sum(len(rows) for rows in grouped.values())
    lines.append("строк всего: %d" % total)
    lines.append("")
    for step, rows in grouped.items():
        if not rows:
            lines.append("%s — не сработал ни разу за этот прогон" % step)
            lines.append("")
            continue
        lines.append("%s — %d строк%s" % (
            step, len(rows), "" if full or not limit or len(rows) <= limit
            else " (показаны первые %d)" % limit))
        shown = rows if full or not limit else rows[:limit]
        for row in shown:
            lines.append(row_text(row, full=full))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parser():
    ap = argparse.ArgumentParser(prog="eval.audit_report",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="путь к базе с таблицей audit; по умолчанию "
                                 "%s (см. storage.audit.path, XMEM_AUDIT_PATH)"
                                 % audit.path())
    ap.add_argument("--run", help="только строки этого номера прогона")
    ap.add_argument("--session", help="только строки этой сессии")
    ap.add_argument("--full", action="store_true",
                    help="печатать вход и выход каждой строки целиком, без обрезки")
    ap.add_argument("--limit", type=int, default=5,
                    help="без --full — сколько строк на шаг показывать (умолчание 5)")
    ap.add_argument("--out", help="куда записать отчёт; по умолчанию — на экран")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    where = Path(args.db) if args.db else None
    grouped = steps_report(where, run=args.run, session_id=args.session)
    body = text(grouped, run=args.run, full=args.full, limit=args.limit)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print("отчёт -> %s" % args.out)
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
