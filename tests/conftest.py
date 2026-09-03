"""Развязка до сбора проверок. Ленту переводит в сторону загрузка пакета.

pytest грузит conftest раньше самих модулей проверок, а импорт пакета делает
всю работу — см. `tests/__init__.py`. Держим здесь только вызов: два места с
одним и тем же путём разъедутся молча, и живая лента снова начнёт расти на
прогоне.

Второе дело conftest — ворота живого стенда. Проверки, поднимающие настоящий
прогон, помечены `slow` и по умолчанию пропускаются: с ними батарея идёт
больше десяти минут, без них — меньше минуты. Включаются ключом:

    XMEM_SLOW=1 python3 -m pytest -q -m slow

Правило самой пометки держит `tests/test_suite_shape.py`: зовёшь `live.run` —
носи пометку, иначе быстрый прогон молча станет долгим.
"""
import pytest

import tests  # noqa: F401
from tests import slow


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: поднимает живой стенд; собирается только при %s=1" % slow.KEY)


def pytest_collection_modifyitems(config, items):
    if slow.wanted():
        return
    skip = pytest.mark.skip(reason="живой стенд: включается %s=1" % slow.KEY)
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
