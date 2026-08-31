#!/usr/bin/env python3
"""Модуль ассоциаций. Ставит связи между фактами, найденными в одном архиве.

Схема описывает связь фактов давно, а карточек в хранилище было ноль: правило
порождения выбирали отдельным ресёрчем, и до его конца граф заполнять было
нечем. Правило первой версии — два повода из пяти, здесь оно исполняется, а не
выбирается заново:

  same_episode     два факта, названные в одном эпизоде. Потолок — восемь
                   фактов на эпизод: без него дюжина самых длинных эпизодов
                   даёт почти половину всех пар графа.
  error_then_fix   упёрлись, потом починили: факты эпизода, который упёрся,
                   и факты следующего эпизода того же разговора, если тот
                   дошёл до конца. Повод направленный и редкий — 74 карточки
                   на архив против 1246 у первого.

Отвергнут `same_project`: он давал бы 88% рёбер и ранжировал хуже случайного,
а как фильтр он и так лежит в поле `scope`. Отложены `adjacent_in_time` (на
уровне случайного) и `same_file`.

Проход идёт по всему архиву целиком и книжки учёта не ведёт. Вес карточки это
число наблюдений повода, и считать его по куску архива значит записать в
хранилище число, которое меньше правды. Повторный проход отдаёт то же самое:
ключ карточки — источник, цель и повод, и запись по нему идёт поверх.
"""
import argparse, itertools
from pathlib import Path

from archive.transcripts import TRANSCRIPTS, episodes_from_file
from domain import models
from pipeline import understand
from storage import port

# Сколько фактов эпизода участвует в поводе same_episode. Число из ресёрча:
# двенадцать самых длинных эпизодов архива без потолка дают 43% всех пар.
CEILING = 8

# Карточки уходят пачками: их тысячи, а вызов двери стоит дорого.
BATCH = 200


def identity(fact):
    """Подпись факта одной строкой — в этой форме её хранит Association."""
    return models.Fact(*fact).identity()


# Сколько фактов с каждой стороны берёт редкий повод. Меньше, чем у первого:
# «упёрлись и починили» это про суть хода, а не про всё, что в нём названо.
RARE_CEILING = 3


def about_work(fact):
    """Факт про работу над проектом, а не про человека или ссылку.

    Редкий повод — про то, как упёрлись и починили; предпочтение «отвечать
    коротко» и адрес документации оказывались в нём только потому, что попали
    в соседний эпизод. Первый прогон дал ровно такие пары: смысл повода уехал,
    а число осталось похожим на правду.
    """
    return fact[0] == "project_state"


def stumbled(ep):
    """Эпизод, который упёрся. Источник редкого повода.

    Смотрим на исход эпизода, а не на слова внутри факта. Слова «упирались в
    препятствие» пишет шаблон извлечения, и повод, опознающий факт по ним,
    работает только пока факты режет шаблон: на фактах от разметки модели он
    молча даёт ноль карточек — так и было в первом прогоне.
    """
    return understand.outcome_of(ep) == "blocked"


def recovered(ep):
    """Эпизод, который после этого дошёл до конца. Цель редкого повода."""
    return understand.outcome_of(ep) == "done"


def note(graph, key, ep):
    """Ещё одно наблюдение повода: вес растёт, края времени раздвигаются."""
    rec = graph.setdefault(key, {"weight": 0, "first": "", "last": ""})
    rec["weight"] += 1
    started, ended = ep["started_at"] or "", ep["ended_at"] or ""
    if started and (not rec["first"] or started < rec["first"]):
        rec["first"] = started
    if ended > rec["last"]:
        rec["last"] = ended
    return rec


def scan(files):
    """Поводы по всему архиву: (источник, цель, повод) -> вес и края времени.

    Порядок концов у симметричного повода канонический — по подписи факта.
    Иначе «А с Б» и «Б с А» становятся двумя карточками об одном и том же, и
    вес каждой вдвое меньше правды.
    """
    graph = {}
    for path in files:
        try:
            episodes = episodes_from_file(path)
        except OSError:
            # Архив живой: файл могли удалить между списком и чтением. Потерянный
            # повод это неточность графа, упавший проход — потерянный заход.
            continue
        pending = {}
        for ep in episodes:
            found = [fact for fact, _ in understand.marked_or_guessed(ep)[0]]
            top = []
            for fact in found:
                if identity(fact) not in [identity(x) for x in top]:
                    top.append(fact)
                if len(top) == CEILING:
                    break
            for one, two in itertools.combinations(top, 2):
                key = tuple(sorted((identity(one), identity(two))))
                note(graph, (key[0], key[1], "same_episode"), ep)

            talk = ep["session_id"] or "unknown"
            work = [fact for fact in found if about_work(fact)]
            if recovered(ep):
                for source in pending.get(talk, []):
                    for target in work[:RARE_CEILING]:
                        if identity(source) != identity(target):
                            note(graph, (identity(source), identity(target),
                                         "error_then_fix"), ep)
            # Эпизод, который упёрся, ждёт следующего эпизода того же разговора:
            # починка приходит после, а не в том же ходе. Ждёт ровно один шаг —
            # через эпизод это уже другая работа, а не та же самая.
            pending[talk] = work[:RARE_CEILING] if stumbled(ep) else []
    return graph


def card_of(key, rec):
    """Карточка связи по накопленному поводу."""
    source, target, cue = key
    return models.Association(source_key=source, target_key=target, cue=cue,
                              weight=float(rec["weight"]),
                              observed_at=rec["last"] or None,
                              first_seen_at=rec["first"] or None)


def fact_of(key):
    """Конец связи по подписи. Разбор общий, см. models.Fact.of_identity."""
    return models.Fact.of_identity(key)


def deliver(items, door):
    """Карточки и их концы — одним вызовом на пачку.

    Дверь без структурной записи графа не получает: связь это ключи, а не
    проза, и текстом её на той стороне не собрать.
    """
    if not hasattr(door, "write_objects"):
        return 0
    records, relations = [], []
    for key, rec in items:
        card = card_of(key, rec)
        records.append(card)
        relations.append(models.link("association_fact_link", association=card,
                                     source_fact=fact_of(key[0]),
                                     target_fact=fact_of(key[1])))
    door.write_objects(records, relations)
    return len(records)


def build(files, dry=True, door=None, limit=None):
    """Проход по архиву: найти поводы и записать карточки.

    Отдаёт, сколько карточек нашлось и сколько уехало в хранилище: расхождение
    между этими числами и есть то, что молча теряется.
    """
    graph = scan(files)
    got = {"cards": len(graph), "written": 0}
    for cue in models.CUES:
        found = sum(1 for key in graph if key[2] == cue)
        if found:
            got[cue] = found
    if dry or not graph:
        return got
    door = door or port.door()
    items = sorted(graph.items())[:limit] if limit else sorted(graph.items())
    for start in range(0, len(items), BATCH):
        got["written"] += deliver(items[start:start + BATCH], door)
    return got


def main():
    ap = argparse.ArgumentParser(description="Связи между фактами по эпизодам архива")
    ap.add_argument("--send", dest="dry", action="store_false", default=True)
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--limit", type=int, help="потолок карточек за заход")
    args = ap.parse_args()

    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    if args.only:
        files = [f for f in files if args.only in str(f)]
    got = build(files, dry=args.dry, limit=args.limit)
    print("карточек %d, записано %d, режим %s"
          % (got["cards"], got["written"], "холостой" if args.dry else "запись"))
    for cue in models.CUES:
        if cue in got:
            print("  %-16s %d" % (cue, got[cue]))


if __name__ == "__main__":
    main()
