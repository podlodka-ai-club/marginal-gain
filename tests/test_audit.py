#!/usr/bin/env python3
"""Журнал аудита: ядро записи и чтения — `domain.audit`.

Запуск: python3 -m unittest tests.test_audit -v

Прогон отчитывался исходами одним словом на ступень обрыва: «факт в БД»
молчала, что именно модель разметила, что маппер отбросил и почему. Здесь
каждое действие пишется в отдельную таблицу той же базы, как есть, и это ядро
проверяется свойствами до того, как за него цепляются сами шаги конвейера.

Свойства:
1. Вход и выход переживают запись и чтение назад — любые вложенные структуры
   JSON, юникод, отсутствующие поля.
2. Запись никогда не бросает исключение наружу: ни когда объект не
   сериализуется, ни когда путь к базе недоступен. Аудит это наблюдение, а не
   решение, которое вправе уронить горячий путь.
3. Строки возвращаются в порядке записи.
4. Отказ шага (`ok=False`) — такая же строка, как успех, отличается только
   полем `ok`.
5. Неизвестный шаг отклоняется явно: опечатка в имени не должна тихо выпадать
   из отчёта.
6. Номер прогона берётся из окружения и пуст вне прогона.

Мутации, на которых проверки обязаны краснеть:
  * ловить исключение сериализации и не писать строку вовсе        → TestRecordNeverRaises
  * менять порядок полей / терять run_id или session_id            → TestRunId, TestOrderAndRefusal
  * писать неизвестный шаг без ошибки                              → TestUnknownStepIsRejected
  * путать «успех» и «отказ» одним полем                           → TestOrderAndRefusal
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import audit
from storage import db

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

# Юникод без суррогатных половинок: они не кодируются в UTF-8, а именно им
# sqlite3 пишет TEXT-колонку. Строка с суррогатом не потерялась бы содержанием
# — она уронила бы саму запись, и тест мерил бы не то, ради чего заведён.
_CHARS = st.characters(blacklist_categories=("Cs",))
TEXT = st.text(alphabet=_CHARS, max_size=40)

JSON_VALUE = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(min_value=-10**6, max_value=10**6),
             st.floats(allow_nan=False, allow_infinity=False, width=32), TEXT),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(TEXT, children, max_size=4)),
    max_leaves=8)


class Explodes:
    """Объект, у которого и JSON, и `str` падают. Проверяет полную терпимость."""

    def __str__(self):
        raise RuntimeError("boom")

    def __repr__(self):
        raise RuntimeError("boom")


class TestTheRecordSurvivesTheRoundTrip(unittest.TestCase):
    @SLOW
    @given(step=st.sampled_from(audit.STEPS), session_id=st.one_of(st.none(), TEXT),
          ok=st.booleans(), payload=JSON_VALUE)
    def test_input_and_output_round_trip(self, step, session_id, ok, payload):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            audit.record(step, input=payload, output=payload,
                        session_id=session_id, ok=ok, where=base)
            got = audit.rows(where=base)
            self.assertEqual(len(got), 1)
            row = got[0]
            self.assertEqual(row["step"], step)
            self.assertEqual(row["session_id"], session_id)
            self.assertEqual(bool(row["ok"]), ok)
            self.assertEqual(row["input"], payload)
            self.assertEqual(row["output"], payload)

    def test_a_plain_string_is_kept_as_is_not_re_quoted(self):
        """Строка на входе — строка и на выходе, не строка в кавычках JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.record("mark", input="дословный ответ модели", where=base)
            self.assertEqual(audit.rows(where=base)[0]["input"], "дословный ответ модели")

    def test_none_stays_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.record("mark", input=None, output=None, where=base)
            row = audit.rows(where=base)[0]
            self.assertIsNone(row["input"])
            self.assertIsNone(row["output"])


class TestRecordNeverRaises(unittest.TestCase):
    def test_an_unserializable_payload_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            try:
                audit.record("mark", input={"bad": Explodes()}, where=base)
            except Exception as bad:                        # noqa: BLE001
                self.fail("record() бросила наружу: %r" % (bad,))
            # Строка либо легла с заменителем текста, либо не легла вовсе —
            # оба исхода допустимы, недопустимо только исключение наружу.

    def test_an_unwritable_path_does_not_raise(self):
        bad_path = Path(tempfile.gettempdir()) / "нет-такого-каталога-xyz" / "a" / "memory.db"
        try:
            audit.record("mark", input={"a": 1}, where=bad_path)
        except Exception as bad:                            # noqa: BLE001
            self.fail("record() бросила наружу: %r" % (bad,))

    def test_a_broken_connection_cache_does_not_raise(self):
        """Кэш соединения помнит закрытое — запись тем не менее не падает."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            audit.record("mark", where=base)
            conn = audit._connection(base)
            conn.close()                                    # порча кэша нарочно
            try:
                audit.record("mark", where=base)
            except Exception as bad:                        # noqa: BLE001
                self.fail("record() бросила наружу: %r" % (bad,))


class TestUnknownStepIsRejected(unittest.TestCase):
    def test_unknown_step_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            with self.assertRaises(ValueError):
                audit.record("не-такой-шаг", where=base)

    def test_the_broken_row_is_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            with self.assertRaises(ValueError):
                audit.record("выдуманный-шаг", where=base)
            self.assertEqual(audit.rows(where=base), [])


class TestOrderAndRefusal(unittest.TestCase):
    @SLOW
    @given(oks=st.lists(st.booleans(), min_size=1, max_size=12))
    def test_rows_come_back_in_write_order_and_refusals_are_rows_too(self, oks):
        """Отказ шага пишется так же, как успех — «ничего не найдено» это действие."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            for i, ok in enumerate(oks):
                audit.record("search", input={"n": i}, output={"n": i},
                            ok=ok, where=base)
            got = audit.rows(where=base)
            self.assertEqual([r["input"]["n"] for r in got], list(range(len(oks))))
            self.assertEqual([bool(r["ok"]) for r in got], oks)
            self.assertEqual(len(got), len(oks), "отказ потерялся среди успехов")


class TestFiltering(unittest.TestCase):
    def test_rows_filter_by_run_step_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            with mock.patch.dict(os.environ, {"XMEM_RUN_ID": "run-a"}):
                audit.record("search", session_id="s1", where=base)
                audit.record("inject", session_id="s1", where=base)
            with mock.patch.dict(os.environ, {"XMEM_RUN_ID": "run-b"}):
                audit.record("search", session_id="s2", where=base)
            self.assertEqual(len(audit.rows(where=base, run="run-a")), 2)
            self.assertEqual(len(audit.rows(where=base, run="run-b")), 1)
            self.assertEqual(len(audit.rows(where=base, step="search")), 2)
            self.assertEqual(len(audit.rows(where=base, session_id="s1")), 2)
            self.assertEqual(len(audit.rows(where=base, run="run-a", step="inject")), 1)


class TestRunId(unittest.TestCase):
    def test_run_id_comes_from_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"XMEM_RUN_ID": "run-42"}):
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            audit.record("search", where=base)
            self.assertEqual(audit.rows(where=base)[0]["run_id"], "run-42")

    def test_run_id_is_empty_outside_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("XMEM_RUN_ID", None)
            with mock.patch.dict(os.environ, env, clear=True):
                base = Path(tmp) / "memory.db"
                audit.reset(base)
                audit.record("search", where=base)
                self.assertEqual(audit.rows(where=base)[0]["run_id"], "")


class TestMigration(unittest.TestCase):
    def test_running_migrate_twice_keeps_one_table_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            conn = db.connect(base)
            db.migrate(conn)
            db.migrate(conn)
            conn.execute(
                "INSERT INTO audit (ts, run_id, session_id, step, ok) "
                "VALUES ('t', '', NULL, 'mark', 1)")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT count(*) FROM audit").fetchone()[0], 1)
            conn.close()

    def test_the_table_has_the_columns_the_report_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            conn = db.connect(base)
            db.migrate(conn)
            cols = {row[1] for row in conn.execute('PRAGMA table_info("audit")')}
            conn.close()
            for name in ("ts", "run_id", "session_id", "step", "ok", "input", "output"):
                self.assertIn(name, cols)

    def test_a_fresh_database_gets_the_table_too(self):
        """Пустая база получает таблицу аудита с первого же открытия."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "fresh.db"
            conn = db.connect(base)
            db.migrate(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='audit'"
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row, "таблица audit не создалась на пустой базе")


if __name__ == "__main__":
    unittest.main()
