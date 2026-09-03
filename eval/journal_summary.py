#!/usr/bin/env python3
"""Сводка по журналу: сколько раз пара прошла каждую ступень цепочки.

Запуск: python3 -m eval.journal_summary --pair макбук --pair город

Журнал (`eval-runs.jsonl`) копит по строке на прогон, а не по строке на пару:
вопрос «плавает ли запись» смотрит на одну пару через несколько прогонов, и
собрать эту цифру руками, читая строки глазами, значит обречь её на ошибку
округления в чью-то пользу. Здесь она считается один раз, скриптом, и число
можно перепроверить, прогнав тот же скрипт снова.

Строка засчитывается в сводку одной пары только если `only` в ней — та же
пара: полные прогоны (не сузившие набор) и прогоны по чужой паре в счёт этой
пары не идут. Отличимость урезанного прогона от полного и есть то, что делает
эту сводку возможной — см. `eval.live.journal_row`.
"""
import argparse
import json
from collections import OrderedDict
from pathlib import Path

from eval import live

DEFAULT_ARM = "memory"


def rows_of(journal_path):
    """Строки журнала, по одной на прогон, в порядке, в котором дописаны."""
    path = Path(journal_path)
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def runs_of(rows, pair_id, arm=DEFAULT_ARM):
    """Прогоны, урезанные ровно до этой пары этим ключом, с этой рукой сыгранной.

    `only` — то самое поле, ради которого эта сводка вообще возможна: без
    него урезанный прогон неотличим от полного, и посчитанные вместе они
    смешали бы знаменатель.
    """
    return [row for row in rows
           if row.get("only") == pair_id and arm in row.get("arms", {})]


def pair_entries(runs, pair_id, arm=DEFAULT_ARM):
    """Строка пары `pair_id` из каждого прогона — та, что реально играна.

    Один урезанный прогон обязан дать ровно одну такую строку: `only`
    сузил набор до этой пары, и второй в нём взяться неоткуда. Прогон, где
    строки пары нет вовсе или их больше одной, — не тот прогон, который эта
    сводка умеет считать, и роняется явно, а не молча пропускается: молчание
    здесь дало бы заниженный знаменатель, а не честный.
    """
    out = []
    for row in runs:
        matches = [entry for entry in row["arms"][arm]["pairs"]
                  if entry["id"] == pair_id]
        if len(matches) != 1:
            raise ValueError(
                "прогон от %s несёт %d строк пары %r с рукой %s, ждали ровно 1"
                % (row.get("ts"), len(matches), pair_id, arm))
        out.append(matches[0])
    return out


def steps_reached(break_field):
    """Какие ступени цепочки пройдены, по полю `break` одной пары одного прогона.

    `break` — первая ступень, ответившая «нет» (`eval.live.break_of`):
    пустая строка значит, что пара дошла до конца цепочки — либо победила,
    либо забуксовала уже после неё, на применении, а не на доставке. Ступени
    ДО обрыва пройдены по построению `break_of` (первый `False` в порядке
    `STEPS`), сама ступень обрыва — нет, а что было после нас не спрашивали:
    цепочка встала раньше, и записывать дальше как «пройдено» значило бы
    выдумать цифру, которой прогон не давал.
    """
    if break_field == "":
        return {step: True for step in live.STEPS}
    idx = live.STEPS.index(break_field)
    return {step: (i < idx) for i, step in enumerate(live.STEPS)}


def summarize(rows, pair_id, arm=DEFAULT_ARM):
    """Сводка одной пары: сколько прогонов, по каждой ступени и по итогу.

    Возвращает `{"total": N, "steps": {ступень: сколько прошли}, "passed": M}`.
    `passed` — то же «засчитана», что в отчёте прогона: `bucket(row) == APPLIED`
    (`Report.passed`), не свой пересчёт удачи.
    """
    runs = runs_of(rows, pair_id, arm=arm)
    entries = pair_entries(runs, pair_id, arm=arm)
    steps = OrderedDict((step, 0) for step in live.STEPS)
    passed = 0
    for entry in entries:
        reached = steps_reached(entry["break"])
        for step in live.STEPS:
            steps[step] += int(reached[step])
        passed += int(entry["outcome"] == live.APPLIED)
    return {"total": len(entries), "steps": steps, "passed": passed}


def text(pair_id, summary):
    total = summary["total"]
    lines = ["%s — %d прогонов" % (pair_id, total)]
    for step, count in summary["steps"].items():
        lines.append("  %-10s %d из %d" % (step, count, total))
    lines.append("  %-10s %d из %d" % ("засчитана", summary["passed"], total))
    return "\n".join(lines)


def parser():
    ap = argparse.ArgumentParser(prog="eval.journal_summary",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=str(live.DEFAULT_JOURNAL),
                    help="откуда читать журнал (по умолчанию %s)"
                         % live.DEFAULT_JOURNAL.name)
    ap.add_argument("--pair", action="append", required=True,
                    help="id пары; ключ можно повторять")
    ap.add_argument("--arm", default=DEFAULT_ARM,
                    help="какая рука считается (по умолчанию %s)" % DEFAULT_ARM)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    rows = rows_of(args.journal)
    for pair_id in args.pair:
        summary = summarize(rows, pair_id, arm=args.arm)
        print(text(pair_id, summary))
        print()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
