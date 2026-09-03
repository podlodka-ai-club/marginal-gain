#!/usr/bin/env python3
"""Вставка памяти: что подставили, из чего собрали, помогло ли.

Запуск: python3 -m unittest tests.test_injection_feedback -v

Память записывала, что подставила в разговор, и никогда не отмечала исход:
семнадцать записей о вставке лежали без единой отметки и без единой связи с
тем, из чего они собраны. Пока петли нет, улучшать память можно только по
набору эталонов — по тому, что мы сами про неё придумали, а не по тому, как
она сработала в живой работе.

Запись при этом уходила прозой, и ключ ей выводил разборщик на той стороне —
тот самый класс, который уже чинили у фактов: ключ, которого мы не знаем, в
связь не поставить.

**Правило отметки, записанное до кода.** `helped` — не «память помогла»;
такого наблюдения у нас нет. Это «ход, в который её подставили, дошёл до
конца»: берём эпизод того же разговора, ВНУТРИ которого случилась вставка, а
если такого нет — первый эпизод, начавшийся после, и смотрим его исход.
`done` — True, `blocked` — False, `abandoned` или эпизода нет вовсе — поле не
пишем: неизвестное это не отрицательное.

Первое условие обязательно: подсказку показывают внутри того же эпизода,
которым её и вызвали, а он по метке начала стартовал раньше вставки почти
всегда. Прежняя версия правила искала только эпизод, начавшийся не раньше
вставки, — и свой собственный эпизод не находила никогда, см.
TestAnInjectionInsideItsOwnEpisodeIsFound ниже.
"""
import contextlib
import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from archive.transcripts import parse_time
from domain import models
from pipeline import suggest, understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "injections"
TALK = "разговор-1"

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

OUTCOMES = st.sampled_from(["done", "blocked", "abandoned"])


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def episode_rows(session, specs):
    """Строки транскрипта: эпизод на просьбу, исход задаётся ответом и ошибкой.

    `ended_at` — своей меткой, если задана, иначе той же, что и начало
    (`at`). Раздельные метки нужны там, где важно настоящее «внутри»
    эпизода: на одной метке для начала и конца вставке внутри него просто
    негде оказаться.
    """
    out = []
    for spec in specs:
        start = {"sessionId": session, "timestamp": spec["at"], "cwd": CWD,
                 "gitBranch": BRANCH}
        out.append(dict(start, type="user",
                        message={"content": spec.get("request", "Посмотри, "
                                 "что там с базой")}))
        if spec["outcome"] == "done":
            blocks = [{"type": "tool_use", "name": "Edit",
                       "input": {"file_path": "%s/db.py" % CWD}},
                      {"type": "text", "text": "Готово."}]
        elif spec["outcome"] == "blocked":
            blocks = [{"type": "tool_result", "is_error": True,
                       "content": "FileNotFoundError: db.py"},
                      {"type": "text", "text": "Не вышло."}]
        else:
            # Брошенный ход — это ход без ответа вовсе. Пустая реплика не
            # брошенный: она попадает в список ответов и делает эпизод удавшимся.
            continue
        end = dict(start, timestamp=spec.get("ended_at", spec["at"]))
        out.append(dict(end, type="assistant", message={"content": blocks}))
    return out


def archive(root, specs, session=TALK, name="разговор.jsonl"):
    path = Path(root) / name
    with path.open("a", encoding="utf-8") as fh:
        for line in episode_rows(session, specs):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


BASE_TIME = dt.datetime(2026, 8, 28, 0, 0, 0, tzinfo=dt.timezone.utc)


def ts(offset_seconds):
    """Метка времени, сортируемая как строка — тем же способом, что архив."""
    return (BASE_TIME + dt.timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def injections_in(base):
    if not Path(base).exists():
        return {}
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return {(r["session_id"], r["injected_at"]): dict(r)
                for r in conn.execute("SELECT * FROM memoryinjection")}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def links_of(base, relation):
    conn = sqlite3.connect(str(base))
    try:
        found = {}
        for link_id, role, kind, key in conn.execute(
                "SELECT link_id, role, object_type, object_key FROM links "
                "WHERE relation = ?", (relation,)):
            found.setdefault(link_id, {})[role] = (kind, json.loads(key))
        return found
    finally:
        conn.close()


def has_row(base, table, key):
    where = " AND ".join('"%s" = ?' % name for name in key)
    conn = sqlite3.connect(str(base))
    try:
        return bool(conn.execute('SELECT 1 FROM "%s" WHERE %s' % (table, where),
                                 list(key.values())).fetchone())
    finally:
        conn.close()


class Spy:
    """Настоящая дверь, которая помнит, чем её звали."""

    def __init__(self, inner):
        self.inner, self.texts, self.batches = inner, [], []

    def write(self, text, wait=False):
        self.texts.append(text)
        return self.inner.write(text, wait=wait)

    def write_objects(self, records, relations=()):
        self.batches.append((list(records), list(relations)))
        return self.inner.write_objects(records, relations)

    def read(self, query, mode="single"):
        return self.inner.read(query, mode=mode)


def fill(files):
    """Наполняем хранилище так, как это делает конвейер."""
    understand.digest(files, door=port.door(), dry=False)


class TestTheInjectionGoesAsStructure(unittest.TestCase):
    """Запись о вставке уходит записью схемы, а не прозой."""

    @SLOW
    @given(outcome=OUTCOMES)
    def test_a_structured_door_is_never_asked_to_write_text(self, outcome):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            spy = Spy(port.door())
            suggest.note_injection(TALK, "Из памяти: что-то было", door=spy)
            self.assertEqual(spy.texts, [], "вставка ушла прозой")
            self.assertTrue(spy.batches, "вставка не ушла вовсе")

    def test_the_key_is_ours_and_has_no_empty_half(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            suggest.note_injection(None, "Из памяти: что-то было", door=port.door())
            found = injections_in(base)
            self.assertEqual(len(found), 1)
            for (talk, at) in found:
                self.assertTrue(talk, "разговор пустой")
                self.assertTrue(at, "время вставки пустое")


class TestTheInjectionKnowsItsTalkAndSources(unittest.TestCase):
    """Связи: куда подставили и из чего собрали."""

    def test_the_talk_is_linked(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            suggest.note_injection(TALK, "Из памяти: что-то было", door=port.door())
            found = links_of(base, "injection_target_session")
            self.assertEqual(len(found), 1)
            ends = list(found.values())[0]
            self.assertEqual(set(ends), {"memory_injection", "session"})
            self.assertEqual(ends["session"][1]["session_id"], TALK)

    def test_every_source_fact_is_linked(self):
        """Факт, попавший в подсказку, становится источником вставки."""
        specs = [{"request": "Посмотри, что там с базой", "outcome": "done",
                  "at": "2026-08-28T10:00:00Z"}]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, specs)
            fill(files)
            door = port.door()
            text, kept, _ = suggest.suggest("файлы demo", mode="raw",
                                            min_score=0.0, door=door)
            self.assertTrue(kept, "память ничего не нашла, проверять нечего")
            suggest.note_injection(TALK, text, kept, door=door)
            found = links_of(base, "injection_source_fact")
            self.assertTrue(found, "источников у вставки нет")
            for ends in found.values():
                self.assertEqual(set(ends), {"memory_injection", "fact"})
                self.assertTrue(has_row(base, "fact", ends["fact"][1]),
                                "источник вставки — факт, которого нет в базе")

    def test_an_injection_without_sources_is_still_written(self):
        """Источников может не быть: вставка от этого не перестаёт быть."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            suggest.note_injection(TALK, "Из памяти: что-то было", (), door=port.door())
            self.assertEqual(len(injections_in(base)), 1)
            self.assertEqual(links_of(base, "injection_source_fact"), {})


class TestTheOutcomeIsSettledFromTheArchive(unittest.TestCase):
    """Отметка исхода. Правило описано в начале файла и здесь исполняется."""

    @SLOW
    @given(outcome=OUTCOMES)
    def test_the_mark_follows_the_next_episode(self, outcome):
        specs = [{"request": "Посмотри, что там с базой", "outcome": outcome,
                  "at": "2026-08-28T12:00:00Z"}]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, specs)
            fill(files)
            door = port.door()
            suggest.note_injection(TALK, "Из памяти: что-то было", (), door=door,
                                   at="2026-08-28T11:00:00Z")
            suggest.settle(files, door=door)
            row = list(injections_in(base).values())[0]
            self.assertEqual(row["session_outcome"], outcome)
            if outcome == "done":
                self.assertEqual(row["helped"], 1)
            elif outcome == "blocked":
                self.assertEqual(row["helped"], 0)
            else:
                self.assertIsNone(row["helped"],
                                  "неизвестное записано как отрицательное")

    def test_an_episode_before_the_injection_does_not_count(self):
        """Ход, который кончился до вставки, к ней отношения не имеет."""
        specs = [{"request": "Посмотри, что там с базой", "outcome": "done",
                  "at": "2026-08-28T09:00:00Z"}]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, specs)
            fill(files)
            door = port.door()
            suggest.note_injection(TALK, "Из памяти: что-то было", (), door=door,
                                   at="2026-08-28T18:00:00Z")
            suggest.settle(files, door=door)
            row = list(injections_in(base).values())[0]
            self.assertIsNone(row["helped"])
            self.assertEqual(row["session_outcome"], "unknown")

    def test_settling_twice_changes_nothing(self):
        specs = [{"request": "Посмотри, что там с базой", "outcome": "done",
                  "at": "2026-08-28T12:00:00Z"}]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, specs)
            fill(files)
            door = port.door()
            suggest.note_injection(TALK, "Из памяти: что-то было", (), door=door,
                                   at="2026-08-28T11:00:00Z")
            suggest.settle(files, door=door)
            was = injections_in(base)
            suggest.settle(files, door=door)
            self.assertEqual(injections_in(base), was)

    @SLOW
    @given(outcome=OUTCOMES)
    def test_the_mark_belongs_to_the_schema(self, outcome):
        """Исход — из списка схемы, а не выдуманное слово."""
        specs = [{"request": "Посмотри, что там с базой", "outcome": outcome,
                  "at": "2026-08-28T12:00:00Z"}]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, specs)
            fill(files)
            door = port.door()
            suggest.note_injection(TALK, "Из памяти", (), door=door,
                                   at="2026-08-28T11:00:00Z")
            suggest.settle(files, door=door)
            for row in injections_in(base).values():
                self.assertIn(row["session_outcome"], models.INJECTION_OUTCOMES)


SPANNING_OUTCOMES = st.sampled_from(["done", "blocked"])


@st.composite
def injection_inside_its_episode(draw):
    """Начало эпизода, его длина и точка вставки строго внутри — в секундах.

    Вставку показывают внутри того же эпизода, которым её и вызвали: эпизод
    по метке начала стартовал раньше вставки, а по метке конца кончился не
    раньше её. `span >= 2` и `1 <= inside <= span - 1` держат вставку строго
    между началом и концом при любом случайном раскладе меток.
    """
    start = draw(st.integers(min_value=0, max_value=5000))
    span = draw(st.integers(min_value=2, max_value=1000))
    inside = draw(st.integers(min_value=1, max_value=span - 1))
    outcome = draw(SPANNING_OUTCOMES)
    return start, span, inside, outcome


class TestAnInjectionInsideItsOwnEpisodeIsFound(unittest.TestCase):
    """Общий баг, не случай одной пары.

    Подсказку показывают внутри того эпизода, которым её и вызвали: эпизод
    стартовал раньше, чем сработал поиск вставки внутри него. Старое правило
    искало первый эпизод, начавшийся НЕ РАНЬШЕ вставки, — и потому не
    находило свой же эпизод никогда, при любом соотношении меток начала,
    вставки и конца. Проверено на широком разбросе меток, без единого
    живого прогона: баг общий для любой вставки, показанной внутри
    вызвавшего её эпизода, а не только для пары «макбук».
    """

    @SLOW
    @given(injection_inside_its_episode())
    def test_settle_finds_the_episode_that_contains_the_injection(self, spec):
        start, span, inside, outcome = spec
        talk = "разговор-внутри"
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, [{"outcome": outcome, "at": ts(start),
                                   "ended_at": ts(start + span)}], session=talk)
            fill(files)
            door = port.door()
            suggest.note_injection(talk, "Из памяти: что-то было", (), door=door,
                                   at=ts(start + inside))
            suggest.settle(files, door=door)
            row = list(injections_in(base).values())[0]
            self.assertEqual(row["session_outcome"], outcome,
                             "вставка внутри своего же эпизода не нашла его")
            expect_helped = {"done": True, "blocked": False}[outcome]
            self.assertEqual(bool(row["helped"]), expect_helped)


class TestTimestampFormatsCompareAsMomentsNotStrings(unittest.TestCase):
    """Найдено /code-review при разборе фикса выше.

    Харнесс шлёт метки эпизодов с `Z`, без дробной части. Наша же
    `injected_at` (см. `injection_of`) — со смещением `+00:00` и
    микросекундами. В одну и ту же секунду строки расходятся на символе
    конца: `.` меньше `Z`, и наивное сравнение строк даёт не тот порядок,
    который был на самом деле. Сравнивать нужно моменты, а не строки.
    """

    def test_a_bare_second_still_orders_before_a_fractional_one(self):
        """Пример ревьюера как есть: без `parse_time` сравнение строк лжёт —
        эпизод, кончившийся раньше вставки, строкой выглядит кончившимся
        позже неё."""
        ended = "2026-09-03T12:34:56Z"
        at = "2026-09-03T12:34:56.500000+00:00"
        self.assertTrue(ended >= at,
                        "сам пример должен ломать сравнение строк (наивно "
                        "«Z» кажется позже дроби) — иначе тест ничего не "
                        "проверяет")
        self.assertLess(parse_time(ended), parse_time(at),
                        "момент из `Z`-метки должен упорядочиться раньше "
                        "момента с микросекундами в ту же секунду")

    def test_settle_does_not_claim_an_episode_that_already_ended(self):
        """Сквозной случай той же путаницы: эпизод кончился ДО вставки, но
        строкой конец («Z», без дроби) кажется позже вставки (с дробью) —
        и наивное сравнение засчитывает его как «содержащий», хотя вставка
        пришла уже после конца. Правильный ответ — соседний эпизод,
        начавшийся позже вставки по-настоящему."""
        talk = "разговор-граница"
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, [
                {"outcome": "blocked", "at": "2026-09-03T12:34:50Z",
                 "ended_at": "2026-09-03T12:34:56Z"},
                {"outcome": "done", "at": "2026-09-03T12:35:00Z",
                 "ended_at": "2026-09-03T12:35:10Z"},
            ], session=talk)
            fill(files)
            door = port.door()
            # Вставка — на полсекунды позже конца ПЕРВОГО эпизода: тот уже
            # закрылся, вставка ему не принадлежит.
            suggest.note_injection(talk, "Из памяти: что-то было", (), door=door,
                                   at="2026-09-03T12:34:56.500000+00:00")
            suggest.settle(files, door=door)
            row = list(injections_in(base).values())[0]
            self.assertEqual(row["session_outcome"], "done",
                             "вставку после конца эпизода засчитали в этот "
                             "уже закрытый эпизод")


class TestTheJournalIsTheListOfInjections(unittest.TestCase):
    """Отметка исхода идёт по журналу: перечислить вставки в хранилище нечем.

    Отсюда её слабое место, названное явно: строка журнала без записи в
    хранилище заводит запись с одним ключом и без содержимого. Так и вышло
    один раз — сквозной тест хука писал в живой журнал пользователя.
    """

    def test_a_journal_line_without_a_key_is_skipped(self):
        """Старые строки журнала — про запрос, а не про вставку. Их не берём."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            log = Path(tmp) / "suggest-log.jsonl"
            log.write_text(json.dumps({"query": "что там", "kept": 0,
                                       "sent": False}) + "\n", encoding="utf-8")
            self.assertEqual(suggest.notes_of(log), [])

    def test_a_broken_line_does_not_kill_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            log = Path(tmp) / "suggest-log.jsonl"
            log.write_text("не json\n" + json.dumps(
                {"session_id": TALK, "injected_at": "2026-08-28T11:00:00Z",
                 "sent": True}) + "\n", encoding="utf-8")
            self.assertEqual(suggest.notes_of(log),
                             [(TALK, "2026-08-28T11:00:00Z")])

    def test_the_injection_writes_its_own_key_to_the_journal(self):
        """Ключ пишет сама отметка: молчание строки не оставляет."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            suggest.note_injection(TALK, "Из памяти: что-то было", (),
                                   door=port.door())
            self.assertEqual([talk for talk, _ in suggest.notes_of()], [TALK])

    def test_a_failed_write_leaves_no_key_behind(self):
        """Ключ в журнале без записи в хранилище заводит пустую вставку.

        Порядок поэтому такой: сперва запись, потом журнал. Иначе `--settle`
        по осиротевшему ключу создаёт запись с одним ключом и без содержимого —
        так уже случалось, чистить пришлось вручную.
        """

        class Broken:
            def write_objects(self, records, relations=()):
                raise RuntimeError("хранилище не приняло запись")

        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            with self.assertRaises(RuntimeError):
                suggest.note_injection(TALK, "Из памяти", (), door=Broken())
            self.assertEqual(suggest.notes_of(), [])


if __name__ == "__main__":
    unittest.main()
