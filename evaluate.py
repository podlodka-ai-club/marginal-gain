#!/usr/bin/env python3
"""Модуль 4, Оценка. Наивно: прогоняем сценарии из GOLDEN_SET и считаем попадания.

Сценарий это вопрос плюс строка, которая обязана встретиться в ответе.
Без этого модуля мы не узнаем, помогает память или мешает.
"""
import argparse, json, time
from pathlib import Path

import suggest

CASES = Path(__file__).parent / "eval-cases.json"
RESULTS = Path.home() / ".local" / "state" / "memory-encoder" / "eval-results.jsonl"


def run_case(case):
    started = time.time()
    try:
        answer = suggest.suggest(case["query"])
        error = None
    except Exception as exc:
        answer, error = "", str(exc)[:300]
    expect = case.get("expect") or []
    forbid = case.get("forbid") or []
    hits = [e for e in expect if e.lower() in answer.lower()]
    false_hits = [f for f in forbid if f.lower() in answer.lower()]
    return {
        "id": case["id"],
        "query": case["query"],
        "ok": bool(expect) and len(hits) == len(expect) and not false_hits,
        "hits": hits, "missed": [e for e in expect if e not in hits],
        "false_hits": false_hits,
        "seconds": round(time.time() - started, 2),
        "chars": len(answer), "error": error, "answer": answer,
    }


def main():
    ap = argparse.ArgumentParser(description="Оценка памяти по сценариям")
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--only", help="только сценарии, чей id содержит эту строку")
    args = ap.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print("нет файла сценариев %s" % path)
        return
    cases = json.loads(path.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    passed = 0
    with RESULTS.open("a", encoding="utf-8") as fh:
        for case in cases:
            res = run_case(case)
            passed += res["ok"]
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            mark = "прошёл" if res["ok"] else "не прошёл"
            extra = ""
            if res["missed"]:
                extra += " не нашлось: %s" % ", ".join(res["missed"])
            if res["false_hits"]:
                extra += " лишнее: %s" % ", ".join(res["false_hits"])
            if res["error"]:
                extra += " ошибка: %s" % res["error"]
            print("%-6s %-6s %4.1f с %5d симв.%s" % (case["id"], mark, res["seconds"], res["chars"], extra))
    print("\nитог: %d из %d" % (passed, len(cases)))


if __name__ == "__main__":
    main()
