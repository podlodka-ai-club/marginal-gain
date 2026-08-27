#!/usr/bin/env python3
"""Адаптер локальной базы. Тот же интерфейс, что у сетевых: подмена без правок.

Отличие одно и оно принципиальное: у сети есть читатель, который отвечает
словами, у базы его нет. Поэтому чтение возвращает найденные записи как есть,
а не пересказ. Подсказке этого хватает: она и так разбирает ответ на куски и
сама решает, что показать.

Текстовая запись здесь разбирается своими силами. Экстрактора нет, зато форматы
`understand.render_fact`, `render_episode` и `suggest.note_injection` пишем мы
сами, и они детерминированы. Что не разобралось, ложится в `raw_text`: потерю
входа надо видеть, а не угадывать по недостающим строкам.
"""
import dataclasses, json, re, threading
from datetime import datetime, timezone

from domain import models
from storage import db

_REPO = None
_LOCK = threading.Lock()

# «Episode 3 of session abc.» — заголовок несёт оба поля первичного ключа.
EPISODE_HEAD = re.compile(r"^Episode\s+(\d+)\s+of\s+session\s+(\S+?)\.?$")
FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# Любая строка вида «что-то: значение», в том числе по-русски. Поля схемы
# названы латиницей, поэтому всё прочее — пояснение для человека, вроде
# «Оценка уверенности: 0.90.». Продолжением значения такая строка не является.
ANY_FIELD = re.compile(r"^[^\s:][^:]{0,60}:\s")


class LocalError(RuntimeError):
    """Локальная база не может выполнить то, о чём её просят."""


def repository():
    global _REPO
    with _LOCK:
        if _REPO is None:
            _REPO = db.Repository()
        return _REPO


def close():
    global _REPO
    with _LOCK:
        repo, _REPO = _REPO, None
    if repo is not None:
        repo.close()


def parse_text(text):
    """Наш собственный формат обратно в запись схемы. Чужой текст не трогаем.

    Возвращает пару (объект схемы, поля) либо None, если заголовок не наш.
    Неизвестные ключи отбрасываются: в тексте живут и пояснения для человека,
    вроде «Подтверждений в архиве», которых в схеме нет.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    head = lines[0].strip()
    found = EPISODE_HEAD.match(head)
    if found:
        object_type = "Episode"
        values = {"episode_number": int(found.group(1)), "session_id": found.group(2)}
    elif head.rstrip(".") in models.OBJECTS:
        object_type, values = head.rstrip("."), {}
    else:
        return None
    known = models.OBJECTS[object_type]
    names = {f.name for f in dataclasses.fields(known)}
    # Значение поля бывает в несколько строк. Продолжение дописываем к
    # последнему известному полю: иначе `content` терял всё, кроме первой
    # строки, и терял молча — ни записи, ни следа в raw_text.
    last = None
    for line in lines[1:]:
        pair = FIELD.match(line.strip())
        if pair and pair.group(1) in names:
            last = pair.group(1)
            values[last] = pair.group(2).strip()
        elif ANY_FIELD.match(line.strip()):
            last = None          # пояснение для человека, а не поле схемы
        elif last:
            values[last] = ("%s\n%s" % (values[last], line.strip())).strip()
    return object_type, values


def _typed(object_type, values):
    """Из текста всё приходит строкой. Числа возвращаем числами."""
    known = {f.name: f.type for f in dataclasses.fields(models.OBJECTS[object_type])}
    out = {}
    for name, value in values.items():
        want = known.get(name)
        if isinstance(value, str) and want in (int, float):
            try:
                value = want(value)
            except ValueError:
                continue
        out[name] = value
    return out


def write_text(text, wait=True, timeout=None):
    """Разбираем известный формат, остальное складываем целиком."""
    parsed = parse_text(text)
    repo = repository()
    if parsed is None:
        return {"stored": "raw", "digest": repo.put_text(
            text, datetime.now(timezone.utc).isoformat())}
    object_type, values = parsed
    values = _typed(object_type, values)
    record = models.OBJECTS[object_type](**{k: v for k, v in values.items()})
    record.validate()
    repo.apply([record.mutation()])
    return {"stored": object_type, "key": record.key()}


def write_objects(mutations, timeout=None):
    """Структурная запись. Ровно тот же список мутаций, что уходит в сеть."""
    items = list(mutations)
    if not items:
        raise LocalError("структурная запись без единой мутации")
    return {"applied": repository().apply(items)}


def read(query, mode="single-answer", timeout=None):
    """Возвращает строку, как и сетевые адаптеры: вызывающий ждёт строку.

    Найденное отдаём списком записей. Своих слов не добавляем: пустой список
    это пустая строка, то есть молчание, а не «ничего не найдено» текстом —
    иначе фраза поехала бы в контекст агента как факт.
    """
    found = repository().search(query)
    return json.dumps(found, ensure_ascii=False) if found else ""


def schema(timeout=None):
    """Схема берётся из `models`, а не из сети: локально это тот же источник."""
    return {"objects": sorted(models.OBJECTS), "relations": sorted(models.RELATIONS),
            "path": str(db.path()), "version": len(db.MIGRATIONS)}
