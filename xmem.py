#!/usr/bin/env python3
"""Единственная дверь наружу: всё общение с xmemory идёт только отсюда.

За дверью три пути, они выбираются переменной XMEM_BACKEND:

  cli  консольная утилита xmemcli. Работает без ключа доступа, но умеет только
       текстовую запись: ключ выводит экстрактор, и при промахе записи
       схлопываются на одну строку. Значение по умолчанию — так было до сих пор.
  api  прямой HTTP. Умеет структурную запись, где ключ задан полем.
  sdk  официальный клиент для Python. То же, что api, но соединение и разбор
       ответа держит он.

Структурная запись есть только у api и sdk. Вызов write_objects на cli падает
явной ошибкой, а не тихо уходит в текст.
"""
import os, subprocess

import telemetry

# Идентификатор хранилища берём из окружения, в коде его быть не должно.
# Задать можно так: export XMEM_INSTANCE_ID=<id>
INSTANCE = os.environ.get("XMEM_INSTANCE_ID", "")

# Пустая переменная в окружении значит «не задано», см. .env.example.
BACKEND = (os.environ.get("XMEM_BACKEND") or "cli").strip().lower()

# Память можно выключить целиком: чтение отдаёт пустоту, запись молча гаснет.
# Нужно для половины сравнения «без памяти» — иначе не с чем сравнивать.
DISABLED = bool(os.environ.get("XMEM_DISABLED"))

# Linux не пропускает один аргумент длиннее 128 КиБ (MAX_ARG_STRLEN).
# Берём с запасом, потому что кириллица это два байта на символ.
MAX_ARG_BYTES = 100_000

# Консоль называет режимы чтения короче, чем сервис.
READ_MODES = {"single": "single-answer", "raw": "raw-tables", "xresponse": "xresponse"}


class BackendError(RuntimeError):
    """Путь наружу не может выполнить то, о чём его просят."""


def _cli_run(args, timeout=180):
    if not INSTANCE:
        raise RuntimeError("не задан XMEM_INSTANCE_ID: укажи хранилище в окружении")
    env = dict(os.environ, XMEM_INSTANCE_ID=INSTANCE)
    proc = subprocess.run(["xmemcli"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("xmemcli %s: %s" % (args[0], (proc.stderr or proc.stdout)[:400]))
    return proc.stdout.strip()


# Прежнее имя. Оставлено, потому что на него ссылается разбор в тестах и заметках.
_run = _cli_run


def _adapter():
    if BACKEND == "api":
        import xmem_api
        return xmem_api
    if BACKEND == "sdk":
        import xmem_sdk
        return xmem_sdk
    if BACKEND == "cli":
        return None
    raise BackendError("неизвестный XMEM_BACKEND: %r, допустимо cli, api, sdk" % BACKEND)


def _split(text):
    """Длинную запись режем на части по границе байтов. Ничего не теряем."""
    data = text.encode("utf-8")
    if len(data) <= MAX_ARG_BYTES:
        return [text]
    parts, start = [], 0
    while start < len(data):
        chunk = data[start:start + MAX_ARG_BYTES]
        # не рвём символ пополам
        while chunk:
            try:
                parts.append(chunk.decode("utf-8"))
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        start += len(chunk)
    total = len(parts)
    return ["[часть %d из %d] %s" % (i + 1, total, p) for i, p in enumerate(parts)]


def write(text, wait=False):
    """Текстовая запись. Ключ выводит экстрактор, см. предупреждение вверху."""
    if DISABLED:
        return ""
    adapter = _adapter()
    if adapter is not None:
        # Ограничение на длину аргумента — свойство консоли, а не сервиса.
        return adapter.write_text(text, wait=wait)
    out = []
    for part in _split(text):
        args = ["write", part]
        if not wait:
            args.append("--no-wait")
        out.append(_cli_run(args))
    return out[0] if len(out) == 1 else "частей %d" % len(out)


def write_objects(records, relations=()):
    """Структурная запись записей схемы и связей между ними.

    Принимает объекты из models. Порядок сохраняется: связь применяется после
    объектов, которые она соединяет, поэтому их можно слать одним вызовом.
    """
    if DISABLED:
        return None
    adapter = _adapter()
    if adapter is None:
        raise BackendError("структурная запись недоступна в консоли: "
                           "поставь XMEM_BACKEND=api или XMEM_BACKEND=sdk")
    mutations = [r.mutation() for r in records] + list(relations)
    return adapter.write_objects(mutations)


@telemetry.traced("retrieve", lambda arg, out: {
    "backend": BACKEND, "disabled": DISABLED, "mode": arg["mode"],
    "query_chars": len(arg["query"] or ""), "answer_chars": len(out or "")})
def read(query, mode="single"):
    """Всегда строка: вызывающие разбирают ответ текстом, см. suggest.pieces."""
    if DISABLED:
        return ""
    adapter = _adapter()
    if adapter is not None:
        return adapter.read(query, mode=READ_MODES.get(mode, mode))
    return _cli_run(["read", query, "--read-mode", mode])
