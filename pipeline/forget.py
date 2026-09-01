#!/usr/bin/env python3
"""Модуль забывания. Просроченное уходит из первой выдачи и остаётся целым.

Память умела запоминать, связывать, находить и отмечать пользу. Забывать она не
умела вовсе, и от этого выдача со временем только пухла: вчерашняя правда и
позапрошлогодняя лежали вперемешку и весили одинаково.

Правило простое и всё целиком здесь:

  переклад   факт, у которого вышел срок, перекладывается в `LapsedFact`.
             Не удаляется и не помечается флагом. Отдельное место убирает
             запись из выборки один раз, а флаг пришлось бы отсеивать порогом
             в каждом месте, где память кого-нибудь спрашивают.
  глубина    достать переложенное можно нарочно, глубоким чтением. Вопрос
             «что было верно тогда» остаётся отвечаемым.
  продление  обращение отодвигает срок, и делает это подсказка в момент
             вставки, а не этот проход, см. `suggest.renew`.

Проход зовётся на конце хода, следом за пониманием и связями: сперва в базу
ложится новое, потом из неё выбывает просроченное. Своей книжки учёта у него
нет и не надо — он смотрит на срок в самой записи, а не на то, где остановился
в прошлый раз.

Запуск:

    python3 -m pipeline.forget                 холостой: сколько бы выбыло
    python3 -m pipeline.forget --send          переклад
    python3 -m pipeline.forget --recall "..."  глубокое чтение
"""
import argparse, contextlib, json

from domain import lifespan
from infra import locks
from storage import port

# Замок общий с остальными проходами по архиву: база одна, и писать в неё
# вдвоём нельзя. Переклад — это INSERT и DELETE подряд под одной фиксацией,
# и встреться он в SQLite со вторым писателем, обрыв стоил бы записи.
LOCK = locks.PASS


def sweep(door=None, now=None, dry=False, lock=None):
    """Переложить просроченное в отложенное. Отдаёт, сколько и смог ли.

    Дверь спрашиваем про умение, а не про имя: у сетевого пути выборки по сроку
    нет, и забывание не имеет права ронять на нём ход. Не умеет — говорит об
    этом числом и признаком, а не исключением наружу.

    Замок неблокирующий и общий, как у остальных проходов. Занято значит «уже
    пишут»: ждать нечего, конец следующего хода позовёт нас снова.
    """
    door = door or port.door()
    at = now or lifespan.now()
    got = {"moved": 0, "able": False, "busy": False, "now": at}
    move = getattr(door, "lapse", None)
    if move is None:
        return got
    held = locks.alone(lock) if lock else contextlib.nullcontext(True)
    with held as mine:
        if not mine:
            got["busy"] = True
            return got
        try:
            got["moved"] = move(at, dry=dry)
        except AttributeError:
            return got
        got["able"] = True
    return got


def recall(query, door=None, limit=10):
    """Глубокое чтение: к найденному добавляется отложенное.

    Отдаёт записи, а не строку. Тем, кто спрашивает глубоко, пересказ не нужен:
    вопрос «что было верно тогда» задают, уже зная, что ищут.
    """
    door = door or port.door()
    step = getattr(door, "deep", None)
    if step is None:
        return []
    try:
        return step(query, limit=limit)
    except AttributeError:
        return []


def main():
    ap = argparse.ArgumentParser(description="Забывание: срок факта и отложенное")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="переложить; без него — только счёт")
    ap.add_argument("--now", help="считать сроки на этот момент, а не на сейчас")
    ap.add_argument("--recall", help="глубокое чтение: достать отложенное")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    if args.recall:
        found = recall(args.recall, limit=args.limit)
        print(json.dumps(found, ensure_ascii=False, indent=2) if found else "")
        return

    got = sweep(now=lifespan.stamp(args.now) if args.now else None, dry=args.dry,
                lock=LOCK)
    if got["busy"]:
        print("замок занят: разбирают и без нас, этот заход не нужен")
        return
    if not got["able"]:
        print("путь наружу забывать не умеет, этот заход не нужен")
        return
    print("режим %s (%d дней), срок на %s, %s %d"
          % (lifespan.mode(), lifespan.days(), got["now"],
             "выбыло бы" if args.dry else "переложено", got["moved"]))


if __name__ == "__main__":
    main()
