#!/usr/bin/env python3
"""Лента обращений: что подсказка показала, чем это кончилось, где промолчала.

Отметка исхода жила полем на записи о вставке и хранила последнее значение.
Один и тот же факт вбрасывается многократно, и «помог трижды из десяти» — это
не то же самое, что «помог в последний раз», а веса и сроки должны считаться
именно по истории. Поле её стирало при каждой отметке.

Поэтому рядом с записью встаёт лента: файл, куда дописывается строка на каждое
событие. Записи о вставке лента не заменяет, связи вставки с разговором и
источниками остаются как были, см. ADR 0010.

Почему файл, а не запись схемы. Читать из хранилища мы умеем только поиском
словами, а свести «показан N раз» поиск не может — нужен полный обход. Половина
причин молчания это поломка самого хранилища, и строка о ней туда не ляжет
ровно тогда, когда нужна. И лента пишется на каждом сообщении человека, в том
числе когда память молчит: поход в хранилище сделал бы молчание дороже
подсказки.

Разбор ленты: python3 -m domain.ledger
"""
import json, os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.environ.get("XMEM_LEDGER") or
           Path.home() / ".local" / "state" / "memory-encoder" / "ledger.jsonl")

# Что случилось. Вброс и молчание — исходы захода, их ровно по одному на заход;
# показ и польза висят на вбросе и приходят пачкой.
EVENTS = ("injected", "shown", "helped", "silent")

# Почему память промолчала. Имя обязательно: без него разрыв между «нашли» и
# «отдали» не разобрать, и все молчания сливаются в одно «не сработало».
REASONS = ("not_found",        # хранилище не отдало ничего
           "incidental",       # всё найденное оказалось ложной находкой
           "below_threshold",  # не прошло порог
           "over_budget",      # не влезло в потолок выдачи
           "backend_error",    # отказ носителя
           "overdue",          # вышел срок горячего пути
           "disabled",         # память выключена рубильником
           "pipeline_error")   # упал сам конвейер, а не носитель

# Ответ про пользу трёхзначный. «Ответа нет» пишется наравне с остальными: свали
# его в отрицательный — и доля пользы поедет вниз на всех фактах сразу, просто
# потому что агент промолчал.
VERDICTS = ("yes", "no", "unknown")

# Чем сняли ответ. Три способа из решения оператора: догадка по архиву, вопрос
# в конце хода, вопрос вместе с вбросом. Проложен пока первый. Способы считаются
# порознь и не складываются: их расхождение само по себе данные.
SOURCES = ("transcript", "turn_end", "inline")

VERDICT_OF = {True: "yes", False: "no", None: "unknown"}

# Сколько текста вопроса кладём в строку. Лента растёт быстрее самой памяти,
# и целый вопрос в каждой строке — то, чем она растёт быстрее всего.
QUERY_CHARS = 200


class LedgerError(ValueError):
    """Событие не сходится со словарём ленты. Лучше упасть, чем писать мусор."""


def now():
    return datetime.now(timezone.utc).isoformat()


def verdict_of(helped):
    """Отметка записи о вставке — в трёхзначный ответ ленты.

    `None` это «ответа нет», а не «не помогло»: у записи оба случая выглядят
    одинаково пустым полем, и различить их можно только здесь.
    """
    if helped not in VERDICT_OF:
        raise LedgerError("непонятная отметка пользы: %r" % (helped,))
    return VERDICT_OF[helped]


def append(event, log=None, **fields):
    """Дописать строку. Только дописать: правок у ленты нет.

    Открываем на дозапись и закрываем сразу же. Держать файл открытым между
    ходами нельзя: хук живёт один вызов, а обрыв на середине хода оставил бы
    полстроки — её потом пропустит разбор, но потеряется целое событие.
    """
    if event not in EVENTS:
        raise LedgerError("нет такого события ленты: %r" % (event,))
    row = dict(fields, at=fields.get("at") or now(), event=event)
    where = Path(log) if log else LOG
    where.parent.mkdir(parents=True, exist_ok=True)
    with where.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def rows(log=None):
    """Лента целиком, в порядке записи. Битые строки пропускаем.

    Строка без известного события тоже пропускается: лента лежит в том же
    каталоге, что и прочие журналы, и чужая строка не должна считаться событием.
    """
    where = Path(log) if log else LOG
    if not where.exists():
        return []
    out = []
    for line in where.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") in EVENTS:
            out.append(row)
    return out


def injected(session_id, injected_at, keys=(), query="", log=None, at=None):
    """Вброс и всё, что в нём показали: строка на вставку плюс строка на запись.

    Показ ключуется парой «вставка плюс запись», а не одной вставкой: без ключа
    записи ответ про пользу повиснет между показанными фактами и достанется не
    тому. Ключ ставится здесь, в момент вброса.
    """
    key = {"session_id": session_id or "unknown", "injected_at": injected_at}
    # Список сразу: ключи считаются и перебираются, а генератор после счёта
    # опустеет — строка о вбросе объявит N записей, а показов не будет ни одного.
    keys = list(keys)
    out = [append("injected", log=log, at=at, query=(query or "")[:QUERY_CHARS],
                  items=len(keys), **key)]
    for one in keys:
        out.append(append("shown", log=log, at=at, key=one, **key))
    return out


def helped(session_id, injected_at, verdict, source="transcript", log=None, at=None):
    """Ответ про пользу по одной вставке, снятый названным способом."""
    if verdict not in VERDICTS:
        raise LedgerError("нет такого ответа: %r, допустимо %s"
                          % (verdict, ", ".join(VERDICTS)))
    if source not in SOURCES:
        raise LedgerError("нет такого способа съёма: %r, допустимо %s"
                          % (source, ", ".join(SOURCES)))
    return append("helped", log=log, at=at, verdict=verdict, source=source,
                  session_id=session_id or "unknown", injected_at=injected_at)


def silence(reason, session_id=None, query="", log=None, at=None, note=None):
    """Молчание с названной причиной. Пишется наравне с вбросом, а не пропускается.

    Молчание — это и есть ответ на вопрос, почему память не сработала. Из ста
    эталонных вопросов нужное находится в 32, а до агента доходит 25; без имён
    причин этот разрыв не разобрать.

    `note` — подробность к причине, когда имени мало: отказ носителя бывает
    разный, и без имени ошибки своя же поломка прячется под чужой.
    """
    if reason not in REASONS:
        raise LedgerError("нет такой причины молчания: %r, допустимо %s"
                          % (reason, ", ".join(REASONS)))
    extra = {"note": note[:QUERY_CHARS]} if note else {}
    return append("silent", log=log, at=at, reason=reason,
                  session_id=session_id or "unknown",
                  query=(query or "")[:QUERY_CHARS], **extra)


def _key_of(row):
    return (row.get("session_id") or "unknown", row.get("injected_at") or "")


def verdicts(rows_, source="transcript"):
    """Текущий ответ по каждой вставке для одного способа съёма.

    Берём последний: у одного способа на одну вставку один ответ, а прежние
    остаются в ленте историей правки. Складывать их значило бы считать
    исправленный ответ дважды.
    """
    out = {}
    for row in rows_:
        if row.get("event") == "helped" and row.get("source") == source:
            out[_key_of(row)] = row.get("verdict")
    return out


def tally(rows_, source="transcript"):
    """По каждой записи: сколько раз показана и чем это кончилось.

    Ответы считаются по одному способу съёма, а не по всем сразу: у трёх
    способов на один показ вышло бы три ответа, и «помог M из N» перестало бы
    сходиться. Расхождение способов между собой — отдельный вопрос и отдельный
    счёт, складывать их нельзя.
    """
    shown = defaultdict(int)
    inside = defaultdict(list)
    for row in rows_:
        if row.get("event") != "shown" or not row.get("key"):
            continue
        shown[row["key"]] += 1
        inside[_key_of(row)].append(row["key"])
    out = {key: {"shown": n, "helped": 0, "not_helped": 0, "unknown": 0}
           for key, n in shown.items()}
    field = {"yes": "helped", "no": "not_helped", "unknown": "unknown"}
    for injection, verdict in verdicts(rows_, source).items():
        if verdict not in field:
            continue
        for key in inside.get(injection, ()):
            out[key][field[verdict]] += 1
    return out


def share(rows_, source="transcript"):
    """Доля пользы по каждой записи. Считается только там, где ответ был.

    Молчание агента в знаменатель не идёт. Пойди оно туда — доля поехала бы
    вниз на всех фактах сразу, а мерили бы мы не пользу, а отзывчивость агента.
    Ответов нет вовсе — None, а не ноль: неизвестное это не отрицательное.
    """
    out = {}
    for key, got in tally(rows_, source).items():
        answered = got["helped"] + got["not_helped"]
        out[key] = round(got["helped"] / float(answered), 4) if answered else None
    return out


def silences(rows_):
    """Сколько раз память промолчала и по какой причине."""
    out = defaultdict(int)
    for row in rows_:
        if row.get("event") == "silent" and row.get("reason") in REASONS:
            out[row["reason"]] += 1
    return dict(out)


def report(log=None):
    """Сводка одной строкой на запись плюс разбивка молчаний."""
    found = rows(log)
    lines = ["строк в ленте: %d" % len(found)]
    mute = silences(found)
    if mute:
        lines.append("молчаний: %d — %s"
                     % (sum(mute.values()),
                        ", ".join("%s %d" % pair for pair in sorted(mute.items()))))
    counted = tally(found)
    for key, got in sorted(counted.items(), key=lambda p: -p[1]["shown"]):
        lines.append("%s: показан %d, помог %d, не помог %d, без ответа %d"
                     % (key, got["shown"], got["helped"], got["not_helped"],
                        got["unknown"]))
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Разбор ленты обращений")
    ap.add_argument("--log", help="файл ленты, по умолчанию %s" % LOG)
    print(report(ap.parse_args().log))


if __name__ == "__main__":
    main()
