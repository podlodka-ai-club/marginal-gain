#!/usr/bin/env python3
"""Настройка снаружи кода: одно имя, по которому выбирается схема разметки.

Порядок силы тот же, что у хуков: окружение сильнее файла, файл сильнее
умолчания. Правка кода не нужна ни для одной половины сравнения — так же, как
не нужна для переключения пути наружу (`XMEM_BACKEND`, см. `hooks/common.sh`).

Здесь нет ни текста просьбы, ни знания о внешнем формате: и то и другое живёт
в схеме, парой. Настройка называет схему, а не описывает её — иначе просьба и
маппер разъезжаются, и первым же следствием будет блок, который никто не умеет
разобрать.
"""
import os
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "state" / "memory-encoder"

# Схема разметки по умолчанию. Имя должно быть в реестре domain/marks.py.
DEFAULT_MARKS = "xmd1"


def setting(env, file_name, default):
    """Значение настройки: окружение, потом файл, потом умолчание.

    Файл читаем целиком по первой строке и без пробелов: писать его будут
    руками через `echo`, и хвостовой перевод строки не должен делать имя
    схемы неизвестным.
    """
    got = (os.environ.get(env) or "").strip()
    if got:
        return got
    target = STATE_DIR / file_name
    try:
        got = target.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        got = ""
    return got or default


def marks():
    """Имя схемы разметки: `XMEM_MARKS`, файл `marks`, иначе умолчание."""
    return setting("XMEM_MARKS", "marks", DEFAULT_MARKS)
