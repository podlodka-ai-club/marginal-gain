#!/usr/bin/env python3
"""Сравнение двух прогонов набора: без памяти и с памятью.

Зачем. Одно число «прошло столько-то» не говорит ничего: неизвестно, сколько
из этого прошло бы и без памяти вовсе. Разница двух прогонов — единственное,
что можно назвать вкладом памяти.

Как. Один и тот же набор, один и тот же порядок, одни и те же вопросы.
Отличие ровно одно: в первой половине память выключена целиком, чтение отдаёт
пустоту. Во второй работает как есть.

Сброс сессии. Между половинами и внутри каждой всё, что могло запомниться в
процессе, гасится: соединение с хранилищем закрывается, счётчики обнуляются,
журнал подсказок отводится в отдельный файл. Иначе вторая половина отвечает
не из памяти, а из остатков первой.

Запуск: MEM_TRACE=1 python3 matrix.py
"""
import argparse, importlib, json, os, time
from collections import defaultdict
from pathlib import Path

BASELINE = "без памяти"
ACTIVE = "с памятью"


def load_env(path=".env"):
    """Ключ доступа держим в файле, а не в командной строке."""
    env = Path(path)
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(name.strip(), value.strip())


def reset_session():
    """Чистая сессия: ничто из прошлого прогона не должно дожить до следующего."""
    import telemetry
    telemetry.close()
    telemetry._COUNTS.clear()
    try:
        import xmem_sdk
        xmem_sdk.close()
    except Exception:
        pass


def reload_pipeline():
    """Перечитываем модули: выключатель памяти читается при импорте.

    Метку прогона проносим через перезагрузку: иначе половины сравнения
    получают разные метки и отчёт по прогону распадается надвое.
    """
    import telemetry
    run_id = telemetry.RUN_ID
    import xmem, xmem_api, xmem_sdk, understand, suggest, evaluate
    for mod in (telemetry, xmem_api, xmem_sdk, xmem, understand, suggest, evaluate):
        importlib.reload(mod)
    telemetry.RUN_ID = run_id
    return suggest, evaluate


def run_phase(phase, cases, disabled, mode, min_score):
    os.environ["XMEM_DISABLED"] = "1" if disabled else ""
    reset_session()
    suggest, evaluate = reload_pipeline()
    import telemetry

    rows = []
    started = time.time()
    for case in cases:
        t = time.time()
        with telemetry.Trace(case["id"], phase=phase):
            try:
                answer, kept, raw = suggest.suggest(case["query"], mode, min_score)
                error = None
            except Exception as exc:
                answer, kept, raw, error = "", [], "", str(exc)[:200]
        # Разбор один на оба прогона: своя копия правил разъехалась бы
        # с оценкой, и половины перестали бы быть сравнимыми.
        verdict = evaluate.judge(case, answer, raw, error)
        rows.append({
            "phase": phase, "id": case["id"], "kind": case.get("kind", ""),
            "ok": verdict["ok"], "found_in_answer": verdict["found_in_answer"],
            "missed": verdict["missed"], "false_hits": verdict["false_hits"],
            "kept": len(kept), "chars": len(answer), "raw_chars": len(raw or ""),
            "seconds": round(time.time() - t, 2), "error": error,
        })
    reset_session()
    return rows, time.time() - started


def score(rows):
    return sum(r["ok"] for r in rows), len(rows)


def by_kind(rows):
    out = defaultdict(lambda: [0, 0])
    for r in rows:
        rec = out[r["kind"] or "без вида"]
        rec[0] += 1
        rec[1] += r["ok"]
    return out


def compare(base, active, base_sec, active_sec):
    bp, bt = score(base)
    ap, at = score(active)
    lines = ["%-22s %8s %8s %8s" % ("вид", BASELINE, ACTIVE, "разница")]
    kb, ka = by_kind(base), by_kind(active)
    for kind in sorted(set(kb) | set(ka)):
        b, a = kb[kind][1], ka[kind][1]
        total = max(kb[kind][0], ka[kind][0])
        lines.append("%-22s %4d/%-3d %4d/%-3d %+8d" % (kind, b, total, a, total, a - b))
    lines.append("")
    lines.append("итог: %d из %d без памяти, %d из %d с памятью" % (bp, bt, ap, at))
    lines.append("вклад памяти: %+d случая (%+.1f процентных пункта)"
                 % (ap - bp, 100.0 * (ap - bp) / max(1, at)))

    # Потери считаем только по тем случаям, где ответ памяти нужное содержал.
    # Складывать их с прошедшими на молчании нельзя: знаменатель другой.
    knew = [r for r in active if r["found_in_answer"]]
    lost = [r for r in knew if not r["ok"]]
    if knew:
        lines.append("память ответила нужным в %d случаях, из них срезал порог %d (%.0f%%)"
                     % (len(knew), len(lost), 100.0 * len(lost) / len(knew)))
    else:
        lines.append("память не ответила нужным ни разу: терять было нечего")
    broken = sum(1 for r in active if r["error"])
    if broken:
        lines.append("упало с ошибкой: %d (пройденными не считаются)" % broken)
    lines.append("время: %.1f с без памяти, %.1f с с памятью" % (base_sec, active_sec))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Сравнение прогонов без памяти и с памятью")
    ap.add_argument("--cases", default="eval-cases.json")
    ap.add_argument("--limit", type=int, help="взять только первые N случаев")
    ap.add_argument("--kind", help="только этот вид случаев")
    ap.add_argument("--mode", default="single", choices=["single", "raw", "xresponse"])
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--out", default="", help="куда сложить построчный итог")
    args = ap.parse_args()

    load_env()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.kind:
        cases = [c for c in cases if c.get("kind") == args.kind]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("ни один случай не подошёл под отбор")
        return

    print("случаев: %d\n" % len(cases))
    print("--- половина 1: %s ---" % BASELINE, flush=True)
    base, base_sec = run_phase(BASELINE, cases, True, args.mode, args.min_score)
    print("прошло %d из %d за %.1f с\n" % (*score(base), base_sec), flush=True)

    print("--- половина 2: %s ---" % ACTIVE, flush=True)
    active, active_sec = run_phase(ACTIVE, cases, False, args.mode, args.min_score)
    print("прошло %d из %d за %.1f с\n" % (*score(active), active_sec), flush=True)

    print(compare(base, active, base_sec, active_sec))

    if args.out:
        Path(args.out).write_text(json.dumps(base + active, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print("\nпострочный итог -> %s" % args.out)

    import telemetry
    if telemetry.ENABLED and Path(telemetry.LOG).exists():
        rows = telemetry.read_log(telemetry.LOG)
        for phase in (BASELINE, ACTIVE):
            print("\n--- шаги конвейера, %s ---" % phase)
            print(telemetry.report(rows, phase=phase, run_id=telemetry.RUN_ID))


if __name__ == "__main__":
    main()
