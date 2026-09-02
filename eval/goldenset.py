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

У набора две половины, они работают в разных прогонах:

  сценарий  реплики человека из тех эпизодов, откуда взяты факты. Первый
            прогон отыгрывает их обычным разговором. Запишется что-нибудь
            или нет — не задаётся, это и есть предмет замера.
  случаи    вопросы с ожидаемым ответом. Второй прогон задаёт их с чистой
            сессии и смотрит, помогла память или нет.

Запуск: python3 -m eval.goldenset
"""
import argparse, hashlib, json, re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from domain import lifespan
from pipeline import understand as u
from infra.scrub import redact

SPLIT = "2026-08-09"          # та же граница, что в holdout.py и в замерах
TARGET = 100                  # размер набора

# Версия набора. Растёт, когда меняется смысл собранного, а не форма файла:
# цифра, снятая на наборе версии N, сравнима только с цифрой того же N.
#   1 — подпись факта о правке это имя проекта; вопрос про файлы спрашивал
#       подписью, то есть на прежней сборке — путём файла вместо проекта;
#   2 — подпись это путь файла, проект берётся из эпизода;
#   3 — факты берутся тем же извлечением, что и в конвейере: сперва разметка
#       модели, шаблоны только там, где разметки нет. На версии 2 набор
#       спрашивал про факты, которых конвейер не пишет вовсе, и такие случаи
#       были непроходимы по построению.
VERSION = 3

# Чем подписан факт в этой версии. Лежит в конверте, чтобы расхождение было
# видно по заголовку файла, а не вычитывалось из сотни случаев.
IDENTITY = "file-path"

KINDS = ("cases", "fixture", "script")


class SetVersionError(RuntimeError):
    """Набор собран не этим кодом. Читать его — мерить не то, что думаешь."""
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
            # Проект кладём рядом с фактом: у факта о правке подпись это путь
            # файла, а не имя проекта, и спросить проект больше не у кого.
            project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
            # Тем же извлечением, что и конвейер: сперва разметка модели, потом
            # шаблоны. Спрашивай набор по шаблонам — он спрашивал бы про факты,
            # которых в хранилище нет, и мерил бы недостижимое.
            for fact, key in u.marked_or_guessed(ep)[0]:
                occ[key].append({"eid": eid, "day": day, "fact": fact,
                                 "project": project})
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


def sources(rec):
    """Эпизоды, из которых факт взят. По ним собирается сценарий разговора."""
    return [list(i["eid"]) for i in rec["items"]]


def case_fact(key, rec):
    """Спрашиваем про проект, ждём путь файла, который в нём правили."""
    item = rec["items"][-1]
    fact = item["fact"]
    path = anonymize(file_of(fact[3]))
    if not path or "/" not in path:
        return None
    return {
        "id": "fact-%s" % short(key),
        "kind": "fact",
        "query": "Какие файлы правились в проекте %s?" % anonymize(item["project"]),
        "expect": [Path(path).name],
        "forbid": [],
        "repeated": rec["repeated"],
        "occurrences": rec["n"],
        "source": sources(rec),
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
        "source": sources(rec),
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
        "source": sources(rec),
    }


def case_link(eid, ep, marked):
    """Два факта из одного эпизода: проверяем, что память отдаёт связанное."""
    facts = [fact for fact, key in u.marked_or_guessed(ep)[0] if key in marked]
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
        "source": [list(eid)],
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
        "source": [list(eid)],
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
        "source": [],
    }


def script(cases, episodes):
    """Первая половина набора: реплики человека для первого прогона.

    Берутся из тех же эпизодов, откуда пришли факты, поэтому сценарий и мишень
    сходятся: второй прогон спрашивает ровно про то, что первый мог записать.
    Порядок — по времени, иначе разговор про файл случается раньше, чем файл
    в нём появился, и связи внутри задачи не складываются.
    """
    want = []
    for case in cases:
        for eid in case["source"]:
            want.append((tuple(eid), case["id"]))

    by_ep = defaultdict(list)
    for eid, cid in want:
        by_ep[eid].append(cid)

    turns = []
    for eid in sorted(by_ep, key=lambda e: (episodes[e]["started_at"] or "", e)):
        ep = episodes[eid]
        request = anonymize(" ".join(ep["request"].split()))
        if not request:
            continue
        turns.append({
            "turn": len(turns) + 1,
            "session": eid[0],
            "episode": eid[1],
            "project": anonymize(Path(ep["cwd"]).name if ep["cwd"] else "unknown"),
            "working_directory": anonymize(ep["cwd"] or ""),
            "git_branch": anonymize(ep["branch"] or ""),
            "started_at": ep["started_at"] or "",
            "request": request[:1500],
            "files": [anonymize(f) for f in ep["files"][:15]],
            "commands": [anonymize(c) for c in ep["commands"][:10]],
            "outcome": u.outcome_of(ep),
            "feeds": sorted(set(by_ep[eid])),
        })
    return turns


def fixture(marked, episodes, cases=(), limit=None):
    """Записи схемы, которыми набор наполняется, если хранилище пустое.

    Наполняем тем, что случаи спрашивают, а не одним удобным видом. Факты
    отвечают на fact, preference и external_resource; обстановка эпизода —
    проект, ветка, исход — живёт на Episode, и без него десять случаев из ста
    не могли пройти в принципе: прогон упирался в 89 из 100 по построению, и
    разница половин мерялась об этот потолок.
    """
    from domain import models
    facts = []
    for key, rec in sorted(marked.items(), key=lambda kv: repr(kv[0])):
        fact = rec["items"][-1]["fact"]
        facts.append(models.Fact(fact_type=fact[0], subject=anonymize(fact[1]),
                                 scope=fact[2], content=anonymize(fact[3])).mutation())
    # Эпизоды кладём только те, на которые ссылаются случаи: весь архив сюда
    # не нужен, а без ссылки эпизод в наборе никто не спросит.
    wanted, seen = [], set()
    for case in cases:
        for source in case.get("source") or []:
            eid = tuple(source)
            if eid in seen or eid not in episodes:
                continue
            seen.add(eid)
            wanted.append(eid)
    out = facts[:limit] if limit else list(facts)
    # Потолок стрижёт факты, но не эпизоды: их и так ровно столько, сколько
    # спросили случаи, а срезав их, мы вернули бы непроходимый набор.
    for eid in wanted:
        ep = episodes[eid]
        project = anonymize(Path(ep["cwd"]).name if ep["cwd"] else "unknown")
        out.append(models.Episode(
            session_id=anonymize(ep["session_id"]) or "unknown",
            episode_number=ep["number"],
            title=anonymize(" ".join(ep["request"].split())[:120]),
            outcome=u.outcome_of(ep),
            project=project,
            git_branch=anonymize(ep["branch"]) or None,
            started_at=ep["started_at"] or None,
            ended_at=ep["ended_at"] or None).mutation())
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


def envelope(kind, items, **meta):
    """Конверт набора: версия, вид, размер, содержимое.

    Форма одна на все три файла. Разные заголовки у случаев, реплик и фикстуры
    означали бы три места, где проверяется версия, и три способа забыть её.
    """
    if kind not in KINDS:
        raise SetVersionError("неизвестный вид набора: %s, известны %s"
                              % (kind, ", ".join(KINDS)))
    built = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {"version": VERSION, "kind": kind, "identity": IDENTITY,
            "built_at": built, "as_of": built, "count": len(items)}
    body.update(meta)
    body["items"] = list(items)
    return body


def dump(path, kind, items, **meta):
    """Набор в файл вместе с конвертом."""
    Path(path).write_text(
        json.dumps(envelope(kind, items, **meta), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def as_of(meta):
    """Момент, на который замер считает сроки. Часть набора, а не прогона.

    Версия набора уже говорит, каким кодом он собран. Момент говорит второе:
    на каком состоянии базы снята цифра. Без него забывание переливает факты
    в отложенные между двумя прогонами, и цифра едет сама по себе — своей
    разницы от хода времени в ней потом не отличить.

    Прежние наборы момента не носят, и отказывать им незачем: сборка — такой же
    снимок состояния, и она в конверте уже есть.
    """
    got = (meta.get("as_of") or meta.get("built_at") or "").strip()
    return lifespan.stamp(got) if got else ""


def load(path, kind=None):
    """Набор из файла. Чужой не отдаём вовсе: молчание тут дороже падения.

    Голый список — это набор прежней сборки. Он читался бы как свой и дал бы
    число, несравнимое ни с чем, поэтому отказ здесь такой же, как на чужой
    версии.
    """
    where = Path(path)
    body = json.loads(where.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or "version" not in body:
        raise SetVersionError(
            "%s собран прежней сборкой, без версии. Пересобрать: "
            "python3 -m eval.goldenset" % where.name)
    if body["version"] != VERSION:
        raise SetVersionError(
            "%s версии %s, а нужна %d. Пересобрать: python3 -m eval.goldenset"
            % (where.name, body["version"], VERSION))
    if kind is not None and body.get("kind") != kind:
        raise SetVersionError("%s это набор вида %r, а спрашивали %r"
                              % (where.name, body.get("kind"), kind))
    items = body.get("items") or []
    if body.get("count") != len(items):
        raise SetVersionError("%s: в конверте %s записей, в списке %d — файл оборван"
                              % (where.name, body.get("count"), len(items)))
    return body, items


def main():
    ap = argparse.ArgumentParser(description="Золотой набор из архива разговоров")
    ap.add_argument("--split", default=SPLIT)
    ap.add_argument("--target", type=int, default=TARGET)
    ap.add_argument("--out", default="eval-cases.json")
    ap.add_argument("--fixture", default="eval-fixture.json")
    ap.add_argument("--script", default="eval-script.json")
    args = ap.parse_args()

    cases, marked, episodes = build(args.split, args.target)
    dump(args.out, "cases", cases, split=args.split, target=args.target)
    rows = fixture(marked, episodes, cases, limit=400)
    dump(args.fixture, "fixture", rows, split=args.split)
    turns = script(cases, episodes)
    dump(args.script, "script", turns, split=args.split)

    from collections import Counter
    kinds = Counter(c["kind"] for c in cases)
    pos = sum(1 for c in cases if c["repeated"])
    print("набор версии %d, подпись факта: %s" % (VERSION, IDENTITY))
    print("случаев %d, из них положительных по разметке %d" % (len(cases), pos))
    print("по видам:", ", ".join("%s %d" % kv for kv in sorted(kinds.items())))
    fed = {c for t in turns for c in t["feeds"]}
    print("реплик в сценарии: %d -> %s" % (len(turns), args.script))
    print("случаев, которые сценарий может наполнить: %d из %d"
          % (len(fed), sum(1 for c in cases if c["kind"] != "absence")))
    print("записей в запасном наполнении: %d -> %s" % (len(rows), args.fixture))


if __name__ == "__main__":
    main()
