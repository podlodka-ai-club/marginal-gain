#!/usr/bin/env python3
"""Честная проверка меры: делим архив по времени, учимся на первой половине.

Вопрос, на который отвечает проверка: предсказывает ли оценка факта то, что
факт повторится дальше? Если высокая оценка не связана с повторением, мера
бесполезна, как бы красиво ни выглядел её верх.

Золотой набор тут не используется: он выведен из того же архива и подтвердил
бы сам себя.
"""
import argparse
from collections import defaultdict
from pathlib import Path

from pipeline import understand as u

SPLIT = "2026-08-09"


def collect(files, split):
    train, test = defaultdict(lambda: {"n": 0, "last": "", "projects": set()}), defaultdict(int)
    for path in files:
        for ep in u.episodes_from_file(path):
            day = (ep["ended_at"] or "")[:10]
            if not day:
                continue
            project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
            for fact in u.facts_of(ep):
                key = u.fact_key(*fact)
                if day < split:
                    rec = train[key]
                    rec["n"] += 1
                    rec["projects"].add(project)
                    if ep["ended_at"] > rec["last"]:
                        rec["last"] = ep["ended_at"]
                else:
                    test[key] += 1
    return train, test


def main():
    ap = argparse.ArgumentParser(description="Проверка меры на отложенной половине архива")
    ap.add_argument("--split", default=SPLIT)
    args = ap.parse_args()

    files = sorted(u.TRANSCRIPTS.rglob("*.jsonl"))
    train, test = collect(files, args.split)
    newest = max((r["last"] for r in train.values() if r["last"]), default="")
    print("граница: %s | фактов в первой половине: %d, во второй: %d"
          % (args.split, len(train), len(test)))
    print()

    buckets = defaultdict(lambda: [0, 0])
    for key, rec in train.items():
        score = u.score_of(rec, newest)
        b = "%.1f" % (int(score * 10) / 10)
        buckets[b][0] += 1
        if key in test:
            buckets[b][1] += 1

    print("оценка | фактов | сбылось | доля")
    for b in sorted(buckets):
        total, hit = buckets[b]
        print("  %s  | %5d  | %5d   | %5.1f%%" % (b, total, hit, 100 * hit / total))

    high = sum(v[1] for k, v in buckets.items() if float(k) >= 0.5)
    high_n = sum(v[0] for k, v in buckets.items() if float(k) >= 0.5)
    low = sum(v[1] for k, v in buckets.items() if float(k) < 0.5)
    low_n = sum(v[0] for k, v in buckets.items() if float(k) < 0.5)
    print()
    if high_n and low_n:
        print("оценка >= 0.5: сбылось %.1f%% (%d из %d)" % (100 * high / high_n, high, high_n))
        print("оценка <  0.5: сбылось %.1f%% (%d из %d)" % (100 * low / low_n, low, low_n))
        print("разница: %.1f раза" % ((high / high_n) / (low / low_n) if low else 0))


if __name__ == "__main__":
    main()
