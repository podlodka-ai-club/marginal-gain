#!/usr/bin/env python3
"""Адаптер поверх HTTP-интерфейса xmemory. Без сторонних библиотек.

Нужен затем, что консольная утилита умеет только текстовую запись: ключ в ней
выводит экстрактор, и при промахе записи схлопываются. Здесь ключ уходит полем.
"""
import json
import os
import socket
import urllib.error
import urllib.request

from storage import graph

# Пустая переменная в окружении значит «не задано», поэтому `or`, а не второй
# аргумент get: .env.example разрешает оставить строку пустой.
BASE = (os.environ.get("XMEM_API_URL") or "https://api.xmemory.ai").rstrip("/")
TIMEOUT = float(os.environ.get("XMEM_TIMEOUT") or 180)


class ApiError(RuntimeError):
    pass


def _instance():
    value = os.environ.get("XMEM_INSTANCE_ID", "")
    if not value:
        raise ApiError("не задан XMEM_INSTANCE_ID: укажи хранилище в окружении")
    return value


def _headers():
    key = os.environ.get("XMEM_API_KEY", "")
    if not key:
        raise ApiError("не задан XMEM_API_KEY: ключ доступа в коде хранить нельзя")
    return {"Content-Type": "application/json", "Authorization": "Bearer %s" % key}


def _call(path, payload=None, method="POST", timeout=None):
    url = "%s/instances/%s%s" % (BASE, _instance(), path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # В сообщение идёт path, а не url: адрес может нести учётные данные.
        raise ApiError("%s %s: %s" % (method, path, detail)) from exc
    except urllib.error.URLError as exc:
        raise ApiError("%s %s: %s" % (method, path, exc.reason)) from exc
    except (socket.timeout, TimeoutError, OSError, ValueError) as exc:
        raise ApiError("%s %s: %s" % (method, path, exc)) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("%s %s: ответ не разбирается как JSON: %s"
                       % (method, path, raw[:200])) from exc


def write_text(text, wait=True, timeout=None):
    """Текстовая запись. Ключ выводит экстрактор — тот же риск, что у консоли."""
    path = "/write" if wait else "/write_async"
    return _call(path, {"text": text}, timeout=timeout)


def write_objects(mutations, timeout=None):
    """Структурная запись: ключ задан явно, модель в записи не участвует.

    Мутации применяются по порядку списка, поэтому связь можно ставить в том же
    вызове, что и объекты, которые она соединяет.
    """
    items = list(mutations)
    if not items:
        raise ApiError("структурная запись без единой мутации")
    return _call("/write", {"structured_mutations": items}, timeout=timeout)


def read(query, mode="single-answer", timeout=None):
    """Возвращает текст ответа. Сервис называет поле режима mode, не read_mode."""
    out = _call("/read", {"query": query, "mode": mode}, timeout=timeout)
    answer = out.get("reader_result")
    if answer is None:
        return ""
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def neighbours(keys, limit=10, timeout=None):
    """Соседи по графу связей. Шаг общий с клиентом, см. storage/graph.py.

    Читателя отдаём по имени модуля, а не ссылкой на функцию: так подмена
    читателя доезжает до шага, а не обходится им стороной.
    """
    return graph.neighbours(
        lambda query, mode: read(query, mode=mode, timeout=timeout),
        keys, limit=limit)


def schema(timeout=None):
    return _call("/schema", None, method="GET", timeout=timeout)
