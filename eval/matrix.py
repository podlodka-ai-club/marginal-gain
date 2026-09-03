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

Запуск: MEM_TRACE=1 python3 -m eval.matrix
"""
import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from eval import evaluate, goldenset

# Тот же набор и тем же путём, что у одиночного прогона: два места с одним
# именем файла разъезжаются молча, и половины перестают быть сравнимыми.
CASES = evaluate.ROOT / "eval-cases.json"

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
    """Чистая сессия: ничто из прошлого прогона не должно дожить до следующего.

    Пути наружу закрывает сама дверь: перечисление руками уже забыло
    storage.local, и его кэш соединения жил насквозь через обе половины.
    """
    from infra import telemetry
    from storage import port
    telemetry.close()
    telemetry._COUNTS.clear()
    try:
        port.close_all()
    except Exception:
        pass


def run_phase(phase, cases, disabled, mode, min_score):
    """Половину выбираем окружением. Перезагружать модули больше не нужно.

    Раньше выключатель памяти читался при импорте двери, и сменить половину
    можно было только importlib.reload — с ним же уезжала метка прогона, и
    её приходилось проносить руками. Теперь путь наружу выбирается при
    вызове door(), и перезагружать нечего.
    """
    os.environ["XMEM_DISABLED"] = "1" if disabled else ""
    reset_session()
    from pipeline import suggest
    from infra import telemetry

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
        # с оценкой, и половины перестали бы быть сравнимыми. Найденным
        # считается то, что могло дойти до агента: ложные находки убирает тот
        # же отсев, каким подсказка чистит выдачу.
        known, cut = suggest.knowledge(raw, case["query"]) if raw else ("", 0)
        verdict = evaluate.judge(case, answer, known, error, raw=raw)
        rows.append({
            "phase": phase, "id": case["id"], "kind": case.get("kind", ""),
            "ok": verdict["ok"], "found_in_answer": verdict["found_in_answer"],
            "false_find": verdict["false_find"], "sifted": cut,
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
    false = sum(1 for r in active if r.get("false_find"))
    lines.append("ложных находок отсеяно %d: слово было только внутри команды в событии"
                 % false)
    lines.append("отсев убрал %d кусков сырого ответа"
                 % sum(r.get("sifted") or 0 for r in active))
    broken = sum(1 for r in active if r["error"])
    if broken:
        lines.append("упало с ошибкой: %d (пройденными не считаются)" % broken)
    lines.append("время: %.1f с без памяти, %.1f с с памятью" % (base_sec, active_sec))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Сравнение прогонов без памяти и с памятью")
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--limit", type=int, help="взять только первые N случаев")
    ap.add_argument("--kind", help="только этот вид случаев")
    ap.add_argument("--mode", default="single", choices=["single", "raw", "xresponse"])
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--out", default="", help="куда сложить построчный итог")
    ap.add_argument("--as-of", dest="as_of",
                    help="момент, на который считать сроки; по умолчанию из набора")
    args = ap.parse_args()

    load_env()
    meta, cases = goldenset.load(args.cases, "cases")
    print("набор версии %d, подпись факта: %s" % (meta["version"], meta.get("identity")))
    at = evaluate.pin(args.as_of or goldenset.as_of(meta))
    print(evaluate.state_line(at))
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

    from infra import telemetry
    if telemetry.ENABLED and Path(telemetry.LOG).exists():
        rows = telemetry.read_log(telemetry.LOG)
        for phase in (BASELINE, ACTIVE):
            print("\n--- шаги конвейера, %s ---" % phase)
            print(telemetry.report(rows, phase=phase, run_id=telemetry.RUN_ID))


if __name__ == "__main__":
    main()
