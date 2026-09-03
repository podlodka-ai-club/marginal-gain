#!/usr/bin/env python3
"""Журнал прогонов: строка на прогон, всегда, никогда не перезаписью.

Запуск: python3 -m pytest tests/test_journal.py -q

Без журнала порог не работает: «4 из 5» само по себе не говорит, шум это или
съезд, потому что снять его не с чем сравнить. Логи прогона жили только в
песочнице исполнителя (умирали вместе с ней), а `--out` писал один файл
перезаписью и без шапки — ни модели, ни формы вброса, ни даты, ни трат.

Свойства:

1. Журнал пишется всегда, без ключа: вызов без `--journal` всё равно кладёт
   строку в файл по умолчанию, лежащий в репозитории (не в песочнице прогона).
2. Каждый вызов — это `append`, никогда не перезапись: вторая строка ложится
   поверх первой, а не вместо неё.
3. Строка несёт всё, чем цифры отличаются друг от друга: время (UTC), модель,
   проигрыватель, форму вброса, файл набора и число пар в нём, по каждой
   паре — id, aim, исход, ступень обрыва, и по каждой руке — счёт, долю,
   траты.
4. Цифры в строке точные: доля — честное `passed / total`, без округления,
   которое подвинуло бы её через порог.
5. `--journal` меняет только путь. Рубильника, отключающего запись, нет.

Мутации, на которых проверки обязаны краснеть:
  * писать журнал только когда передан `--journal`      → TestTheJournalIsAlwaysWritten
  * открывать файл журнала на запись ("w") вместо "a"    → TestEveryRunAppendsALine
  * не прокинуть модель/форму/файл набора в строку       → TestTheLineCarriesWhatMakesNumbersComparable
  * не прокинуть исход или ступень обрыва по паре        → TestTheLineCarriesWhatMakesNumbersComparable
  * округлить долю (round/int) вместо честного деления   → TestTheShareIsExact
  * завернуть запись журнала в `if args.journal:`        → TestMainAlwaysAppendsTheJournal
"""
import ast
import json
import os
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live

ROOT = Path(__file__).resolve().parent.parent

FAST = settings(deadline=None, max_examples=50)


# --- сырьё --------------------------------------------------------------

class FakeReport:
    """Отчёт руки, из которого журналу нужны цифры, траты и цепочка."""

    def __init__(self, asked, cost=0.0, voice="opora", probe=None):
        self.asked = asked
        self.cost = cost
        self.voice = voice
        self.probe = probe or {}

    @property
    def total(self):
        return len(self.asked)

    @property
    def passed(self):
        return sum(1 for row in self.asked if live.bucket(row) == live.APPLIED)


def a_row(id="пара", aim="apply", ok=True, injected=True, intruded=False,
         error=None, reason=None):
    return {"id": id, "aim": aim, "ok": ok, "injected": injected,
            "intruded": intruded, "error": error, "reason": reason}


ROWS = st.builds(
    a_row,
    id=st.text(alphabet="абвгдежзийклмноп", min_size=1, max_size=8),
    aim=st.sampled_from(("apply", "avoid")),
    ok=st.booleans(), injected=st.booleans(), intruded=st.booleans(),
)


# --- 1 и 5. журнал пишется всегда, ключ меняет только путь -------------------

class TestTheJournalIsAlwaysWritten(unittest.TestCase):
    """Свойство 1 и 5. Без `--journal` пишем в путь по умолчанию, не молчим."""

    def test_the_default_journal_lives_in_the_repository(self):
        """Путь по умолчанию — в репозитории, не в песочнице прогона."""
        self.assertTrue(live.DEFAULT_JOURNAL.is_relative_to(live.ROOT))

    def test_the_flag_only_overrides_the_path(self):
        args = live.parser().parse_args(["--pairs", "нет.json"])
        self.assertIsNone(args.journal)
        args = live.parser().parse_args(["--pairs", "нет.json",
                                         "--journal", "свой.jsonl"])
        self.assertEqual(args.journal, "свой.jsonl")

    def test_there_is_no_switch_to_turn_the_journal_off(self):
        """Ни одного ключа с именем вроде `--no-journal` не заведено."""
        names = {s for a in live.parser()._actions for s in a.option_strings}
        self.assertFalse(any("no-journal" in n or "no_journal" in n
                             for n in names), names)


# --- 2. append, не перезапись ------------------------------------------------

class TestEveryRunAppendsALine(unittest.TestCase):
    """Свойство 2. Вторая строка ложится поверх первой."""

    @given(rows=st.lists(st.dictionaries(
        st.text(alphabet="ab", min_size=1, max_size=4),
        st.integers(), max_size=4), min_size=1, max_size=6))
    @FAST
    def test_n_calls_leave_n_lines_in_order(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            for row in rows:
                live.append_journal(path, row)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), len(rows))
            self.assertEqual([json.loads(line) for line in lines], rows)

    def test_a_second_call_does_not_touch_the_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            live.append_journal(path, {"n": 1})
            first = path.read_text(encoding="utf-8")
            live.append_journal(path, {"n": 2})
            got = path.read_text(encoding="utf-8")
            self.assertTrue(got.startswith(first),
                            "первая строка изменилась вторым вызовом")
            self.assertEqual(len(got.splitlines()), 2)

    def test_the_journal_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "глубже" / "ещё" / "journal.jsonl"
            live.append_journal(path, {"n": 1})
            self.assertTrue(path.exists())

    def test_the_write_holds_an_exclusive_lock(self):
        """Запись берёт `LOCK_EX` и отпускает `LOCK_UN` — не гонкой по времени.

        Строка журнала растёт с числом пар (по паре — id, aim, исход,
        ступень), и `write()` большой строки не гарантированно один системный
        вызов даже под `O_APPEND`: без замка конкурентные писатели дали бы
        файл с перемешанными байтами. Гонку по реальному времени в юнит-тесте
        не собрать надёжно — GIL и малый размер страницы чаще прячут её, чем
        показывают, — поэтому проверяется само поведение: вызов оборачивает
        запись в захват и снятие замка на переданном файле, в этом порядке.
        """
        from unittest import mock

        calls = []
        real_flock = live.fcntl.flock

        def spy(fh, op):
            calls.append(op)
            return real_flock(fh, op)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            with mock.patch.object(live.fcntl, "flock", side_effect=spy):
                live.append_journal(path, {"n": 1})
        self.assertEqual(calls, [live.fcntl.LOCK_EX, live.fcntl.LOCK_UN],
                         "запись не берёт эксклюзивный замок на весь `write`")


# --- 3. состав строки ---------------------------------------------------------

class TestTheLineCarriesWhatMakesNumbersComparable(unittest.TestCase):
    """Свойство 3. Всё, без чего цифра прогона несравнима со следующей."""

    @given(model=st.text(alphabet="haiku-sonnetXYZ", min_size=1, max_size=12),
           player=st.sampled_from(("claude", "replay")),
           pairs_count=st.integers(min_value=0, max_value=50))
    @FAST
    def test_the_header_fields_are_present(self, model, player, pairs_count):
        played = OrderedDict(memory=FakeReport([a_row()]))
        row = live.journal_row(played, player=player, model=model,
                               pairs_file="набор.json", pairs_count=pairs_count)
        self.assertIn("ts", row)
        self.assertEqual(row["model"], model)
        self.assertEqual(row["player"], player)
        self.assertEqual(row["pairs_file"], "набор.json")
        self.assertEqual(row["pairs_count"], pairs_count)
        self.assertEqual(row["voice"], "opora")

    def test_the_timestamp_is_utc_and_parses(self):
        played = OrderedDict(memory=FakeReport([]))
        row = live.journal_row(played, player="claude", model="haiku",
                               pairs_file="n.json", pairs_count=0)
        self.assertTrue(row["ts"].endswith("Z"), row["ts"])
        from datetime import datetime
        datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))

    @given(rows=st.lists(ROWS, min_size=1, max_size=6, unique_by=lambda r: r["id"]))
    @FAST
    def test_every_pair_row_carries_its_id_aim_outcome_and_break(self, rows):
        report = FakeReport(rows)
        played = OrderedDict(memory=report)
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=len(rows))
        by_id = {p["id"]: p for p in line["arms"]["memory"]["pairs"]}
        self.assertEqual(set(by_id), {r["id"] for r in rows})
        for row in rows:
            entry = by_id[row["id"]]
            self.assertEqual(entry["aim"], row["aim"])
            self.assertEqual(entry["outcome"], live.bucket(row))
            self.assertEqual(entry["break"], "")   # без цепочки — прочерком

    def test_the_break_step_comes_from_the_chain_when_it_is_known(self):
        row = a_row(id="город", ok=False, injected=True)
        probe = {"marked": True, "facts": 1, "candidates": 3,
                 "injected": True, "ok": False, "expected": True}
        report = FakeReport([row], probe={"город": probe})
        played = OrderedDict(memory=report)
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=1)
        entry = line["arms"]["memory"]["pairs"][0]
        self.assertEqual(entry["break"], live.break_of(probe))

    def test_arms_are_kept_apart_in_the_journal_too(self):
        played = OrderedDict(memory=FakeReport([a_row(ok=True, injected=True)],
                                               cost=0.12),
                             bare=FakeReport([a_row(ok=False, injected=False)],
                                            cost=0.05))
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=1)
        self.assertEqual(set(line["arms"]), {"memory", "bare"})
        self.assertEqual(line["arms"]["memory"]["cost_usd"], 0.12)
        self.assertEqual(line["arms"]["bare"]["cost_usd"], 0.05)
        self.assertEqual(line["arms"]["memory"]["passed"], 1)
        self.assertEqual(line["arms"]["bare"]["passed"], 0)


# --- 4. доля точная, без округления в свою пользу ----------------------------

class TestTheShareIsExact(unittest.TestCase):
    """Свойство 4. Доля — честное деление, не округление."""

    @given(passed=st.integers(min_value=0, max_value=20),
           extra=st.integers(min_value=0, max_value=20))
    @FAST
    def test_the_share_is_passed_over_total_with_no_rounding(self, passed, extra):
        total = passed + extra
        rows = ([a_row(id="p%d" % i, ok=True, injected=True) for i in range(passed)]
               + [a_row(id="f%d" % i, ok=False, injected=False) for i in range(extra)])
        played = OrderedDict(memory=FakeReport(rows))
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=total)
        share = line["arms"]["memory"]["share"]
        if total == 0:
            self.assertIsNone(share)
        else:
            self.assertEqual(share, passed / total)
            # Не округлено: значение с плавающей точкой, а не готовый процент.
            self.assertNotIsInstance(share, int)

    def test_an_empty_arm_reports_no_share_not_zero(self):
        played = OrderedDict(memory=FakeReport([]))
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=0)
        self.assertIsNone(line["arms"]["memory"]["share"])


# --- журнал в main() ----------------------------------------------------------

class TestMainAlwaysAppendsTheJournal(unittest.TestCase):
    """`main` дописывает журнал не под условием отдельного ключа.

    Проверка статическая, а не живым прогоном: `live.main` поднимает стенд, а
    его в быстрой батарее поднимать нельзя (`tests/test_suite_shape.py`).
    Смотрим на разбор, как и `test_model_header.TestTheRunThreadsTheModel`.
    """

    def _main_tree(self):
        tree = ast.parse((ROOT / "eval" / "live.py").read_text(encoding="utf-8"))
        return next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "main")

    def test_main_calls_append_journal(self):
        main = self._main_tree()
        calls = [node for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "append_journal"]
        self.assertTrue(calls, "main() не зовёт append_journal вовсе")

    def test_the_call_is_not_gated_behind_a_journal_flag_check(self):
        """Запись не завёрнута в `if args.journal:` — тогда без ключа тишина."""
        main = self._main_tree()

        class Guarded(ast.NodeVisitor):
            def __init__(self):
                self.stack = []
                self.hit = []

            def visit_If(self, node):
                self.stack.append(ast.unparse(node.test))
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                if getattr(node.func, "id", None) == "append_journal":
                    self.hit.append(list(self.stack))
                self.generic_visit(node)

        visitor = Guarded()
        visitor.visit(main)
        self.assertTrue(visitor.hit, "main() не зовёт append_journal вовсе")
        for stack in visitor.hit:
            gating = [test for test in stack
                     if "journal" in test and "played" not in test]
            self.assertEqual(gating, [],
                             "запись журнала спрятана за условием ключа: %s"
                             % gating)


if __name__ == "__main__":
    unittest.main()
