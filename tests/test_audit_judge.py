#!/usr/bin/env python3
"""Аудит оценки: применена подсказка или нет и по какому признаку решено.

Запуск: python3 -m unittest tests.test_audit_judge -v

`domain.ledger.helped` — единственная дверь, которой в ленту попадает ответ
про пользу подсказки, тремя разными способами съёма (transcript/turn_end/
inline, см. ADR 0012). Признак и есть способ съёма: он объясняет, «по какому
признаку решено».

Мутации, на которых проверки обязаны краснеть:
  * не писать аудит на одном из трёх способов съёма           → TestEachSourceIsAudited
  * терять признак (source) в строке аудита                   → TestTheSourceIsTheCriterion
  * путать «да» и «нет/неизвестно» одним полем ok              → TestOkMatchesTheVerdict
"""
import os
import tempfile
import unittest

from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import audit, ledger

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "memory.db"
        audit.reset(self.base)
        self._env = mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(self.base)})
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        audit.reset(self.base)
        self._tmp.cleanup()


class TestEachSourceIsAudited(unittest.TestCase):
    @SLOW
    @given(source=st.sampled_from(ledger.SOURCES), verdict=st.sampled_from(ledger.VERDICTS))
    def test_every_call_leaves_exactly_one_judge_row(self, source, verdict):
        # `@given` на unittest.TestCase крутит примеры внутри одного вызова
        # setUp/tearDown, а не между ними — своя песочница нужна на каждый
        # пример, иначе строки прошлых примеров путаются с этим.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            with mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(base)}):
                ledger.helped("разговор-1", "2026-01-01T00:00:00Z", verdict,
                              source=source, log=tmp + "/ledger.jsonl")
            got = audit.rows(where=base, step="judge")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["output"]["verdict"], verdict)


class TestTheSourceIsTheCriterion(Case):
    def test_the_criterion_in_the_row_is_the_harvesting_method(self):
        ledger.helped("разговор-1", "2026-01-01T00:00:00Z", "yes", source="inline",
                      log=self._tmp.name + "/ledger.jsonl")
        row = audit.rows(where=self.base, step="judge")[0]
        self.assertEqual(row["input"]["source"], "inline")

    def test_the_injection_key_identifies_which_hint_was_judged(self):
        ledger.helped("разговор-1", "2026-01-01T00:00:00Z", "yes", source="transcript",
                      log=self._tmp.name + "/ledger.jsonl")
        row = audit.rows(where=self.base, step="judge")[0]
        self.assertEqual(row["input"]["injection"],
                         ledger.key_of("разговор-1", "2026-01-01T00:00:00Z"))
        self.assertEqual(row["session_id"], "разговор-1")


class TestOkMatchesTheVerdict(unittest.TestCase):
    def test_yes_is_ok_no_and_unknown_are_not(self):
        for verdict, expect_ok in (("yes", True), ("no", False), ("unknown", False)):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp) / "memory.db"
                audit.reset(base)
                with mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(base)}):
                    ledger.helped("разговор-%s" % verdict, "2026-01-01T00:00:00Z",
                                  verdict, source="transcript",
                                  log=tmp + "/ledger.jsonl")
                row = audit.rows(where=base, step="judge")[0]
                self.assertEqual(bool(row["ok"]), expect_ok, verdict)


if __name__ == "__main__":
    unittest.main()
