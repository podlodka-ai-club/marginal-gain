#!/usr/bin/env python3
"""Порог 70%: отчёт говорит прямо, в окне доля руки с памятью или нет.

Запуск: python3 -m pytest tests/test_threshold.py -q

Правило работы сменилось: раньше набор рос до первого провала и вставал,
теперь — до тех пор, пока доля руки с памятью не ниже 70%. Порог без цифры в
самом отчёте бесполезен: «4 из 5» само по себе не говорит, в окне мы или нет,
и сверять пришлось бы вручную каждый раз.

Свойства:

1. Доля — честное `passed / total`. Пар не было — доли нет, а не ноль:
   ноль читался бы как «вышли из окна», хотя мы просто ничего не мерили.
2. В окне — доля не ниже 0.7, включая саму границу.
3. Отчёт (`Bout.text()`) называет долю руки с памятью и говорит словами, в
   окне она или нет.
4. Порог живёт одним значением в коде и не берётся ключом командной строки.
5. Голая рука на вердикт не влияет — окно считается только по руке с памятью.

Мутации, на которых проверки обязаны краснеть:
  * `>` вместо `>=` на границе порога              → TestInWindow
  * `passed / (total or 1)` вместо `None` на нуле   → TestShareOf
  * отчёт не называет долю или вердикт              → TestTheReportNamesTheShare
  * порог читается из окружения/аргументов          → TestTheThresholdIsOneConstant
  * вердикт считается по голой руке, а не по памяти  → TestTheBareArmDoesNotSkewTheVerdict
"""
import os
import re
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live

ROOT = Path(__file__).resolve().parent.parent
FAST = settings(deadline=None, max_examples=100)


def a_row(id, ok):
    return {"id": id, "aim": "apply", "ok": ok, "injected": ok,
           "intruded": False, "error": None, "reason": None}


class FakeReport:
    """Рука: `passed` строк применили факт, `total - passed` — нет. Все `apply`.

    Ряды строятся, а не хранятся отдельным полем: `Bout.window_line` теперь
    считает долю по `report.asked` (`live.share_of_aim`), и фейку нужно нести
    то же самое сырьё, что несёт настоящий отчёт, а не подставное число.
    """

    def __init__(self, passed, total):
        self.asked = ([a_row("p%d" % i, True) for i in range(passed)]
                     + [a_row("f%d" % i, False) for i in range(total - passed)])
        self.root = Path("/тут/песочница")
        self.probe = {}

    @property
    def passed(self):
        return sum(1 for row in self.asked if live.bucket(row) == live.APPLIED)

    @property
    def total(self):
        return len(self.asked)

    def text(self):
        return "итог: %d из %d" % (self.passed, self.total)


# --- 1. доля ------------------------------------------------------------------

class TestShareOf(unittest.TestCase):
    @given(passed=st.integers(min_value=0, max_value=50),
           extra=st.integers(min_value=0, max_value=50))
    @FAST
    def test_share_is_honest_division(self, passed, extra):
        total = passed + extra
        report = FakeReport(passed, total)
        got = live.share_of(report)
        if total == 0:
            self.assertIsNone(got)
        else:
            self.assertEqual(got, passed / total)

    def test_no_pairs_is_not_a_zero_share(self):
        self.assertIsNone(live.share_of(FakeReport(0, 0)))


# --- 2. окно --------------------------------------------------------------

class TestInWindow(unittest.TestCase):
    def test_the_threshold_itself_is_inside_the_window(self):
        """Ровно порог — ещё в окне, не только строго выше него."""
        self.assertTrue(live.in_window(live.THRESHOLD))

    @given(share=st.floats(min_value=0.0, max_value=0.6999, allow_nan=False))
    @FAST
    def test_below_the_threshold_is_out(self, share):
        self.assertFalse(live.in_window(share))

    @given(share=st.floats(min_value=0.7, max_value=1.0, allow_nan=False))
    @FAST
    def test_at_or_above_the_threshold_is_in(self, share):
        self.assertTrue(live.in_window(share))

    def test_no_share_is_not_in_the_window(self):
        """Нет доли — не в окне, но и не «провал»: отдельно от нуля."""
        self.assertFalse(live.in_window(None))

    @given(passed=st.integers(min_value=0, max_value=50),
           total=st.integers(min_value=1, max_value=50))
    @FAST
    def test_agrees_with_share_of_on_real_reports(self, passed, total):
        passed = min(passed, total)
        report = FakeReport(passed, total)
        expect = (passed / total) >= live.THRESHOLD
        self.assertEqual(live.in_window(live.share_of(report)), expect)


# --- 3. отчёт называет долю и вердикт -----------------------------------------

class TestTheReportNamesTheShare(unittest.TestCase):
    @given(passed=st.integers(min_value=0, max_value=20),
           extra=st.integers(min_value=0, max_value=20))
    @FAST
    def test_the_bout_prints_the_share_and_a_verdict(self, passed, extra):
        total = passed + extra
        if total == 0:
            return
        bout = live.Bout({"memory": FakeReport(passed, total)})
        text = bout.text()
        self.assertIn("%d" % passed, text)
        self.assertIn("%d" % total, text)
        share = passed / total
        if share >= live.THRESHOLD:
            self.assertIn("в окне", text)
        else:
            self.assertNotIn("в окне", text)

    def test_zero_pairs_still_says_something_not_a_bare_number(self):
        bout = live.Bout({"memory": FakeReport(0, 0)})
        text = bout.text()
        self.assertNotIn("None", text)

    def test_a_bare_only_bout_names_no_memory_verdict(self):
        """Нет руки с памятью — про окно отчёт молчит: мерить нечего."""
        bout = live.Bout({"bare": FakeReport(3, 5)})
        text = bout.text()
        self.assertNotIn("в окне", text)
        self.assertNotIn("ниже порога", text)


# --- 4. порог — одна константа, не ключ ---------------------------------------

class TestTheThresholdIsOneConstant(unittest.TestCase):
    def test_the_value_is_seventy_percent(self):
        self.assertEqual(live.THRESHOLD, 0.7)

    def test_no_cli_flag_names_the_threshold(self):
        names = {s for a in live.parser()._actions for s in a.option_strings}
        self.assertFalse(any("threshold" in n or "порог" in n for n in names),
                         names)

    def test_the_literal_appears_exactly_once_in_the_source(self):
        """`0.7` в коде — только там, где объявлена константа."""
        source = (ROOT / "eval" / "live.py").read_text(encoding="utf-8")
        hits = re.findall(r"(?<![\d.])0\.7(?![\d])", source)
        self.assertEqual(len(hits), 1,
                         "0.7 встречается %d раз(а) — порог не в одном месте"
                         % len(hits))


# --- 5. голая рука не влияет на вердикт ---------------------------------------

class TestTheBareArmDoesNotSkewTheVerdict(unittest.TestCase):
    @given(memory_passed=st.integers(min_value=0, max_value=10),
           bare_passed=st.integers(min_value=0, max_value=10),
           total=st.integers(min_value=1, max_value=10))
    @FAST
    def test_the_verdict_only_looks_at_memory(self, memory_passed, bare_passed,
                                              total):
        memory_passed = min(memory_passed, total)
        bare_passed = min(bare_passed, total)
        alone = live.Bout({"memory": FakeReport(memory_passed, total)})
        both = live.Bout({"memory": FakeReport(memory_passed, total),
                          "bare": FakeReport(bare_passed, total)})
        in_alone = "в окне" in alone.text()
        in_both = "в окне" in both.text()
        self.assertEqual(in_alone, in_both,
                         "рука без памяти изменила вердикт по окну")


if __name__ == "__main__":
    unittest.main()
