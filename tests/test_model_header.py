#!/usr/bin/env python3
"""Имя модели в шапке отчёта.

Запуск: python3 -m pytest tests/test_model_header.py -q

Цифры, снятые на разных моделях, не сравниваются никогда — ровно так же, как
цифры разных форм вброса не сравниваются между собой (`test_voice.py`). Форма
вброса и проигрыватель уже называют себя в шапке отчёта; модель хода — нет, и
поэтому цифра, снятая на дорогой модели, неотличима от цифры, снятой на
дешёвой, если смотреть только на отчёт.

Мутации, на которых проверки обязаны краснеть:
  * `Report` не принимает модель вовсе, либо принимает и молчит о ней
        → TestTheReportNamesItsModel
  * модель не назвали — строка пропадает вместо «не назван»
        → TestNoModelIsNotSilentlyMissing
  * `run` не пробрасывает модель в отчёт (печатает не ту, что играла)
        → TestTheRunThreadsTheModel
"""
import ast
import os
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live

ROOT = Path(__file__).resolve().parent.parent

FAST = settings(deadline=None, max_examples=50)

MODEL_NAMES = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126,
                           blacklist_characters="%"),
    min_size=1, max_size=20)


class TestTheReportNamesItsModel(unittest.TestCase):
    """Отчёт называет модель хода рядом с проигрывателем и формой вброса."""

    @given(model=MODEL_NAMES)
    @FAST
    def test_the_report_says_which_model_it_ran(self, model):
        box = live.Sandbox(root="/tmp/не-открывается")
        report = live.Report(box, live.Agent.name, [], model=model)
        self.assertIn("модель: %s" % model, report.text())

    @given(model=MODEL_NAMES)
    @FAST
    def test_the_model_line_sits_in_the_header_block(self, model):
        """Строка модели — в той же группе, что проигрыватель и форма вброса.

        Не в подвале отчёта после разбивки по исходам: там её при беглом
        чтении шапки не найти, а именно шапку сверяют, решая, сравнима ли
        цифра с прошлым прогоном.
        """
        box = live.Sandbox(root="/tmp/не-открывается")
        report = live.Report(box, live.Agent.name, [], model=model)
        lines = report.text().splitlines()
        header = lines[:lines.index("")] if "" in lines else lines
        self.assertTrue(any(line.startswith("модель: ") for line in header),
                        "модель не в шапке отчёта: %s" % header)


class TestNoModelIsNotSilentlyMissing(unittest.TestCase):
    """Модель не назвали — отчёт обязан сказать это вслух, а не промолчать.

    Молча пропавшая строка неотличима от забытого пробрасывания: то же
    правило, каким в этом отчёте уже помечены неснимаемые исходы прочерком с
    причиной.
    """

    def test_without_a_model_the_report_still_names_it(self):
        box = live.Sandbox(root="/tmp/не-открывается")
        report = live.Report(box, live.Agent.name, [], model=None)
        self.assertIn("модель: не назван", report.text())

    def test_the_default_report_construction_still_carries_a_model_line(self):
        """Старый вызов без `model=` не роняет отчёт и не теряет строку."""
        box = live.Sandbox(root="/tmp/не-открывается")
        report = live.Report(box, live.Agent.name, [])
        self.assertIn("модель: не назван", report.text())


class TestTheRunThreadsTheModel(unittest.TestCase):
    """`run` отдаёт отчёту ту модель, которой играл, а не какую-то свою."""

    def test_run_passes_model_to_report_by_keyword(self):
        """Статическая проверка: `run` зовёт `Report(..., model=model)`.

        Ловит и «параметр есть, но никуда не идёт», и «зовём Report без
        ключа, а после руками путаем поля» — оба выглядели бы как рабочий
        код, но модель, напечатанная отчётом, была бы не той, что играла.
        """
        tree = ast.parse((ROOT / "eval" / "live.py").read_text(encoding="utf-8"))
        run_body = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "run")
        calls = [node for node in ast.walk(run_body)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "Report"]
        self.assertTrue(calls, "run() не зовёт Report вовсе")
        passed = {kw.arg: ast.unparse(kw.value)
                 for call in calls for kw in call.keywords}
        self.assertEqual(passed.get("model"), "model",
                         "run() зовёт Report(model=...) не своим параметром "
                         "model: %s" % passed)


if __name__ == "__main__":
    unittest.main()
