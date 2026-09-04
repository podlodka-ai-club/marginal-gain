#!/usr/bin/env python3
"""Провал делится на «дали не то» и «отдала, не применил» — не один ярлык.

Запуск: python3 -m pytest tests/test_wrong_fed.py -q

Раньше оба класса были одним исходом `UNUSED` («отдала, не применил»): он
значил буквально «вброс состоялся, а ожидаемого в ответе нет», и не различал,
был ли нужный факт вообще во вбросе. Живой прогон это спутал на паре «макбук»:
вбросили диагональ экрана и дату вместо модели M5, а строка отчёта в той же
строке писала «вброс: да» рядом с тем же ярлыком, что и у пары, которой факт
дали верно, но агент не применил.

Признак `expect_in_feed` разводит их. Считается той же строчной проверкой
(вхождение подстроки без разбора регистра), какой `evaluate.judge` судит сам
ответ, — не своей копией: разойдись они, признак и исход мерили бы разными
линейками, и один не объяснял бы другой.

Свойства:

1. `bucket()`: провал с вбросом и `expect_in_feed is False` — `WRONG_FED`;
   тот же провал с `True` или `None` (признак не считали или не к чему) —
   по-прежнему `UNUSED`. Остальные ветви (`ok`, `error`, `intruded`,
   молчание) признаком не затронуты вовсе.
2. `judge_one`: `expect_in_feed` — `True` тогда и только тогда, когда каждое
   слово `expect` встречается (без учёта регистра) в тексте, который
   действительно дошёл до агента, и это то же самое вхождение, каким считаются
   `hits` в ответе. У пары без `expect` (в т.ч. отрицательной) признак — `None`,
   не `False`: спрашивать было не о чем, и `False` обвинило бы вброс безосновательно.
3. `pair_row`/`journal_row`: признак ложится в строку журнала рядом с исходом,
   а не отдельно и не вместо него.
4. Старая строка журнала (без поля `expect_in_feed`) не роняет ни `bucket()`,
   ни сводку — поле родилось вместе с этой задачей, и дозаполнять его задним
   числом нельзя, но читать такую строку возможным быть обязано.

Мутации, на которых проверки обязаны краснеть:
  * вернуть `UNUSED` всегда, не разбирая `expect_in_feed`         → TestBucketSplitsTheOldLabel
  * сравнивать `expect_in_feed` с `answer`, а не с тем, что вброшено
                                                                   → TestExpectInFeedMatchesWhatReachedTheAgent
  * не прокинуть `expect_in_feed` в строку журнала                → TestTheJournalRowCarriesTheFlag
  * упасть на строке журнала без нового поля                      → TestOldJournalRowsStillWork
"""
import os
import sqlite3
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger
from eval import live
from storage import db

FAST = settings(deadline=None, max_examples=100)

WORDS = st.text(alphabet="абвгдеowsянкаМacbookM5казань", min_size=1, max_size=10)


# --- сырьё --------------------------------------------------------------

def a_row(id="пара", aim="apply", ok=False, injected=True, intruded=False,
         error=None, reason=None, expect_in_feed=None):
    row = {"id": id, "aim": aim, "ok": ok, "injected": injected,
          "intruded": intruded, "error": error, "reason": reason}
    if expect_in_feed is not None:
        row["expect_in_feed"] = expect_in_feed
    return row


def seed_injection(base, session_id, content, at="2026-01-01T00:00:00+00:00"):
    """Кладёт запись о вбросе прямо в базу — то, что читает `given_to`."""
    conn = db.connect(base)
    db.migrate(conn)
    conn.execute(
        'INSERT INTO memoryinjection (session_id, injected_at, injected_content) '
        'VALUES (?, ?, ?)', (session_id, at, content))
    conn.commit()
    conn.close()


def mark_injected(state_dir, session_id, at="2026-01-01T00:00:00Z"):
    """Отмечает вброс в ленте — то, что читает `verdict_of` (поле `injected`)."""
    log = Path(state_dir) / "ledger.jsonl"
    ledger.injected(session_id, at, log=log)


# --- 1. bucket() делит провал на два класса ----------------------------------

class TestBucketSplitsTheOldLabel(unittest.TestCase):
    """Свойство 1. `WRONG_FED` и `UNUSED` — по `expect_in_feed`, не иначе."""

    def test_injected_and_not_ok_and_wrong_feed_is_the_new_bucket(self):
        row = a_row(ok=False, injected=True, expect_in_feed=False)
        self.assertEqual(live.bucket(row), live.WRONG_FED)

    def test_injected_and_not_ok_and_right_feed_stays_unused(self):
        row = a_row(ok=False, injected=True, expect_in_feed=True)
        self.assertEqual(live.bucket(row), live.UNUSED)

    def test_injected_and_not_ok_and_unknown_feed_stays_unused(self):
        """`None` — «не считали» (отрицательная пара, нет expect), не «дали не то»."""
        row = a_row(ok=False, injected=True)
        self.assertNotIn("expect_in_feed", row)
        self.assertEqual(live.bucket(row), live.UNUSED)

    @given(expect_in_feed=st.one_of(st.none(), st.booleans()))
    @FAST
    def test_the_flag_never_changes_a_successful_verdict(self, expect_in_feed):
        """Признак решает только среди провалов — удачу он не трогает."""
        row = a_row(ok=True, injected=True, expect_in_feed=expect_in_feed)
        self.assertEqual(live.bucket(row), live.APPLIED)

    @given(expect_in_feed=st.one_of(st.none(), st.booleans()))
    @FAST
    def test_the_flag_never_matters_without_an_injection(self, expect_in_feed):
        row = a_row(ok=False, injected=False, reason="not_found",
                   expect_in_feed=expect_in_feed)
        self.assertEqual(live.bucket(row), live.NOT_FOUND)

    @given(expect_in_feed=st.one_of(st.none(), st.booleans()))
    @FAST
    def test_the_flag_never_overrides_error_or_intrusion(self, expect_in_feed):
        broken = a_row(ok=False, injected=True, error="boom",
                       expect_in_feed=expect_in_feed)
        self.assertEqual(live.bucket(broken), live.BROKEN)
        intruded = a_row(ok=False, injected=True, intruded=True,
                         expect_in_feed=expect_in_feed)
        self.assertEqual(live.bucket(intruded), live.INTRUDED)

    @given(expect_in_feed=st.one_of(st.none(), st.booleans()))
    @FAST
    def test_wrong_fed_is_a_subset_of_the_old_unused_definition(self, expect_in_feed):
        """Новый класс не расширяет условие: `WRONG_FED` или `UNUSED`, третьего нет
        среди провалов с вбросом — ровно та пара, что раньше сливалась в один."""
        row = a_row(ok=False, injected=True, expect_in_feed=expect_in_feed)
        self.assertIn(live.bucket(row), (live.WRONG_FED, live.UNUSED))


# --- пример из задачи: макбук ------------------------------------------------

class TestTheMacbookRegressionFromTheTask(unittest.TestCase):
    """Ровно случай задачи: вбросили диагональ и дату, не модель M5."""

    def test_wrong_content_injected_is_told_apart_from_ignored_content(self):
        wrong_content = a_row(id="макбук", ok=False, injected=True,
                              expect_in_feed=False)
        ignored_content = a_row(id="макбук", ok=False, injected=True,
                                expect_in_feed=True)
        self.assertEqual(live.bucket(wrong_content), live.WRONG_FED)
        self.assertEqual(live.bucket(ignored_content), live.UNUSED)
        self.assertNotEqual(live.bucket(wrong_content), live.bucket(ignored_content))


# --- 2. expect_in_feed — та же строчная проверка, что судит ответ -----------

class TestExpectInFeedMatchesWhatReachedTheAgent(unittest.TestCase):
    """Свойство 2. Признак смотрит на вброшенный текст, не на ответ агента."""

    def _judge(self, tmp, expect, fed_text, said="неважно что ответил агент"):
        base = Path(tmp) / "memory.db"
        seed_injection(base, "talk-1", fed_text)
        state = Path(tmp) / "state"
        state.mkdir()
        mark_injected(state, "talk-1")
        box = mock.Mock(db=base, state=state)
        pair = {"id": "p", "aim": "apply", "task": {"say": "вопрос"},
               "expect": expect, "forbid": []}
        reply = live.Reply(text=said, session_id="talk-1", cost=0.0, error=None)
        return live.judge_one(box, pair, reply)

    @given(word=WORDS)
    @FAST
    def test_present_in_the_feed_sets_the_flag_true(self, word):
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, [word], "текст вброса: %s тут" % word)
            self.assertTrue(row["expect_in_feed"], row)

    @given(word=WORDS, other=WORDS)
    @FAST
    def test_absent_from_the_feed_sets_the_flag_false(self, word, other):
        fed = "текст вброса: %s тут" % other
        if not word.strip() or word.lower() in fed.lower():
            return   # само слово всё же встретилось (в шаблоне или в `other`)
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, [word], fed)
            self.assertFalse(row["expect_in_feed"], row)

    def test_case_is_ignored_exactly_as_the_answer_check_ignores_it(self):
        """Та же строчная проверка: регистр не важен ни там, ни там."""
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, ["MacBook"], "речь про macbook pro")
            self.assertTrue(row["expect_in_feed"])

    def test_a_pair_without_expect_never_gets_the_flag_false(self):
        """Отрицательная пара (`expect` пуст) — признак `None`, не `False`."""
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, [], "что угодно")
            self.assertIsNone(row["expect_in_feed"])

    def test_the_flag_requires_every_expected_word_not_just_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, ["овсян", "казань"], "тут только овсянка")
            self.assertFalse(row["expect_in_feed"])

    def test_an_empty_feed_never_satisfies_a_real_expectation(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self._judge(tmp, ["овсян"], "")
            self.assertFalse(row["expect_in_feed"])


# --- 3. журнал несёт признак рядом с исходом --------------------------------

class FakeReport:
    def __init__(self, asked, probe=None):
        self.asked = asked
        self.probe = probe or {}


class TestTheJournalRowCarriesTheFlag(unittest.TestCase):
    """Свойство 3. `pair_row`/`journal_row` не молчат про признак."""

    @given(flag=st.one_of(st.none(), st.booleans()))
    @FAST
    def test_pair_row_carries_expect_in_feed_verbatim(self, flag):
        row = a_row(id="п", ok=False, injected=True, expect_in_feed=flag)
        report = FakeReport([row])
        line = live.pair_row(report, row)
        self.assertEqual(line["expect_in_feed"], flag)
        self.assertIn("outcome", line)
        self.assertIn("break", line)

    def test_journal_row_threads_the_flag_through_to_each_pair(self):
        rows = [a_row(id="a", ok=False, injected=True, expect_in_feed=False),
               a_row(id="b", ok=False, injected=True, expect_in_feed=True)]

        class R:
            def __init__(self, asked):
                self.asked, self.probe = asked, {}
                self.cost, self.voice = 0.0, "opora"

            @property
            def total(self):
                return len(self.asked)

            @property
            def passed(self):
                return sum(1 for r in self.asked if live.bucket(r) == live.APPLIED)

        played = OrderedDict(memory=R(rows))
        line = live.journal_row(played, player="claude", model="haiku",
                                pairs_file="n.json", pairs_count=2)
        by_id = {p["id"]: p for p in line["arms"]["memory"]["pairs"]}
        self.assertEqual(by_id["a"]["expect_in_feed"], False)
        self.assertEqual(by_id["a"]["outcome"], live.WRONG_FED)
        self.assertEqual(by_id["b"]["expect_in_feed"], True)
        self.assertEqual(by_id["b"]["outcome"], live.UNUSED)


# --- 4. старая строка журнала без поля не роняет разбор ---------------------

class TestOldJournalRowsStillWork(unittest.TestCase):
    """Свойство 4. Поле родилось с этой задачей — старых строк оно не касается,
    но читать их дальше обязано без падений."""

    def test_bucket_handles_a_row_with_no_flag_at_all(self):
        old = {"id": "макбук", "aim": "apply", "ok": False, "injected": True,
              "intruded": False, "error": None, "reason": None}
        self.assertEqual(live.bucket(old), live.UNUSED)

    def test_pair_row_handles_a_row_with_no_flag_at_all(self):
        old = {"id": "макбук", "aim": "apply", "ok": False, "injected": True,
              "intruded": False, "error": None, "reason": None}
        report = FakeReport([old])
        line = live.pair_row(report, old)
        self.assertIsNone(line["expect_in_feed"])

    def test_journal_summary_reads_a_mix_of_old_and_new_pair_rows(self):
        """`eval.journal_summary` смотрит только на `outcome`/`break` — новое
        поле рядом ему не мешает, и старую строку без него оно тоже не роняет.
        """
        from eval import journal_summary as js

        old_entry = {"id": "макбук", "aim": "apply", "outcome": live.UNUSED,
                    "break": "факт в БД"}
        new_entry = {"id": "макбук", "aim": "apply", "outcome": live.WRONG_FED,
                    "break": "факт в БД", "expect_in_feed": False}
        old_row = {"only": "макбук", "arms": {"memory": {"pairs": [old_entry]}}}
        new_row = {"only": "макбук", "arms": {"memory": {"pairs": [new_entry]}}}
        summary = js.summarize([old_row, new_row], "макбук")
        self.assertEqual(summary["total"], 2)


if __name__ == "__main__":
    unittest.main()
