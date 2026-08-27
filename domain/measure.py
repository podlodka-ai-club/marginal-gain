#!/usr/bin/env python3
"""Мера факта: насколько ему верить. Оценка от 0 до 1.

Три доли: повторяемость, охват проектов, свежесть. Считается по свёрнутому
узлу и ничего не знает ни про архив, ни про хранилище — только про запись
вида {"n": ..., "projects": {...}, "last": ...}.

Признаки (features) считаются рядом и в меру не входят: слой принятия решения
поверх них — отдельная работа, ADR 0002.

Доли и веса пока зашиты константами. Это известная негибкость: каждая правка
меры правит тело функции. Открывать её реестром, как признаки и извлекатель,
имеет смысл тогда, когда долей станет больше трёх.
"""
from infra import telemetry
from infra.timeline import parse_time

# Потолки: выше них добавка перестаёт расти. Повторяемость упирается в десять
# вхождений, охват — в три проекта, свежесть — в тридцать дней.
REPEAT_CAP = 10
SPREAD_CAP = 3
FRESH_DAYS = 30

# Веса долей. Сумма единица, иначе оценка выйдет за 0…1.
WEIGHTS = {"repeat": 0.5, "spread": 0.2, "fresh": 0.3}


@telemetry.traced("weigh_fact", lambda arg, out: {
    "occurrences": arg["rec"]["n"], "projects": len(arg["rec"]["projects"]),
    "score": round(out, 3)})
def score_of(rec, newest):
    """Оценка от 0 до 1. Три доли: повторяемость, охват проектов, свежесть."""
    repeat = min(rec["n"], REPEAT_CAP) / float(REPEAT_CAP)
    spread = min(len(rec["projects"]), SPREAD_CAP) / float(SPREAD_CAP)
    fresh = 0.0
    if rec["last"] and newest:
        a, b = parse_time(rec["last"]), parse_time(newest)
        if a and b:
            days = (b - a).days
            fresh = max(0.0, 1.0 - days / float(FRESH_DAYS))
    return round(WEIGHTS["repeat"] * repeat + WEIGHTS["spread"] * spread
                 + WEIGHTS["fresh"] * fresh, 3)
