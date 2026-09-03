#!/usr/bin/env python3
"""Отчёт по журналу аудита: `eval.audit_report`.

Запуск: python3 -m unittest tests.test_audit_report -v

Отчёт строится скриптом по таблице аудита, а не собирается глазами по логам —
это и есть весь смысл задачи. Проверки здесь про то, что отчёт не молчит там,
где молчит сам аудит, и не обрезает то, что просили не сокращать.

Мутации, на которых проверки обязаны краснеть:
  * пропускать шаг без строк вместо явного «не сработал»    → TestSilentStepIsNamed
  * `--full` теряет содержимое входа/выхода                 → TestFullPrintsEverything
  * `--run` не сужает выборку                                → TestRunFilters
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import audit_report
from storage import audit


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "memory.db"
        audit.reset(self.base)

    def tearDown(self):
        audit.reset(self.base)
        self._tmp.cleanup()


class TestSilentStepIsNamed(Case):
    def test_every_step_appears_in_the_grouping_even_with_no_rows(self):
        audit.record("search", where=self.base, ok=False, output={"reason": "not_found"})
        grouped = audit_report.steps_report(self.base)
        self.assertEqual(list(grouped), list(audit.STEPS))
        for step in audit.STEPS:
            if step != "search":
                self.assertEqual(grouped[step], [], step)

    def test_the_text_says_so_in_words(self):
        audit.record("search", where=self.base)
        body = audit_report.text(audit_report.steps_report(self.base))
        self.assertIn("fact — не сработал ни разу", body)
        self.assertIn("link — не сработал ни разу", body)


class TestFullPrintsEverything(Case):
    def test_full_mode_keeps_the_entire_input_and_output(self):
        long_text = "дословный ответ модели " * 40
        audit.record("mark", where=self.base,
                     input={"replies": [long_text]},
                     output={"kept": 1, "dropped": {}})
        grouped = audit_report.steps_report(self.base)
        body = audit_report.text(grouped, full=True)
        self.assertIn(long_text.strip(), body)

    def test_without_full_a_limit_still_shows_the_true_count(self):
        for i in range(8):
            audit.record("fact", where=self.base, input={"n": i}, output={"n": i})
        grouped = audit_report.steps_report(self.base)
        body = audit_report.text(grouped, full=False, limit=3)
        self.assertIn("fact — 8 строк", body)
        self.assertIn("показаны первые 3", body)


class TestRunFilters(Case):
    def test_only_rows_of_the_named_run_are_grouped(self):
        with mock.patch.dict(os.environ, {"XMEM_RUN_ID": "прогон-A"}):
            audit.record("search", where=self.base, input={"who": "A"})
        with mock.patch.dict(os.environ, {"XMEM_RUN_ID": "прогон-B"}):
            audit.record("search", where=self.base, input={"who": "B"})
        grouped = audit_report.steps_report(self.base, run="прогон-A")
        self.assertEqual(len(grouped["search"]), 1)
        self.assertEqual(grouped["search"][0]["input"]["who"], "A")


class TestMainWritesToFile(Case):
    def test_out_flag_writes_the_report_to_a_file(self):
        audit.record("judge", where=self.base, output={"verdict": "yes"})
        out = Path(self._tmp.name) / "report.txt"
        code = audit_report.main(["--db", str(self.base), "--full", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        self.assertIn("judge", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
