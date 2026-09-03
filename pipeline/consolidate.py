#!/usr/bin/env python3
"""Офлайн-свёртка. Группа записей про одно и то же становится одной.

Память копила и уже умела забывать по сроку. Чего она не умела — сворачивать:
три записи про одно и то же оставались тремя и втроём же занимали места в
выдаче. Свёртка отличается от забывания тем, что знание не теряется, а
обобщается, и потому обратимость здесь обязательна.

Правило целиком:

  группа     записи одного вида, охвата и проекта с посимвольно одинаковым
             содержанием. Правило узкое нарочно: слияние по смыслу у нас не
             выбрано, это отдельный незакрытый ресёрч. См. `domain.folding`.
  замена     берётся из самой группы — та, что прожила бы дольше. Подпись
             `fact_type|subject|scope` при этом не меняется, и граф остаётся
             при своих узлах.
  отставка   остальные перекладываются в `LapsedFact`, туда же, куда забывание
             кладёт просроченное. Не удаляются. Повод различает пометка
             `merged_into`: пусто — вышел срок, заполнено — свернули.
  обратимость  на замене лежит `merged_from` — из чего она собрана. Исходные
             читаются глубоким чтением, а `--undo` возвращает их обратно.

Проход идёт последним на конце хода, следом за забыванием: сперва в базу
ложится новое и из неё выбывает просроченное, и только потом оставшееся
сворачивается. Замок общий с остальными проходами по архиву — база одна, и
писателей в ней не может быть двое. Занято значит «уже пишут»: уходим, конец
следующего хода позовёт снова.

Запуск:

    python3 -m pipeline.consolidate                 холостой: сколько бы свернулось
    python3 -m pipeline.consolidate --send          свёртка
    python3 -m pipeline.consolidate --from "ключ"   из чего собрана замена
    python3 -m pipeline.consolidate --undo "ключ"   развернуть обратно
"""
import argparse
import contextlib
import json

from domain import lifespan
from infra import locks
from storage import port

# Замок именно общий, а не свой. Дело не в книжке учёта — её у свёртки нет, —
# а в том, что база одна: два писателя разом встретились бы в SQLite.
LOCK = locks.PASS


def fold(door=None, now=None, dry=False, lock=None):
    """Свернуть дубли. Отдаёт, сколько уехало в отставку, и смог ли.

    Дверь спрашиваем про умение, а не про имя: у сетевого пути выборки по
    содержанию нет, и свёртка не имеет права ронять на нём ход. Не умеет —
    говорит об этом числом и признаком, а не исключением наружу.
    """
    door = door or port.door()
    at = now or lifespan.now()
    got = {"folded": 0, "able": False, "busy": False, "now": at}
    step = getattr(door, "fold", None)
    if step is None:
        return got
    held = locks.alone(lock) if lock else contextlib.nullcontext(True)
    with held as mine:
        if not mine:
            got["busy"] = True
            return got
        try:
            got["folded"] = step(at, dry=dry)
        except AttributeError:
            return got
        got["able"] = True
    return got


def sources(identity, door=None):
    """Из чего собрана замена. Записи, а не пересказ: сравнивают их сами."""
    door = door or port.door()
    step = getattr(door, "folded", None)
    if step is None:
        return []
    try:
        return step(identity)
    except AttributeError:
        return []


def unfold(identity, door=None):
    """Развернуть свёртку обратно. Отдаёт, сколько записей вернулось."""
    door = door or port.door()
    step = getattr(door, "unfold", None)
    if step is None:
        return 0
    try:
        return step(identity)
    except AttributeError:
        return 0


def main():
    ap = argparse.ArgumentParser(description="Свёртка: дубли сходятся в одну запись")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="свернуть; без него — только счёт")
    ap.add_argument("--now", help="считать отставку на этот момент, а не на сейчас")
    ap.add_argument("--from", dest="source", help="из чего собрана замена")
    ap.add_argument("--undo", help="развернуть свёртку по подписи замены")
    args = ap.parse_args()

    if args.source:
        got = sources(args.source)
        print(json.dumps(got, ensure_ascii=False, indent=2) if got else "")
        return
    if args.undo:
        print("вернулось записей %d" % unfold(args.undo))
        return

    got = fold(now=lifespan.stamp(args.now) if args.now else None, dry=args.dry,
               lock=LOCK)
    if got["busy"]:
        print("замок занят: разбирают и без нас, этот заход не нужен")
        return
    if not got["able"]:
        print("путь наружу сворачивать не умеет, этот заход не нужен")
        return
    print("на %s %s %d" % (got["now"], "свернулось бы" if args.dry else "свёрнуто",
                           got["folded"]))


if __name__ == "__main__":
    main()
