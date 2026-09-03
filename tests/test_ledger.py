#!/usr/bin/env python3
"""Лента обращений: показ, польза и молчание дописываются, а не затираются.

Запуск: python3 -m unittest tests.test_ledger -v

Память отмечала исход последней вставки и стирала предыдущий. История не
копилась, поэтому «этот факт помогает через раз» сказать было нечем — а веса и
сроки должны считаться именно на такой истории, см. ADR 0010.

Второе: в журнал не попадали молчания, а они и отвечают на вопрос, почему
память не сработала. Из ста эталонных вопросов память отвечает нужным в 32, а
до агента доходит 25, и без имён причин разбирать этот разрыв нечем.

**Правила, записанные до кода.**

1. Лента только дописывается. Прочитанное раньше остаётся началом прочитанного
   позже — ни одна строка не правится и не исчезает.
2. Заход подсказки оставляет ровно одну отметку исхода: либо вброс, либо
   молчание с названной причиной. Ни того и другого разом, ни пустоты.
3. Показ и польза считаются раздельно и накапливаются. По факту видно «показан
   N раз, помог M раз», а не последнее значение.
4. Ответа нет — это `unknown`, а не `no`. Молчание агента в отрицательный ответ
   не сваливается: доля пользы считается только там, где ответ был.
"""
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, models
from pipeline import suggest, understand
from storage import local, port

TALK = "разговор-1"

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])

REASONS = st.sampled_from(ledger.REASONS)
VERDICTS = st.sampled_from(ledger.VERDICTS)

# Ключ факта: вид и охват из схемы, тема — свободный текст без разделителя.
SUBJECTS = st.text(alphabet="абвгдежзabcdefgh ", min_size=1, max_size=12).map(str.strip)
KEYS = st.builds(lambda t, s, c: "%s|%s|%s" % (t, s or "тема", c),
                 st.sampled_from(models.FACT_TYPES), SUBJECTS,
                 st.sampled_from(models.SCOPES))


@contextlib.contextmanager
def tape():
    """Своя лента на время проверки. В домашнюю пользователя не пишем."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        with mock.patch.object(ledger, "LOG", path):
            yield path


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"), \
         mock.patch.object(ledger, "LOG", Path(tmp) / "ledger.jsonl"):
        try:
            yield base
        finally:
            local.close()


class Answering:
    """Дверь, которая отвечает заданным. Подменяем ответ хранилища, а не его.

    Запись уходит настоящей двери: проверяем ленту на живом проходе, а дверь,
    которая молча глотает запись, показала бы ленту рядом с записью, которой
    нет.
    """

    name = "local"

    def __init__(self, answer=""):
        self.answer = answer

    def read(self, query, mode="single"):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def write(self, text, wait=False):
        return port.door().write(text, wait)

    def write_objects(self, records, relations=(), op="create"):
        return port.door().write_objects(records, relations, op)


def fact_piece(content, score=None):
    """Кусок выдачи в том виде, в каком его отдаёт хранилище."""
    body = dict(object_type="Fact", fact_type="project_state", subject="демо",
                scope="project", content=content)
    if score is not None:
        body["content"] = "%s Оценка уверенности: %.2f" % (content, score)
    return json.dumps([body], ensure_ascii=False)


def injections_in(base):
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM memoryinjection")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class TestTheLedgerOnlyGrows(unittest.TestCase):
    """Правило 1. Лента дописывается и не правится."""

    @SLOW
    @given(reasons=st.lists(REASONS, min_size=1, max_size=8))
    def test_what_was_read_stays_the_beginning_of_what_is_read_later(self, reasons):
        with tape():
            seen = []
            for reason in reasons:
                was = ledger.rows()
                self.assertEqual(was[:len(seen)], seen, "прочитанное раньше поехало")
                ledger.silence(reason, session_id=TALK, query="вопрос")
                seen = ledger.rows()
                self.assertEqual(len(seen), len(was) + 1, "строка не дописалась")

    @SLOW
    @given(reasons=st.lists(REASONS, min_size=0, max_size=8))
    def test_every_append_is_one_line_and_none_is_lost(self, reasons):
        with tape():
            for reason in reasons:
                ledger.silence(reason, session_id=TALK, query="вопрос")
            self.assertEqual([row["reason"] for row in ledger.rows()], reasons)

    def test_a_broken_line_does_not_kill_the_read(self):
        with tape() as path:
            ledger.silence("not_found", session_id=TALK, query="вопрос")
            with path.open("a", encoding="utf-8") as fh:
                fh.write("не json\n")
            ledger.silence("overdue", session_id=TALK, query="вопрос")
            self.assertEqual([row["reason"] for row in ledger.rows()],
                             ["not_found", "overdue"])


class TestEveryPassLeavesExactlyOneOutcome(unittest.TestCase):
    """Правило 2. Заход оставляет либо вброс, либо молчание. Ровно одно."""

    CASES = {
        "not_found": "",
        # Проза читателя вместо записей: так отвечает текстовый путь наружу.
        "not_found_prose": "no matching files found",
        "incidental": json.dumps([{"object_type": "Event", "project": "альфа",
                                   "session_id": "s", "sequence_number": 1,
                                   "content": "ls -la"}], ensure_ascii=False),
        "below_threshold": fact_piece("что-то было", score=0.1),
        "over_budget": fact_piece("ц" * (suggest.MAX_CHARS + 500)),
        "backend_error": port.BackendError("носитель не отвечает"),
        "overdue": suggest.Overdue(),
    }

    def attend(self, answer, **kw):
        return suggest.attend("альфа", session_id=TALK, door=Answering(answer),
                              hot=True, record=False, **kw)

    def test_each_reason_is_reachable_from_the_pipeline(self):
        """Каждая причина не выдумана, а достижима настоящим заходом."""
        for case, answer in self.CASES.items():
            reason = case.split("_prose")[0]
            with self.subTest(case=case), tape():
                kw = {"min_score": 0.5} if reason == "below_threshold" else {}
                text, _kept, got = self.attend(answer, **kw)
                self.assertEqual(text, "", "память не промолчала")
                self.assertEqual(got, reason)
                rows = ledger.rows()
                self.assertEqual([row["event"] for row in rows], ["silent"],
                                 "молчание не попало в ленту")
                self.assertEqual(rows[0]["reason"], reason)

    # Две причины проверяются отдельно: рубильник дверью не задаётся, а своя
    # поломка приходит не из ответа хранилища, а из середины конвейера.
    APART = ("disabled", "pipeline_error")

    def test_every_declared_reason_is_covered_by_a_case(self):
        """Причина, до которой не добирается ни один заход, — выдумка."""
        covered = {case.split("_prose")[0] for case in self.CASES}
        self.assertEqual(covered | set(self.APART), set(ledger.REASONS))

    def test_our_own_crash_is_not_called_a_backend_failure(self):
        """Поломка конвейера называется своим именем.

        Свали её на носитель — и колонка отказов начнёт расти от наших же
        ошибок, а разбивка по причинам, ради которой всё затевалось, соврёт.
        """
        with tape(), mock.patch.object(suggest, "render",
                                       side_effect=TypeError("своя поломка")):
            _text, _kept, got = self.attend(fact_piece("файлы демо"))
            self.assertEqual(got, "pipeline_error")
            rows = ledger.rows()
            self.assertEqual([row["event"] for row in rows], ["silent"])
            self.assertEqual(rows[0]["reason"], "pipeline_error")
            self.assertIn("TypeError", rows[0]["note"])

    def test_prose_from_the_backend_is_not_called_over_budget(self):
        """«Ничего не нашлось» словами — самая частая причина молчания.

        Она проходит порог (оценки у прозы нет, а отсутствие оценки не отказ) и
        отсеивается уже потолком. Назови её «не влезло в потолок» — и самая
        большая колонка разбивки уедет в чужую.
        """
        with tape():
            _text, _kept, got = self.attend("no matching files found")
            self.assertEqual(got, "not_found")

    def test_a_switched_off_memory_says_so_and_does_not_say_not_found(self):
        """Выключенная память молчит по своей причине, а не «не нашли»."""
        with tape():
            _text, _kept, got = suggest.attend("альфа", session_id=TALK,
                                               door=port.SilentDoor(), hot=True,
                                               record=False)
            self.assertEqual(got, "disabled")

    @SLOW
    @given(reason=REASONS)
    def test_the_reason_always_has_a_name_from_the_list(self, reason):
        with tape():
            ledger.silence(reason, session_id=TALK, query="вопрос")
            for row in ledger.rows():
                self.assertIn(row["reason"], ledger.REASONS)

    def test_a_pass_that_spoke_leaves_an_injection_and_no_silence(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            text, kept, got = suggest.attend(
                "демо", session_id=TALK, door=Answering(fact_piece("файлы демо")),
                hot=True)
            self.assertTrue(text, "память промолчала, проверять нечего")
            self.assertIsNone(got)
            events = [row["event"] for row in ledger.rows()]
            self.assertIn("injected", events)
            self.assertNotIn("silent", events)
            self.assertEqual(events.count("injected"), 1)


class TestHistoryAccumulates(unittest.TestCase):
    """Правило 3. По факту видно «показан N раз, помог M раз»."""

    @SLOW
    @given(keys=st.lists(KEYS, min_size=1, max_size=4, unique=True),
           times=st.integers(min_value=1, max_value=6))
    def test_the_count_of_shows_is_the_number_of_shows(self, keys, times):
        with tape():
            for turn in range(times):
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, keys, query="вопрос")
            got = ledger.tally(ledger.rows())
            for key in keys:
                self.assertEqual(got[key]["shown"], times)

    @SLOW
    @given(schedule=st.lists(VERDICTS, min_size=1, max_size=10))
    def test_helped_counts_answers_not_the_last_one(self, schedule):
        """Отметка не затирает предыдущую: считается вся история."""
        key = "project_state|демо|project"
        with tape():
            for turn, verdict in enumerate(schedule):
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, verdict, source="transcript")
            got = ledger.tally(ledger.rows())[key]
            self.assertEqual(got["shown"], len(schedule))
            self.assertEqual(got["helped"], schedule.count("yes"))
            self.assertEqual(got["not_helped"], schedule.count("no"))
            self.assertEqual(got["unknown"], schedule.count("unknown"))

    @SLOW
    @given(schedule=st.lists(VERDICTS, min_size=1, max_size=10))
    def test_answers_never_outnumber_shows(self, schedule):
        key = "user|человек|global"
        with tape():
            for turn, verdict in enumerate(schedule):
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, verdict, source="transcript")
            got = ledger.tally(ledger.rows())[key]
            self.assertLessEqual(got["helped"] + got["not_helped"] + got["unknown"],
                                 got["shown"])

    def test_helped_three_times_out_of_ten_is_expressible(self):
        """То самое, чего не выражала отметка на записи."""
        key = "project_state|демо|project"
        with tape():
            for turn in range(10):
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, "yes" if turn < 3 else "no",
                              source="transcript")
            got = ledger.tally(ledger.rows())[key]
            self.assertEqual((got["shown"], got["helped"]), (10, 3))

    @SLOW
    @given(first=VERDICTS, second=VERDICTS)
    def test_a_corrected_answer_does_not_count_twice(self, first, second):
        """Одна вставка, один способ съёма — один текущий ответ, а не два."""
        key = "project_state|демо|project"
        with tape():
            at = "2026-08-28T10:00:00Z"
            ledger.injected(TALK, at, [key], query="вопрос")
            ledger.helped(TALK, at, first, source="transcript")
            ledger.helped(TALK, at, second, source="transcript")
            got = ledger.tally(ledger.rows())[key]
            answers = got["helped"] + got["not_helped"] + got["unknown"]
            self.assertEqual(answers, 1)
            self.assertEqual(len(ledger.rows()), 4, "лента потеряла историю правки")

    @SLOW
    @given(reasons=st.lists(REASONS, min_size=1, max_size=6))
    def test_the_silences_are_counted_by_name(self, reasons):
        with tape():
            for reason in reasons:
                ledger.silence(reason, session_id=TALK, query="вопрос")
            got = ledger.silences(ledger.rows())
            for reason in set(reasons):
                self.assertEqual(got[reason], reasons.count(reason))


class TestNoAnswerIsNotADenial(unittest.TestCase):
    """Правило 4. «Ответа нет» — своё значение, а не «не помогло»."""

    @SLOW
    @given(answered=st.lists(st.sampled_from(["yes", "no"]), min_size=1, max_size=6),
           mute=st.integers(min_value=0, max_value=6))
    def test_silence_never_lands_in_the_negative(self, answered, mute):
        key = "project_state|демо|project"
        with tape():
            turn = 0
            for verdict in answered + ["unknown"] * mute:
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, verdict, source="transcript")
                turn += 1
            got = ledger.tally(ledger.rows())[key]
            self.assertEqual(got["not_helped"], answered.count("no"),
                             "молчание сложилось с отрицательным ответом")
            self.assertEqual(got["helped"], answered.count("yes"))
            self.assertEqual(got["unknown"], mute)

    @SLOW
    @given(answered=st.lists(st.sampled_from(["yes", "no"]), min_size=1, max_size=6),
           mute=st.integers(min_value=0, max_value=6))
    def test_the_share_is_counted_only_where_there_was_an_answer(self, answered, mute):
        """Доля пользы не едет вниз оттого, что агент промолчал."""
        key = "project_state|демо|project"
        with tape():
            turn = 0
            for verdict in answered:
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, verdict, source="transcript")
                turn += 1
            before = ledger.share(ledger.rows())[key]
            for _ in range(mute):
                at = "2026-08-28T10:%02d:00Z" % turn
                ledger.injected(TALK, at, [key], query="вопрос")
                ledger.helped(TALK, at, "unknown", source="transcript")
                turn += 1
            self.assertEqual(ledger.share(ledger.rows())[key], before)

    def test_an_abandoned_turn_is_unknown_and_not_a_no(self):
        """Ход без исхода — «ответа нет». Так же, как в записи о вставке."""
        with tape():
            ledger.injected(TALK, "2026-08-28T10:00:00Z", ["user|человек|global"],
                            query="вопрос")
            ledger.helped(TALK, "2026-08-28T10:00:00Z", ledger.verdict_of(None),
                          source="transcript")
            got = ledger.tally(ledger.rows())["user|человек|global"]
            self.assertEqual((got["unknown"], got["not_helped"]), (1, 0))

    @SLOW
    @given(helped=st.sampled_from([True, False, None]))
    def test_the_mark_of_the_record_maps_to_three_values(self, helped):
        self.assertEqual(ledger.verdict_of(helped),
                         {True: "yes", False: "no", None: "unknown"}[helped])


class TestTheLedgerStandsBesideTheRecord(unittest.TestCase):
    """Лента встаёт рядом с записью о вставке, а не вместо неё."""

    def test_the_injection_record_is_still_written(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            suggest.attend("демо", session_id=TALK,
                           door=Answering(fact_piece("файлы демо")))
            self.assertEqual(len(injections_in(base)), 1, "записи о вставке нет")
            self.assertIn("injected", [row["event"] for row in ledger.rows()])

    def test_the_shown_facts_are_the_sources_of_the_injection(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            text, kept, _ = suggest.attend(
                "демо", session_id=TALK,
                door=Answering(fact_piece("файлы демо")))
            self.assertTrue(kept, "память ничего не нашла, проверять нечего")
            shown = [row for row in ledger.rows() if row["event"] == "shown"]
            self.assertEqual([row["key"] for row in shown],
                             [one.identity() for one in suggest.sources_of(kept)])

    def test_a_rejected_record_still_leaves_the_show_in_the_ledger(self):
        """Показ был, если текст ушёл агенту. Носитель тут ни при чём.

        Привяжи ленту к записи — и заход, чью запись хранилище не приняло,
        пропал бы из неё целиком: ни вброса, ни молчания. То есть невидимо
        ровно там, где имена причин и нужны.
        """

        class Deaf(Answering):
            def write_objects(self, records, relations=(), op="create"):
                raise RuntimeError("хранилище не приняло запись")

        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            text, _kept, got = suggest.attend(
                "демо", session_id=TALK, door=Deaf(fact_piece("файлы демо")))
            self.assertTrue(text, "подсказка не собралась, проверять нечего")
            self.assertIsNone(got)
            self.assertEqual(injections_in(base), [], "запись всё-таки легла")
            events = [row["event"] for row in ledger.rows()]
            self.assertEqual(events.count("injected"), 1, "показ потерялся")
            self.assertIn("shown", events)


class TestTheRunDoesNotWriteIntoTheLivingLedger(unittest.TestCase):
    """Прогон не дописывает в ленту пользователя.

    Лента пишется на каждом заходе подсказки, в том числе из проверок, которые
    про неё не знают. Первый же прогон дописал в живую ленту четыре тысячи
    строк про выдуманный разговор — счёт «показан N раз» после такого врёт на
    всех записях сразу.
    """

    def test_the_default_path_leads_away_from_home(self):
        self.assertFalse(str(ledger.LOG).startswith(str(Path.home())),
                         "лента прогона ведёт в домашний каталог: %s" % ledger.LOG)

    def test_a_pass_without_a_substituted_ledger_still_writes_beside(self):
        """Заход, который ленту не подменял, пишет не в пользовательскую."""
        got = suggest.attend("альфа", session_id=TALK, door=Answering(""),
                             record=False)
        self.assertEqual(got[2], "not_found")
        self.assertTrue(ledger.LOG.exists())
        self.assertFalse(str(ledger.LOG).startswith(str(Path.home())))


class TestSettlingTwiceDoesNotInflateTheLedger(unittest.TestCase):
    """Проход отметки идёт по журналу целиком каждый раз."""

    def test_a_second_pass_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            suggest.note_injection(TALK, "Из памяти", (), door=door,
                                   at="2026-08-28T11:00:00Z")
            suggest.settle([], door=door)
            was = ledger.rows()
            self.assertTrue([row for row in was if row["event"] == "helped"],
                            "отметка не попала в ленту")
            suggest.settle([], door=door)
            self.assertEqual(ledger.rows(), was)


if __name__ == "__main__":
    unittest.main()
