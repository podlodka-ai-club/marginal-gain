#!/usr/bin/env python3
"""Извлечение фактов из эпизода — реестр правил.

Правило это функция от эпизода, возвращает список фактов. Одна эвристика —
одна функция. Сборщик facts_of ничего про эвристики не знает: он складывает
то, что вернули правила из NAMES, и вычищает секреты один раз на выходе.

Так же устроен реестр признаков в domain/features.py, и по той же причине. Была
лесенка из четырёх if в одном теле; эпик обещает тридцать пять эвристик, то
есть тридцать пять правок этого тела, каждая с риском задеть соседние.

Типы фактов берём из схемы: user, preference, project_state,
external_resource. Выдумывать свои нельзя — хранилище их не отличит.
"""
import re
from pathlib import Path

from infra.scrub import redact

# Пути, которые не являются рабочим кодом: наши же записи разговоров,
# состояние инструментов, временные файлы.
NOT_CODE = ("/.claude/", "/.local/state/", "/tmp/", "/.cache/")

# Отказы, которые устроил сам обвес, а не внешний мир. Знанием не являются,
# но повторяются чаще всего настоящего и потому лезут наверх любой меры.
HARNESS_NOISE = ("pretooluse", "posttooluse", "blocked:", "exit code",
                 "tool_use_error", "hook error", "command not found",
                 "no such file or directory", "did not complete within")

# Темы предпочтений, подтверждённые архивом. Число в скобках это сколько
# просьб нашлось на 2026-08-24. Тем без подтверждения тут быть не должно.
PREF_TOPICS = [
    ("отвечать коротко, длинные ответы человек не читает",
     r"(кратк|короче|много текста|не читаю|сократ|покороче)"),
    ("проверять факты, не выдумывать, не утверждать непроверенное",
     r"(не выдумыв|проверь точно|убедись|перепровер)"),
    ("задавать вопросы по одному за раз",
     r"(по одному|поочеред|один вопрос|за раз)"),
    ("не использовать тире и дефисы в тексте",
     r"(тире|дефис)"),
    ("сначала показать план, править код после согласования",
     r"(сначала план|покажи план|согласу|не трогай пока|дождись)"),
]

# Имена в порядке добавления, как в features.NAMES. Стенд сверяется с ними.
NAMES = ["preferences", "edited_files", "obstacles", "links"]


def _project(ep):
    return Path(ep["cwd"]).name if ep["cwd"] else "unknown"


def _request(ep):
    return " ".join(ep["request"].split())


def preferences(ep):
    """Предпочтение это не любая просьба, а попадание в известную тему.

    Иначе каждое сообщение человека становится отдельным «предпочтением».
    Пример просьбы в текст не вшиваем: чужие слова из примера потом находятся
    поиском и выдаются как знание по чужой теме.
    """
    low = _request(ep).lower()
    return [("preference", topic, "global", "Пользователь просит: %s" % topic)
            for topic, pat in PREF_TOPICS if re.search(pat, low)]


def edited_files(ep):
    """Правка файла — состояние проекта. Наши служебные пути не в счёт."""
    project, request = _project(ep), _request(ep)
    return [("project_state", project, "project",
             "В проекте %s правился файл %s ради задачи: %s"
             % (project, target, request[:200]))
            for target in ep["files"][:15]
            if not any(bad in target for bad in NOT_CODE)]


def obstacles(ep):
    """Первое препятствие эпизода. Отказы обвеса отсеиваем: это не знание."""
    project = _project(ep)
    return [("project_state", project, "project",
             "В проекте %s упирались в препятствие: %s" % (project, err[:300]))
            for err in ep["errors"][:1]
            if not any(bad in err.lower() for bad in HARNESS_NOISE)]


# Чем адрес обычно обёрнут в тексте ответа. Обёртка прилипала к адресу и
# уезжала в набор эталонов как часть ожидаемого: случай ждал `...:3000"` и
# не мог пройти ничем.
WRAPPERS = ".,;:!?)]}>\"'`«»"


def links(ep):
    """Первый адрес из каждого ответа агента."""
    project, out = _project(ep), []
    for reply in ep["replies"]:
        for token in ("http://", "https://"):
            idx = reply.find(token)
            if idx >= 0:
                url = reply[idx:].split()[0].rstrip(WRAPPERS)
                out.append(("external_resource", project, "project",
                            "Для проекта %s использовался адрес %s" % (project, url)))
                break
    return out


RULES = {"preferences": preferences, "edited_files": edited_files,
         "obstacles": obstacles, "links": links}


def facts_of(ep):
    """Факты эпизода по всем известным правилам. Секреты чистим один раз."""
    found = [fact for name in NAMES for fact in RULES[name](ep)]
    return [(t, s, sc, redact(c)) for t, s, sc, c in found]


def fact_key(fact_type, subject, scope, content):
    """Чем два факта считаются одним и тем же."""
    if fact_type == "external_resource":
        return ("url", content.split()[-1])
    if fact_type == "preference":
        return ("pref", subject)   # subject это тема, она и есть ключ
    if "правился файл" in content:
        return ("file", subject, content.split("правился файл ")[-1].split(" ради")[0])
    return ("other", subject, content[:60])
