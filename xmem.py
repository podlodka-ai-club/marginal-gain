#!/usr/bin/env python3
"""Единственная дверь наружу: всё общение с xmemory идёт только отсюда."""
import os, subprocess

# Идентификатор хранилища берём из окружения, в коде его быть не должно.
# Задать можно так: export XMEM_INSTANCE_ID=<id>
INSTANCE = os.environ.get("XMEM_INSTANCE_ID", "")

# Linux не пропускает один аргумент длиннее 128 КиБ (MAX_ARG_STRLEN).
# Берём с запасом, потому что кириллица это два байта на символ.
MAX_ARG_BYTES = 100_000


def _run(args, timeout=180):
    if not INSTANCE:
        raise RuntimeError("не задан XMEM_INSTANCE_ID: укажи хранилище в окружении")
    env = dict(os.environ, XMEM_INSTANCE_ID=INSTANCE)
    proc = subprocess.run(["xmemcli"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("xmemcli %s: %s" % (args[0], (proc.stderr or proc.stdout)[:400]))
    return proc.stdout.strip()


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
    out = []
    for part in _split(text):
        args = ["write", part]
        if not wait:
            args.append("--no-wait")
        out.append(_run(args))
    return out[0] if len(out) == 1 else "частей %d" % len(out)


def read(query, mode="single"):
    return _run(["read", query, "--read-mode", mode])
