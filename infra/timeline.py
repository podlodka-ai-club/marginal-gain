#!/usr/bin/env python3
"""Время: разбор отметки транскрипта и день недели. Один на весь проект.

Ниже некуда — ни от чего не зависит. Лежит здесь, а не в archive, потому что
меру факта (domain) свежесть тоже интересует, а domain ниже archive и тянуть
его наверх нельзя.

Разборов было два, save.when и understand.parse_time, и расходились они молча.
"""
from datetime import datetime

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_time(stamp):
    """Отметка времени в datetime, или None если не разобралась."""
    try:
        return datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def when(stamp):
    """Час и день недели из отметки. Нужны схеме для поиска по времени."""
    moment = parse_time(stamp) if stamp else None
    if moment is None:
        return None, None
    return moment.hour, DAYS[moment.weekday()]
