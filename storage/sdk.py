#!/usr/bin/env python3
"""Адаптер поверх официального клиента xmemory для Python.

Отличие от HTTP-адаптера одно: клиент сам держит соединение, повторы и разбор
ответа. Интерфейс тот же, чтобы модули менялись местами без правок вызывающих.
Пакет ставится отдельно, поэтому импорт отложен: без него остальной код живёт.
"""
import json, os, threading

from storage import graph

# Пустая переменная в окружении значит «не задано», см. .env.example.
BASE = os.environ.get("XMEM_API_URL") or ""
TIMEOUT = float(os.environ.get("XMEM_TIMEOUT") or 180)

_CLIENT = None
_LOCK = threading.Lock()


class SdkError(RuntimeError):
    pass


def _instance():
    value = os.environ.get("XMEM_INSTANCE_ID", "")
    if not value:
        raise SdkError("не задан XMEM_INSTANCE_ID: укажи хранилище в окружении")
    return value


def _api():
    """Клиент создаётся один раз на процесс и переиспользуется.

    Замок нужен затем, что два потока иначе построят по клиенту, и один из них
    останется висеть с открытым пулом соединений, который никто не закроет.
    """
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            try:
                from xmemory import XmemoryClient
            except ImportError as exc:
                raise SdkError("клиент xmemory не установлен: pip install "
                               "git+https://github.com/xmemory-ai/xmemory_client_py.git") from exc
            key = os.environ.get("XMEM_API_KEY", "")
            if not key:
                raise SdkError("не задан XMEM_API_KEY: ключ доступа в коде хранить нельзя")
            _CLIENT = XmemoryClient(BASE or None, api_key=key, timeout=TIMEOUT)
        client = _CLIENT
    return client.instance(_instance())


def close():
    """Закрыть соединение. Нужно только долгоживущим процессам."""
    global _CLIENT
    with _LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None:
        client.close()


def write_text(text, wait=True, timeout=None):
    api = _api()
    if wait:
        return api.write(text, timeout=timeout)
    return api.write_async(text, timeout=timeout)


def write_objects(mutations, timeout=None):
    """Структурная запись: ключ задан явно, модель в записи не участвует."""
    items = list(mutations)
    if not items:
        raise SdkError("структурная запись без единой мутации")
    return _api().write(structured_mutations=items, timeout=timeout)


def read(query, mode="single-answer", timeout=None):
    """Возвращает текст ответа, как и консоль: вызывающий ждёт строку."""
    from xmemory import ReadMode
    out = _api().read(query, read_mode=ReadMode(mode), timeout=timeout)
    answer = out.reader_result
    if answer is None:
        return ""
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def neighbours(keys, limit=10, timeout=None):
    """Соседи по графу связей. Шаг тот же, что у прямого HTTP.

    Разойдись эти два пути вопросом или разбором — выдача поехала бы от
    того, каким клиентом сегодня подпёрта дверь. Поэтому шаг один на оба.
    """
    return graph.neighbours(
        lambda query, mode: read(query, mode=mode, timeout=timeout),
        keys, limit=limit)


def schema(timeout=None):
    return _api().get_schema(timeout=timeout)
