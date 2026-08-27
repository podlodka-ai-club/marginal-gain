#!/usr/bin/env python3
"""Вычистка учётных данных. Нижний слой: отсюда не звонят никуда.

Раньше это жило в encoder.py вместе со старым конвейером записи, а тот тянул
дверь хранилища. Получалось, что очистка секретов зависит от хранилища, и
кольцо encoder → xmem → telemetry → encoder держалось на отложенном импорте:
любой перенос его наверх ронял импорт всего проекта.

Шаблоны здесь одни на всех — и на путь записи, и на журнал. Копия уже
разъезжалась молча, поэтому второго списка быть не должно.
"""
import re

SECRETS = [
    (re.compile(r"\bxmem_[A-Za-z0-9_\-]{6,}"), "<xmem-key>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"), "<gitlab-token>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "<github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "<jwt>"),
    (re.compile(r"(?i)\b(authorization|bearer|private-token|api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"(?i)machine\s+\S+\s+login\s+\S+\s+password\s+\S+"), "<netrc-entry>"),
    # Учётные данные внутри адреса: scheme://user:secret@host. Так их отдаёт
    # git remote -v, и ни одна из проверок выше их не видит: префикса нет,
    # слова "token" рядом нет. Реальная утечка боевого ключа пришла отсюда.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^:/?#\s@]+):([^@/\s]+)@"), r"\1\2:<redacted>@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<private-key>"),
    (re.compile(r"\bhvs\.[A-Za-z0-9_\-]{10,}"), "<vault-token>"),
    (re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----"), "<private-key-marker>"),
]

def redact(text):
    for pat, repl in SECRETS:
        text = pat.sub(repl, text)
    return text
