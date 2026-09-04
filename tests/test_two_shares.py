#!/usr/bin/env python3
"""Две доли вместо одной: полнота у `apply`, точность у `avoid`.

Запуск: python3 -m pytest tests/test_two_shares.py -q

Одна доля («применила / всего») смешивала пары с разной целью. Пара, где
знание нужно вставить (`apply`), и пара, где его нужно придержать (`avoid`),
складывались в одно отношение — и задранная точность (соблазна нет, молчать
даром) прикрывала провал полноты, цифра «применила» держалась на нулевом
применении. Раздел на две доли и порог, проверяемый на каждой порознь
(`eval.pairs`, п. 3 задачи), устраняет ровно эту подмену.

Свойства:

1. `share_of_aim` — честное деление, порознь по `apply` и `avoid`. Пар цели
   не было — доли нет, а не ноль.
2. У `apply` считается только `bucket() == APPLIED`. У `avoid` считается и
   `APPLIED`, и `COINCIDED` — обе ветки, где `bucket()` говорит «ok»;
   `INTRUDED`/`WRONG_FED`/`UNUSED`/`NOT_FOUND`/`CUT`/`BROKEN` — нет.
3. `Bout.window_line()` называет обе доли и вердикт по каждой, порог общий
   (`THRESHOLD`), но проверяется на каждой доле отдельно — одна не может
   прикрыть другую.
4. `journal_row` несёт обе доли в строке журнала, каждая своим
   `passed`/`total`/`share`, без округления.

Мутации, на которых проверки обязаны краснеть:
  * `share_of_aim` смешивает `apply` и `avoid` в одно отношение → TestShareOfAim
  * `avoid` не засчитывает `COINCIDED`, только `APPLIED`           → TestAimPassed
  * `window_line` называет только одну долю или один вердикт        → TestWindowLineNamesBothShares
  * порог по одной доле решает вердикт другой                      → TestEachShareHasItsOwnVerdict
  * `journal_row` не несёт `apply`/`avoid` или округляет долю       → TestJournalCarriesBothShares
"""
import os
import unittest

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live

FAST = settings(deadline=None, max_examples=100)


def a_row(id, aim, ok=True, injected=True, intruded=False, error=None, reason=None):
    return {"id": id, "aim": aim, "ok": ok, "injected": injected,
           "intruded": intruded, "error": error, "reason": reason}


class FakeReport:
    def __init__(self, asked):
        self.asked = asked

    @property
    def passed(self):
        return sum(1 for row in self.asked if live.bucket(row) == live.APPLIED)

    @property
    def total(self):
        return len(self.asked)


# --- 1 и 2. доля порознь, свой признак успеха на каждую цель -----------------

class TestShareOfAim(unittest.TestCase):
    @given(passed=st.integers(min_value=0, max_value=15),
           extra=st.integers(min_value=0, max_value=15))
    @FAST
    def test_apply_share_is_honest_division(self, passed, extra):
        rows = ([a_row("p%d" % i, "apply", ok=True, injected=True) for i in range(passed)]
               + [a_row("f%d" % i, "apply", ok=False, injected=False) for i in range(extra)])
        report = FakeReport(rows)
        got = live.share_of_aim(report, "apply")
        total = passed + extra
        if total == 0:
            self.assertIsNone(got)
        else:
            self.assertEqual(got.passed, passed)
            self.assertEqual(got.total, total)
            self.assertEqual(got.ratio, passed / total)

    def test_no_pairs_of_this_aim_is_not_a_zero_share(self):
        report = FakeReport([a_row("а", "avoid", ok=True, injected=False)])
        self.assertIsNone(live.share_of_aim(report, "apply"))

    @given(rows=st.lists(st.tuples(
        st.sampled_from(("apply", "avoid")), st.booleans(), st.booleans()),
        min_size=1, max_size=20))
    @FAST
    def test_apply_and_avoid_never_share_a_denominator(self, rows):
        """Строка одной цели не входит в знаменатель доли другой цели."""
        built = [a_row("r%d" % i, aim, ok=ok, injected=injected)
                for i, (aim, ok, injected) in enumerate(rows)]
        report = FakeReport(built)
        apply_total = sum(1 for r in built if r["aim"] == "apply")
        avoid_total = sum(1 for r in built if r["aim"] == "avoid")
        apply_share = live.share_of_aim(report, "apply")
        avoid_share = live.share_of_aim(report, "avoid")
        self.assertEqual(apply_share.total if apply_share else 0, apply_total)
        self.assertEqual(avoid_share.total if avoid_share else 0, avoid_total)


class TestAimPassed(unittest.TestCase):
    """Свойство 2. Успех строки под свою цель — не один и тот же бакет."""

    def test_apply_only_counts_applied(self):
        applied = a_row("a", "apply", ok=True, injected=True)
        coincided = a_row("b", "apply", ok=True, injected=False)
        self.assertEqual(live.bucket(applied), live.APPLIED)
        self.assertEqual(live.bucket(coincided), live.COINCIDED)
        self.assertTrue(live.aim_passed(applied, "apply"))
        self.assertFalse(live.aim_passed(coincided, "apply"),
                         "совпадение без вброса не должно засчитываться в полноту")

    def test_avoid_counts_applied_and_coincided_alike(self):
        applied = a_row("a", "avoid", ok=True, injected=True)
        coincided = a_row("b", "avoid", ok=True, injected=False)
        self.assertEqual(live.bucket(applied), live.APPLIED)
        self.assertEqual(live.bucket(coincided), live.COINCIDED)
        self.assertTrue(live.aim_passed(applied, "avoid"))
        self.assertTrue(live.aim_passed(coincided, "avoid"),
                        "приплетать было нечего — это тоже удержание, а не провал")

    def test_avoid_intruded_never_passes(self):
        leaked = a_row("c", "avoid", ok=True, injected=True, intruded=True)
        self.assertEqual(live.bucket(leaked), live.INTRUDED)
        self.assertFalse(live.aim_passed(leaked, "avoid"))

    @given(row=st.builds(a_row, id=st.just("x"), aim=st.just("apply"),
                         ok=st.booleans(), injected=st.booleans(),
                         intruded=st.booleans(),
                         reason=st.one_of(st.none(),
                                          st.sampled_from(live.CUT_REASONS))))
    @FAST
    def test_aim_passed_agrees_with_bucket_for_every_row(self, row):
        applied = live.bucket(row) == live.APPLIED
        coincided = live.bucket(row) == live.COINCIDED
        self.assertEqual(live.aim_passed(row, "apply"), applied)
        self.assertEqual(live.aim_passed(row, "avoid"), applied or coincided)


# --- 3. отчёт называет обе доли -----------------------------------------------

class TestWindowLineNamesBothShares(unittest.TestCase):
    def test_apply_and_avoid_pairs_both_get_a_line(self):
        rows = [a_row("применил", "apply", ok=True, injected=True),
               a_row("удержал", "avoid", ok=True, injected=False)]
        bout = live.Bout({"memory": FakeReport(rows)})
        text = bout.window_line()
        self.assertIn("полнота", text)
        self.assertIn("точность", text)

    def test_a_missing_aim_says_no_pairs_not_a_number(self):
        rows = [a_row("применил", "apply", ok=True, injected=True)]
        bout = live.Bout({"memory": FakeReport(rows)})
        text = bout.window_line()
        self.assertIn("пар не было", text)
        self.assertNotIn("None", text)


class TestEachShareHasItsOwnVerdict(unittest.TestCase):
    """Свойство 3 (продолжение). Провал одной доли не красит другую и наоборот."""

    def test_low_completeness_does_not_hide_behind_high_precision(self):
        rows = ([a_row("miss%d" % i, "apply", ok=False, injected=False)
                for i in range(9)]
               + [a_row("hit", "apply", ok=True, injected=True)]
               + [a_row("held%d" % i, "avoid", ok=True, injected=False)
                 for i in range(9)])
        bout = live.Bout({"memory": FakeReport(rows)})
        text = bout.window_line()
        lines = text.splitlines()
        completeness = next(l for l in lines if l.startswith("полнота"))
        precision = next(l for l in lines if l.startswith("точность"))
        self.assertIn("ниже порога", completeness)
        self.assertIn("в окне", precision)

    def test_low_precision_does_not_hide_behind_high_completeness(self):
        rows = ([a_row("hit%d" % i, "apply", ok=True, injected=True)
                for i in range(9)]
               + [a_row("leak", "avoid", ok=True, injected=True, intruded=True)])
        bout = live.Bout({"memory": FakeReport(rows)})
        text = bout.window_line()
        lines = text.splitlines()
        completeness = next(l for l in lines if l.startswith("полнота"))
        precision = next(l for l in lines if l.startswith("точность"))
        self.assertIn("в окне", completeness)
        self.assertIn("ниже порога", precision)


# --- 4. строка журнала несёт обе доли ------------------------------------------

class TestJournalCarriesBothShares(unittest.TestCase):
    @given(passed=st.integers(min_value=0, max_value=10),
           extra=st.integers(min_value=0, max_value=10),
           held=st.integers(min_value=0, max_value=10))
    @FAST
    def test_apply_and_avoid_shares_land_in_the_journal_row_unrounded(
            self, passed, extra, held):
        from collections import OrderedDict
        rows = ([a_row("p%d" % i, "apply", ok=True, injected=True)
                for i in range(passed)]
               + [a_row("f%d" % i, "apply", ok=False, injected=False)
                 for i in range(extra)]
               + [a_row("h%d" % i, "avoid", ok=True, injected=False)
                 for i in range(held)])
        played = OrderedDict(memory=FakeReport(rows))
        # journal_row спрашивает report.cost/report.voice/report.probe тоже.
        played["memory"].cost = 0.0
        played["memory"].voice = "opora"
        played["memory"].probe = {}
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=len(rows))
        apply_field = line["arms"]["memory"]["apply"]
        avoid_field = line["arms"]["memory"]["avoid"]
        apply_total = passed + extra
        if apply_total == 0:
            self.assertIsNone(apply_field["share"])
        else:
            self.assertEqual(apply_field["passed"], passed)
            self.assertEqual(apply_field["total"], apply_total)
            self.assertEqual(apply_field["share"], passed / apply_total)
            self.assertNotIsInstance(apply_field["share"], int)
        if held == 0:
            self.assertIsNone(avoid_field["share"])
        else:
            self.assertEqual(avoid_field["passed"], held)
            self.assertEqual(avoid_field["total"], held)
            self.assertEqual(avoid_field["share"], 1.0)


if __name__ == "__main__":
    unittest.main()
