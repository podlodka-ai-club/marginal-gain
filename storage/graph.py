#!/usr/bin/env python3
"""Шаг по связям для того пути, у которого есть только читатель.

Локальная база ходит по графу сама: у неё есть SQL. У сети его нет, зато есть
чтение с режимом `raw-tables` — сырой результат запроса, а не пересказ словами
(см. research/research-3-xmemory-api.md). Этого хватает: связь адресует факт
строкой `fact_type|subject|scope`, и обход сводится к двум простым выборкам.

Здесь только шаг. Ни `api`, ни `sdk` он не знает и знать не должен: оба пути
отличаются тем, кто держит соединение, а вопрос и разбор ответа у них общие.
Разойдись они — выдача поехала бы в зависимости от того, каким клиентом сегодня
подпёрта дверь, а это ровно то, ради чего дверь и заводили.

Вопроса два, а не один с соединением. Каждый — плоская выборка по списку
значений: такую читатель сочиняет однозначно, а join через таблицу связей это
ещё одна догадка модели на горячем пути, и промах в ней виден не был бы никак.

Затухание и правило одного шага живут не здесь, а в `pipeline.suggest`: они
общие для всех дверей, и лежи они у пути наружу — каждый путь считал бы своё.
"""
import json

from domain import models

# Режим чтения: нам нужен сырой результат, пересказ разобрать нельзя.
RAW = "raw-tables"

# Колонки, без которых ответ не ответ. Отдал читатель что-то другое — значит
# вопрос он понял по-своему, и выдумывать за него нечего.
LINK_COLUMNS = ("source_key", "target_key", "weight")
FACT_COLUMNS = ("fact_type", "subject", "scope")

LINKS_QUESTION = (
    "Верни строки таблицы Association, у которых source_key или target_key "
    "равны одному из значений: %s. "
    "Нужны колонки source_key, target_key, weight, тяжёлые первыми.")

FACTS_QUESTION = (
    "Верни строки таблицы Fact, у которых fact_type, subject и scope, "
    "соединённые знаком | в таком порядке, равны одному из значений: %s. "
    "Нужны колонки fact_type, subject, scope, content, project.")


def _quoted(values):
    """Значения в вопрос. Кавычку внутри удваиваем, как это делает SQL.

    Тема факта — свободный текст: у размеченного факта её пишет модель, и
    апостроф в ней вполне реален. Без удвоения такой ключ рвал бы вопрос, а
    сосед пропадал бы молча.
    """
    return ", ".join("'%s'" % str(value).replace("'", "''") for value in values)


def _rows(answer, wanted):
    """Ответ в режиме raw-tables — в список словарей. Чужая форма — пусто.

    Читатель волен ответить прозой, пустотой или таблицей не о том: он сочиняет
    SQL по словам вопроса. Разбор обязан это пережить молча — обход добавка, и
    его промах не имеет права ронять подсказку.
    """
    body = answer
    if isinstance(body, str):
        text = body.strip()
        if not text:
            return []
        try:
            body = json.loads(text)
        except ValueError:
            return []
    if not isinstance(body, dict):
        return []
    columns = [c.get("name") if isinstance(c, dict) else c
               for c in body.get("columns") or []]
    if not set(wanted) <= set(columns):
        return []
    out = []
    for row in body.get("rows") or []:
        if not isinstance(row, (list, tuple)) or len(row) != len(columns):
            continue
        out.append(dict(zip(columns, row)))
    return out


def _weight(row):
    """Вес связи числом. Тип колонки задаёт читатель, доверять ему нельзя."""
    try:
        return float(row.get("weight") or 0)
    except (TypeError, ValueError):
        return 0.0


def neighbours(read, keys, limit=10):
    """Факты, связанные карточкой с любым из названных. Тяжёлые первыми.

    `read` — чтение того пути, который зовёт: подменяется он, а не имя модуля,
    поэтому шаг один на все пути наружу.

    Сам факт-источник в соседи не попадает: он уже в выдаче, и второй раз он бы
    только съел потолок. Связь ненаправленная — сосед берётся с того конца,
    которого не спрашивали.
    """
    keys = [key for key in keys if key]
    if not keys:
        return []          # спрашивать нечего: вопрос без значений вернёт всё
    links = _rows(read(LINKS_QUESTION % _quoted(keys), mode=RAW), LINK_COLUMNS)
    known, order = set(keys), []
    for row in sorted(links, key=_weight, reverse=True):
        source, target = row.get("source_key"), row.get("target_key")
        if source not in known and target not in known:
            continue       # читатель принёс чужую строку: она не о наших ключах
        other = target if source in known else source
        if not other or other in known:
            continue
        known.add(other)
        order.append((other, _weight(row)))
        if len(order) >= limit:
            break
    if not order:
        return []
    found = _rows(read(FACTS_QUESTION % _quoted(key for key, _ in order), mode=RAW),
                  FACT_COLUMNS)
    rows = {}
    for row in found:
        record = {name: value for name, value in row.items()
                  if value not in (None, "")}
        record["object_type"] = "Fact"
        # Подпись собираем тем же правилом, каким её собирает схема: разбор с
        # краёв и сборка через `identity`, иначе тема с разделителем разъедется.
        key = models.Fact(fact_type=row.get("fact_type") or "",
                          subject=row.get("subject") or "",
                          scope=row.get("scope") or "").identity()
        rows.setdefault(key, record)
    # Связь может пережить факт: конец есть, строки нет. Такого соседа
    # пропускаем — выдумывать запись не из чего.
    return [(rows[key], weight) for key, weight in order if key in rows]
