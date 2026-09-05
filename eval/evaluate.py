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
import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from domain import lifespan
from eval import goldenset
from pipeline import suggest
from infra import config, telemetry
from storage import port

# Набор лежит в корне репозитория, а не внутри пакета: его собирают, читают и
# коммитят рядом с кодом. Путь абсолютный, потому что замер зовут и из корня, и
# из хука, и из планировщика — текущий каталог у всех троих разный.
ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval-cases.json"
RESULTS = config.state_dir() / "eval-results.jsonl"


def state_line(as_of):
    """На каком состоянии базы сделан прогон. Печатается до первого случая.

    Без этой строки цифра ни с чем не сходится: заработало забывание, шестьдесят
    семь фактов уехали в отложенное, и итог упал на шесть пунктов без единой
    правки кода. Три числа показывают, что именно переливается: фактов и
    отложенных стало иначе, живых на момент — столько же.

    Путь наружу о своём состоянии рассказывать не обязан. Сетевой не умеет, и
    ронять на нём замер нельзя: скажем то, что знаем, — момент.
    """
    at = as_of or "сейчас"
    door = port.door()
    tell = getattr(door, "state", None)
    if tell is None:
        return "состояние: путь %s о себе не рассказывает, сроки на %s" % (
            getattr(door, "name", "?"), at)
    try:
        got = tell(as_of)
    except AttributeError:
        return "состояние: путь %s о себе не рассказывает, сроки на %s" % (
            getattr(door, "name", "?"), at)
    return ("состояние базы: фактов %d, отложенных %d, живых на момент %d, "
            "сроки на %s" % (got["facts"], got["lapsed"], got["alive"], at))


def _in_kind(word, text):
    """Слово категории в тексте: совпадение с начала слова, а не подстрокой.

    Категория — закрытый словарь основ, и основы у неё короткие: «нут», «мяс»,
    «боб». Голая подстрока ловит их в «минут», «замяться» и «оба», то есть
    находит мясо там, где его нет, и делает это молча. Начало слова снимает
    ровно этот класс, оставляя падежи: «нут», «нута», «нутом» — те же.

    Отдельные слова `expect`/`forbid` этого правила не знают и знать не должны:
    по ним считана история цифр семи прежних пар, и сдвинь мы правило — старая
    цифра стала бы несравнима с новой, выглядя точно так же.
    """
    return re.search(r"(?<![^\W\d_])%s" % re.escape(word.lower()),
                     text, re.UNICODE) is not None


def judge(case, answer, known, error, raw=None):
    """Разбор одного результата. Упавший случай пройденным не считается.

    Раньше `ok` не смотрел на ошибку, и случай «промолчи» проходил всегда:
    сломанный конвейер молчит не хуже исправного. Итог не мог опуститься
    ниже пяти, сколько бы всё ни было сломано.

    `known` — сырой ответ памяти без ложных находок, его готовит тот же отсев,
    каким подсказка чистит выдачу (`suggest.knowledge`). Раньше на его месте
    стоял сырой ответ целиком, и находкой считалась любая подстрока: слово,
    случайно попавшее внутрь команды в событии, записывалось в «нашли» и
    навсегда оставалось необъяснимой потерей. Одно правило на обе стороны,
    иначе «нашли» и «отдали» меряют разные миры.

    Категории (`vocab`, разрешённые загрузчиком набора из имён в закрытые
    словари, см. `eval.pairs`) судятся здесь же и тем же вхождением строки, а
    не своим правилом: разойдись они, «было в ответе мясо» отвечало бы одной
    линейкой, а «было ожидаемое» — другой. Категория ожидается хотя бы одним
    своим словом и запрещается ни одним.
    """
    expect = case.get("expect") or []
    forbid = case.get("forbid") or []
    vocab = case.get("vocab") or {}
    wanted_kinds = vocab.get("expect") or {}
    banned_kinds = vocab.get("forbid") or {}
    low_sent = answer.lower()
    low_known = (known or "").lower()
    low_raw = (raw if raw is not None else known or "").lower()
    hits = [e for e in expect if e.lower() in low_sent]
    # `found_in_answer` — попадание в ответ памяти, а не в её содержимое.
    # При режиме single ответ синтезирован читателем, и факт может лежать
    # в хранилище, не попав в него. Названо по тому, что действительно меряет.
    known_hits = [e for e in expect if e.lower() in low_known]
    raw_hits = [e for e in expect if e.lower() in low_raw]
    false_hits = [f for f in forbid if f.lower() in low_sent]
    # Категория ожидается «хотя бы одним словом», запрещается «ни одним».
    # Название категории и есть то, чего не хватило или что приплелось: слово
    # из закрытого словаря говорит, чем именно категория себя показала.
    hit_kinds = [name for name, words in wanted_kinds.items()
                 if any(_in_kind(w, low_sent) for w in words)]
    known_kinds = [name for name, words in wanted_kinds.items()
                   if any(_in_kind(w, low_known) for w in words)]
    raw_kinds = [name for name, words in wanted_kinds.items()
                 if any(_in_kind(w, low_raw) for w in words)]
    false_hits += [w for words in banned_kinds.values() for w in words
                   if _in_kind(w, low_sent)]
    missed = ([e for e in expect if e not in hits]
              + [name for name in wanted_kinds if name not in hit_kinds])
    # «Есть чего ждать» и «есть что запрещать» — две отдельные величины: пара
    # без ожиданий вовсе это «промолчи», и она проходит молчанием. Категории
    # входят в обе на равных с отдельными словами, иначе пара, судящая только
    # категориями, читалась бы как «промолчи» и проходила бы чем угодно.
    wanted = bool(expect) or bool(wanted_kinds)
    banned = bool(forbid) or bool(banned_kinds)
    ok = error is None and not false_hits and (
        (wanted and not missed) or (banned and not wanted))
    found = wanted and not ([e for e in expect if e not in known_hits]
                            + [n for n in wanted_kinds if n not in known_kinds])
    found_raw = wanted and not ([e for e in expect if e not in raw_hits]
                                + [n for n in wanted_kinds if n not in raw_kinds])
    return {
        "ok": bool(ok),
        "found_in_answer": bool(found),
        # Ложная находка: в сыром ответе слово есть, знанием оно не является.
        # Требуем, чтобы не уцелело ничего: случай, где одно ожидаемое слово
        # пережило отсев, а второе нет, — не мусор, а потеря на извлечении, и
        # записывать его в мусор значит завышать долю отсеянного.
        "false_find": bool(wanted and not found and not known_hits
                           and not known_kinds and found_raw),
        "hits": hits + hit_kinds, "missed": missed,
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
    # Найденным считается то, что могло дойти: ложные находки отсеивает тот же
    # код, что чистит выдачу.
    known, cut = suggest.knowledge(raw, case["query"]) if raw else ("", 0)
    verdict = judge(case, answer, known, error, raw=raw)
    return {
        "id": case["id"], "kind": case.get("kind", ""), "trace_id": tr.trace_id,
        "query": case["query"], "ok": verdict["ok"],
        "found_in_answer": verdict["found_in_answer"],
        "false_find": verdict["false_find"], "sifted": cut,
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
    ap.add_argument("--as-of", dest="as_of",
                    help="момент, на который считать сроки; по умолчанию из набора")
    args = ap.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print("нет файла сценариев %s — собери его: python3 -m eval.goldenset" % path)
        return
    # Версию набора проверяет загрузчик: цифра, снятая на чужой сборке, не
    # сравнима с прежней, а выглядит точно так же.
    try:
        meta, cases = goldenset.load(path, "cases")
    except goldenset.SetVersionError as bad:
        print(bad)
        return
    print("набор версии %d, подпись факта: %s, собран %s"
          % (meta["version"], meta.get("identity"), meta.get("built_at")))
    # Момент замера ставим в окружение, а не тащим через все слои: чтение зовут
    # из подсказки и из хука, и им про момент знать нечего.
    at = pin(args.as_of or goldenset.as_of(meta))
    print(state_line(at))
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
            if res["false_find"]:
                extra += " (ложная находка: слово только внутри команды)"
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


def pin(as_of):
    """Закрепить момент на весь прогон. Отдаёт то, что закрепил.

    Через окружение, тем же способом, каким выбирается путь наружу: чтение
    ходит из подсказки, из хука и отсюда, и передавать момент руками пришлось
    бы через каждый из трёх.
    """
    at = lifespan.stamp(as_of) if as_of else ""
    os.environ["XMEM_AS_OF"] = at
    return at


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
    false = [r for r in rows if r.get("false_find")]
    lines.append("")
    lines.append("итог: %d из %d" % (passed, len(rows)))
    if failed:
        lines.append("упало с ошибкой: %d (пройденными не считаются)" % failed)
    if answered:
        lines.append("память ответила нужным в %d случаях, из них срезал порог %d (%.0f%%)"
                     % (len(answered), len(lost), 100.0 * len(lost) / len(answered)))
    else:
        lines.append("память не ответила нужным ни разу: терять было нечего")
    # Ложная находка потерей не является: слово встретилось внутри команды в
    # событии, знанием оно не было. Считаем отдельно, чтобы разрыв «нашли —
    # отдали» не приписывал себе чужой мусор.
    lines.append("ложных находок отсеяно %d: слово было только внутри команды в событии"
                 % len(false))
    # Объём среза отдельным числом: срезанное зря по итогу неотличимо от
    # срезанного по делу, и перетянутый отсев выглядел бы улучшением.
    lines.append("отсев убрал %d кусков сырого ответа"
                 % sum(r.get("sifted") or 0 for r in rows))
    return "\n".join(lines)


if __name__ == "__main__":
    main()
