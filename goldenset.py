#!/usr/bin/env python3
"""Сборка золотого набора из нашего же архива разговоров.

Разметка берётся задним числом: факт считается положительным, если к нему
обратились ещё раз позже границы. Человека не спрашиваем, поэтому набор
пересобирается в любой момент и не устаревает вместе с мнением разметчика.

Личные данные вычищаются: домашний путь, имя пользователя, почта, адреса
машин. Подмена детерминирована, поэтому один и тот же путь всюду выглядит
одинаково и связи между записями не рвутся.

Набор покрывает то, что схема умеет хранить: факты о файлах, предпочтения,
внешние адреса, связи между фактами одного эпизода и обстановку эпизода.
Отдельно кладутся случаи, где память обязана промолчать.

Запуск: python3 goldenset.py --out eval-cases.json --fixture eval-fixture.json
"""
import argparse, hashlib, json, re
from collections import defaultdict
from pathlib import Path

import understand as u
from encoder import redact

SPLIT = "2026-08-09"          # та же граница, что в holdout.py и в замерах
TARGET = 100                  # размер набора
HOME = str(Path.home())
USER = Path.home().name

# Личное, что встречается в транскриптах открытым текстом.
PERSONAL = [
    (re.compile(re.escape(HOME)), "/home/person"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "person@example.org"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "10.0.0.1"),
    (re.compile(r"\bhttps?://[\w.-]*:[^@\s/]+@"), "https://person:secret@"),
]
if USER:
    PERSONAL.append((re.compile(r"\b%s\b" % re.escape(USER)), "person"))


def anonymize(text):
    """Секреты и личное. Порядок важен: сначала секреты, потом личное."""
    text = redact(text or "")
    for pat, repl in PERSONAL:
        text = pat.sub(repl, text)
    return text


def short(key):
    """Короткое устойчивое имя случая. По ключу факта, а не по счётчику."""
    return hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:8]


def collect(files):
    """Все вхождения фактов с привязкой к эпизоду и времени."""
    occ = defaultdict(list)
    episodes = {}
    for path in files:
        for ep in u.episodes_from_file(path):
            day = (ep["ended_at"] or "")[:10]
            if not day:
                continue
            eid = (ep["session_id"], ep["number"])
            episodes[eid] = ep
            for fact in u.facts_of(ep):
                occ[u.fact_key(*fact)].append({"eid": eid, "day": day, "fact": fact})
    return occ, episodes


def label(occ, split):
    """Положительный = к факту обратились ещё раз после границы."""
    out = {}
    for key, items in occ.items():
        before = [i for i in items if i["day"] < split]
        after = [i for i in items if i["day"] >= split]
        if before:
            out[key] = {"items": before, "repeated": bool(after), "n": len(before)}
    return out


def file_of(content):
    if "правился файл " not in content:
        return ""
    return content.split("правился файл ")[-1].split(" ради")[0]


def case_fact(key, rec):
    """Спрашиваем про проект, ждём путь файла, который в нём правили."""
    fact = rec["items"][-1]["fact"]
    path = anonymize(file_of(fact[3]))
    if not path or "/" not in path:
        return None
    return {
        "id": "fact-%s" % short(key),
        "kind": "fact",
        "query": "Какие файлы правились в проекте %s?" % anonymize(fact[1]),
        "expect": [Path(path).name],
        "forbid": [],
        "repeated": rec["repeated"],
        "occurrences": rec["n"],
    }


def case_pref(key, rec):
    fact = rec["items"][-1]["fact"]
    return {
        "id": "pref-%s" % short(key),
        "kind": "preference",
        "query": "О чём пользователь просил в связи с темой «%s»?" % anonymize(fact[1]),
        "expect": [anonymize(fact[1])],
        "forbid": [],
        "repeated": rec["repeated"],
        "occurrences": rec["n"],
    }


def case_url(key, rec):
    fact = rec["items"][-1]["fact"]
    url = anonymize(fact[3].split()[-1])
    host = url.split("//")[-1].split("/")[0]
    if not host:
        return None
    return {
        "id": "url-%s" % short(key),
        "kind": "external_resource",
        "query": "Какие внешние адреса использовались в проекте %s?" % anonymize(fact[1]),
        "expect": [host],
        "forbid": [],
        "repeated": rec["repeated"],
        "occurrences": rec["n"],
    }


def case_link(eid, ep, marked):
    """Два факта из одного эпизода: проверяем, что память отдаёт связанное."""
    facts = [f for f in u.facts_of(ep) if u.fact_key(*f) in marked]
    files = [anonymize(file_of(f[3])) for f in facts if file_of(f[3])]
    files = [Path(f).name for f in files if f and "/" in f]
    if len(set(files)) < 2:
        return None
    project = anonymize(Path(ep["cwd"]).name if ep["cwd"] else "unknown")
    return {
        "id": "link-%s" % short(eid),
        "kind": "association",
        "query": "Что правилось вместе в проекте %s в одной задаче?" % project,
        "expect": sorted(set(files))[:2],
        "forbid": [],
        "repeated": True,
        "occurrences": len(facts),
    }


def case_context(eid, ep):
    """Обстановка эпизода: проект, ветка, исход."""
    project = anonymize(Path(ep["cwd"]).name if ep["cwd"] else "unknown")
    if not ep["branch"] or project == "unknown":
        return None
    return {
        "id": "ctx-%s" % short(eid),
        "kind": "context",
        "query": "В какой ветке шла работа над проектом %s?" % project,
        "expect": [anonymize(ep["branch"])],
        "forbid": [],
        "repeated": True,
        "occurrences": 1,
    }


ABSENT = [
    ("Что известно про проект sirius-quantum-ledger?", "sirius-quantum-ledger"),
    ("Какие файлы правились в проекте helios-payments?", "helios-payments"),
    ("Что пользователь просил про работу с камерами наблюдения?", "камер"),
    ("Какие адреса использовались для проекта tundra-crm?", "tundra-crm"),
    ("В какой ветке шла работа над проектом obsidian-mill?", "obsidian-mill"),
]


def case_absent(i, query, token):
    """Память обязана промолчать. Без таких случаев набор поощряет болтливость."""
    return {
        "id": "absent-%d" % i,
        "kind": "absence",
        "query": query,
        "expect": [],
        "forbid": [token],
        "repeated": False,
        "occurrences": 0,
    }


def fixture(marked, episodes):
    """Записи схемы, которыми набор наполняется, если хранилище пустое."""
    import models
    out = []
    for key, rec in sorted(marked.items(), key=lambda kv: repr(kv[0])):
        fact = rec["items"][-1]["fact"]
        out.append(models.Fact(fact_type=fact[0], subject=anonymize(fact[1]),
                               scope=fact[2], content=anonymize(fact[3])).mutation())
    return out


def build(split, target):
    files = sorted(u.TRANSCRIPTS.rglob("*.jsonl"))
    occ, episodes = collect(files)
    marked = label(occ, split)

    # Сначала повторившиеся: на них разметка положительная и случай осмыслен.
    order = sorted(marked.items(), key=lambda kv: (not kv[1]["repeated"],
                                                   -kv[1]["n"], repr(kv[0])))
    cases, seen = [], set()
    makers = {"project_state": case_fact, "preference": case_pref,
              "external_resource": case_url}
    for key, rec in order:
        maker = makers.get(rec["items"][-1]["fact"][0])
        case = maker(key, rec) if maker else None
        if case and case["expect"] and case["id"] not in seen:
            seen.add(case["id"])
            cases.append(case)

    used = {c["id"] for c in cases}
    for eid, ep in sorted(episodes.items()):
        for maker in (case_link, case_context):
            case = maker(eid, ep, marked) if maker is case_link else maker(eid, ep)
            if case and case["id"] not in used:
                used.add(case["id"])
                cases.append(case)

    # Доля по видам держится явно, иначе набор перекашивает в самый частый вид.
    quota = {"fact": 40, "preference": 15, "external_resource": 15,
             "association": 15, "context": 10}
    picked = []
    for kind, limit in quota.items():
        picked += [c for c in cases if c["kind"] == kind][:limit]
    picked += [case_absent(i, q, t) for i, (q, t) in enumerate(ABSENT, 1)]

    # Вид, которого в архиве меньше квоты, добираем остальными: размер набора
    # важнее ровной доли, иначе набор молча выходит меньше заказанного.
    have = {c["id"] for c in picked}
    for case in cases:
        if len(picked) >= target:
            break
        if case["id"] not in have:
            have.add(case["id"])
            picked.append(case)
    if len(picked) < target:
        raise SystemExit("архив дал только %d случаев из %d — опусти --target"
                         % (len(picked), target))
    return picked[:target], marked, episodes


def main():
    ap = argparse.ArgumentParser(description="Золотой набор из архива разговоров")
    ap.add_argument("--split", default=SPLIT)
    ap.add_argument("--target", type=int, default=TARGET)
    ap.add_argument("--out", default="eval-cases.json")
    ap.add_argument("--fixture", default="eval-fixture.json")
    args = ap.parse_args()

    cases, marked, episodes = build(args.split, args.target)
    Path(args.out).write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    used = {c["id"].split("-", 1)[1] for c in cases if c["kind"] != "absence"}
    rows = [m for m in fixture(marked, episodes)][:400]
    Path(args.fixture).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")

    from collections import Counter
    kinds = Counter(c["kind"] for c in cases)
    pos = sum(1 for c in cases if c["repeated"])
    print("случаев %d, из них положительных по разметке %d" % (len(cases), pos))
    print("по видам:", ", ".join("%s %d" % kv for kv in sorted(kinds.items())))
    print("записей в наполнении: %d -> %s" % (len(rows), args.fixture))


if __name__ == "__main__":
    main()
