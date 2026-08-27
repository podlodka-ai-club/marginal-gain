#!/usr/bin/env python3
"""Модуль 4, Оценка. Прогоняет золотой набор и считает, где потерялось.

Сценарий это вопрос плюс строки, которые обязаны встретиться в ответе, и
строки, которых там быть не должно. Вторые важнее первых: без них набор
поощряет болтливость, а не память.

Каждый случай идёт под своей меткой, и все шаги конвейера пишутся в журнал
под ней же. Поэтому «прошло 7 из 100» перестаёт быть единственным числом:
видно, память ничего не нашла, или нашла и порог срезал.

Замер включается переменной: MEM_TRACE=1 python3 -m eval.evaluate
"""
import argparse, json, time
from collections import defaultdict
from pathlib import Path

from pipeline import suggest
from infra import telemetry

CASES = Path(__file__).parent / "eval-cases.json"
RESULTS = Path.home() / ".local" / "state" / "memory-encoder" / "eval-results.jsonl"


def judge(case, answer, raw, error):
    """Разбор одного результата. Упавший случай пройденным не считается.

    Раньше `ok` не смотрел на ошибку, и случай «промолчи» проходил всегда:
    сломанный конвейер молчит не хуже исправного. Итог не мог опуститься
    ниже пяти, сколько бы всё ни было сломано.
    """
    expect = case.get("expect") or []
    forbid = case.get("forbid") or []
    low_sent, low_raw = answer.lower(), (raw or "").lower()
    hits = [e for e in expect if e.lower() in low_sent]
    # `found_in_answer` — попадание в ответ памяти, а не в её содержимое.
    # При режиме single ответ синтезирован читателем, и факт может лежать
    # в хранилище, не попав в него. Названо по тому, что действительно меряет.
    raw_hits = [e for e in expect if e.lower() in low_raw]
    false_hits = [f for f in forbid if f.lower() in low_sent]
    ok = error is None and not false_hits and (bool(expect) and len(hits) == len(expect)
                                               or bool(forbid) and not expect)
    return {
        "ok": bool(ok),
        "found_in_answer": bool(expect) and len(raw_hits) == len(expect),
        "hits": hits, "missed": [e for e in expect if e not in hits],
        "false_hits": false_hits,
    }


def run_case(case, mode, min_score):
    started = time.time()
    with telemetry.Trace(case["id"]) as tr:
        try:
            answer, kept, raw = suggest.suggest(case["query"], mode, min_score)
            error = None
        except Exception as exc:
            answer, kept, raw, error = "", [], "", str(exc)[:300]
    verdict = judge(case, answer, raw, error)
    return {
        "id": case["id"], "kind": case.get("kind", ""), "trace_id": tr.trace_id,
        "query": case["query"], "ok": verdict["ok"],
        "found_in_answer": verdict["found_in_answer"],
        "hits": verdict["hits"], "missed": verdict["missed"],
        "false_hits": verdict["false_hits"], "need": len(case.get("expect") or []),
        "kept": len(kept), "seconds": round(time.time() - started, 2),
        "chars": len(answer), "raw_chars": len(raw or ""), "answer": answer,
        "error": error,
    }


def main():
    ap = argparse.ArgumentParser(description="Оценка памяти по золотому набору")
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--only", help="только сценарии, чей id содержит эту строку")
    ap.add_argument("--kind", help="только этот вид случаев")
    ap.add_argument("--mode", default="single", choices=["single", "raw", "xresponse"])
    ap.add_argument("--min-score", type=float, default=suggest.MIN_SCORE)
    args = ap.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print("нет файла сценариев %s — собери его: python3 -m eval.goldenset" % path)
        return
    cases = json.loads(path.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.kind:
        cases = [c for c in cases if c.get("kind") == args.kind]
    if not cases:
        print("ни один случай не подошёл под отбор")
        return

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with RESULTS.open("a", encoding="utf-8") as fh:
        for case in cases:
            res = run_case(case, args.mode, args.min_score)
            rows.append(res)
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            mark = "прошёл" if res["ok"] else "не прошёл"
            extra = ""
            if res["missed"]:
                extra += " не нашлось: %s" % ", ".join(res["missed"])
            if res["false_hits"]:
                extra += " лишнее: %s" % ", ".join(res["false_hits"])
            if not res["ok"] and res["found_in_answer"]:
                extra += " (память ответила, срезал порог)"
            if res["error"]:
                extra = " ошибка: %s" % res["error"]
            print("%-18s %-10s %4.1f с %5d симв.%s"
                  % (res["id"], mark, res["seconds"], res["chars"], extra), flush=True)

    print()
    print(summary(rows))
    telemetry.close()
    if telemetry.ENABLED:
        print()
        print("шаги конвейера, журнал %s:" % telemetry.LOG)
        print(telemetry.report(telemetry.read_log(telemetry.LOG),
                               run_id=telemetry.RUN_ID))


def summary(rows):
    """Сводка по видам плюс разбивка: где именно потерялось.

    Потери считаются только по случаям, где ответ памяти нужное содержал.
    Случаи «промолчи» ожидаемого не имеют вовсе, и складывать их с остальными
    нельзя: колонка уходила в минус.
    """
    by_kind = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        rec = by_kind[r["kind"] or "без вида"]
        rec[0] += 1
        rec[1] += r["ok"]
        rec[2] += r["found_in_answer"] and not r["ok"]
    lines = ["%-20s %8s %8s %16s" % ("вид", "всего", "прошло", "ответила, срезан")]
    for kind in sorted(by_kind):
        total, ok, lost = by_kind[kind]
        lines.append("%-20s %8d %8d %16d" % (kind, total, ok, lost))
    passed = sum(r["ok"] for r in rows)
    failed = sum(1 for r in rows if r["error"])
    answered = [r for r in rows if r["found_in_answer"]]
    lost = [r for r in answered if not r["ok"]]
    lines.append("")
    lines.append("итог: %d из %d" % (passed, len(rows)))
    if failed:
        lines.append("упало с ошибкой: %d (пройденными не считаются)" % failed)
    if answered:
        lines.append("память ответила нужным в %d случаях, из них срезал порог %d (%.0f%%)"
                     % (len(answered), len(lost), 100.0 * len(lost) / len(answered)))
    else:
        lines.append("память не ответила нужным ни разу: терять было нечего")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
