#!/usr/bin/env python3
"""Ложная находка: слово внутри команды знанием не является.

Запуск: python3 -m unittest tests.test_false_hits -v

Память находила нужное вдвое чаще, чем доносила до агента. Разбор недоехавших
случаев показал: половина из них потерей не была вовсе.
Слово из вопроса случайно встретилось внутри команды, которую агент когда-то
выполнил, или внутри её вывода. Формально совпадение есть, знания нет — и
замер записывал такой случай в «нашли», а место в выдаче съедала команда.

Правило отсева, записанное до кода. Событие это сырьё: команда, её вывод,
реплика. Знанием оно становится в двух случаях:

  * вопрос зацепил его за то, чем оно является — инструмент, ветка;
  * вопрос сам просит дословного: «какой командой это чинили».

Слово, встретившееся только в имени места (проект, каталог, разговор) или
только внутри дословного тела, знанием не является: событий одного проекта в
архиве десятки тысяч, и любое из них подошло бы так же.

Отсев режет случайное совпадение, а не вид записи. Поэтому здесь две половины
одной пары: на одном и том же хранилище невербатимный вопрос команду не
получает, а вербатимный — получает. Проверка, у которой есть только первая
половина, зеленеет и на коде, который событий не отдаёт никогда.

Свойствами: важно не «в этом примере команда не пришла», а «ни одно слово,
живущее только внутри команды, не доходит до агента, и ни один факт от отсева
не теряется».
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import query as words_of
from eval import evaluate
from pipeline import save, suggest, understand
from storage import db, port

CWD = "/home/person/dev/demo"
PROJECT = "demo"
BRANCH = "demo-branch"

# Вопрос, который дословного не просит: спрашивают про файлы, не про команды.
PLAIN = "Какие файлы правились в проекте demo?"
# Тот же архив, но вопрос ровно про команду. Событие здесь и есть ответ.
VERBATIM = "Какой командой это чинили в проекте demo?"
# Вопрос по делу: назван не проект, а ветка. Дословного он не просит.
BY_DEED = "Что делали в ветке demo-branch?"

# Имена, в которых живёт слово из списка просьб о дословном. Просьбой они не
# являются: это файл, проект и ветка.
NAMES = ["errors.py", "bash-tools", "error-handler", "output.log", "command.py"]

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])

MARKS = ["hacker-sprint-memory.md", "runlog.py", "suspicious_phrases.yml",
         "arxiv.org", "localhost:5008"]
EDITED = ["db.py", "run.py", "port.py"]


def rows(session, edited, mark):
    """Разговор: правился один файл, а слово mark только мелькнуло в выводе.

    Слово mark не правилось и знанием не является — оно лежит внутри дословного
    тела события, вывода команды. Путь проекта в этом же выводе стоит нарочно:
    так выглядит настоящая находка поиска, которая цепляется за место.

    Вывод команды выбран не случайно: пересказ эпизода перечисляет команды и
    правленые файлы, но не их вывод. Слово в команде утекло бы в пересказ, и
    проверка «до агента не дошло» падала бы на честном знании эпизода.
    """
    head = {"sessionId": session, "timestamp": "2026-08-28T10:00:00Z",
            "cwd": CWD, "gitBranch": BRANCH}
    return [
        dict(head, type="user", message={"content": "Посмотри, что там с базой"}),
        dict(head, type="assistant", message={"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "%s/%s" % (CWD, edited)}}]}),
        dict(head, type="user", message={"content": [
            {"type": "tool_result",
             "content": "%s/%s\n%s/%s\n" % (CWD, edited, CWD, mark)}]}),
        dict(head, type="assistant", message={"content": [
            {"type": "text", "text": "Готово."}]}),
    ]


def archive(root, edited="db.py", mark=MARKS[0], session="разговор-1"):
    path = Path(root) / "разговор.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(session, edited, mark):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    from storage import local
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(save, "STATE", Path(tmp) / "save.json"), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def fill(files):
    """Полный конвейер: события сохранением, эпизоды и факты пониманием."""
    save.ingest(files, dry=False, door=port.door())
    understand.digest(files, door=port.door(), dry=False)


def out(question):
    text, kept, raw = suggest.suggest(question, mode="raw", min_score=0.0,
                                      door=port.door())
    return text, kept, raw


def kinds(kept):
    return [(r or {}).get("object_type") for _, _, r in kept]


def event(**over):
    """Событие в той форме, в какой его отдаёт хранилище."""
    base = {"object_type": "Event", "event_type": "tool_call", "tool_name": "Bash",
            "project": PROJECT, "working_directory": CWD, "git_branch": BRANCH,
            "session_id": "разговор-1", "content": '{"command": "ls %s"}' % CWD}
    base.update(over)
    return base


class TestTheWordInsideACommandDoesNotReachTheAgent(unittest.TestCase):
    """Головная пара: одно хранилище, два вопроса, разные исходы."""

    @SLOW
    @given(mark=st.sampled_from(MARKS), edited=st.sampled_from(EDITED))
    def test_a_plain_question_never_gets_the_command(self, mark, edited):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, edited, mark))
            text, kept, raw = out(PLAIN)
            self.assertIn(mark, raw, "память слова не нашла, отсеивать нечего")
            self.assertIn(edited, text, "настоящее знание до агента не дошло")
            self.assertNotIn(mark, text,
                             "слово из вывода команды дошло до агента: %s" % text)
            self.assertNotIn("Event", kinds(kept))

    @SLOW
    @given(mark=st.sampled_from(MARKS), edited=st.sampled_from(EDITED))
    def test_a_question_about_the_command_still_gets_it(self, mark, edited):
        """Вторая половина пары. Без неё первая зеленеет на любом коде."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, edited, mark))
            text, kept, _ = out(VERBATIM)
            self.assertIn("Event", kinds(kept),
                          "вопрос про команду события не получил: %s" % kinds(kept))
            self.assertIn(mark, text, "вывод команды до агента не дошёл: %s" % text)


class TestAskingForVerbatimIsRecognisedByWordsNotSubstrings(unittest.TestCase):
    """Просьбу о дословном узнаём по слову вопроса, а не по его подстроке.

    Сравнение подстрокой ошибалось в обе стороны сразу и молча: вопрос про файл
    `errors.py` включал исключение и выключал отсев целиком.
    """

    @SLOW
    @given(name=st.sampled_from(NAMES))
    def test_a_name_never_switches_the_sift_off(self, name):
        for shape in ("Какие файлы правились в %s?",
                      "Какие файлы правились в проекте %s?",
                      "Что делали в ветке %s?"):
            question = shape % name
            self.assertFalse(suggest.asks_verbatim(question), question)

    def test_a_real_request_for_verbatim_is_recognised(self):
        for question in ("Какой командой это чинили?",
                         "Что показал запуск тестов?",
                         "Какая ошибка была в сборке?",
                         "Что было в выводе команды?"):
            self.assertTrue(suggest.asks_verbatim(question), question)

    @SLOW
    @given(name=st.sampled_from(NAMES))
    def test_a_name_does_not_hide_a_real_request(self, name):
        """Имя не выключает исключение, но и не мешает ему сработать."""
        self.assertTrue(suggest.asks_verbatim("Какой командой правили %s?" % name))


class TestTheSiftCutsChanceNotAKind(unittest.TestCase):
    """Отсев режет случайное совпадение, а не вид записи."""

    @SLOW
    @given(name=st.sampled_from(MARKS))
    def test_a_fact_is_never_incidental(self, name):
        for kind in ("Fact", "Episode", "Session", "LapsedFact"):
            record = {"object_type": kind, "project": PROJECT, "content": name}
            self.assertFalse(suggest.incidental(record, [PROJECT, name]), kind)

    def test_an_event_caught_by_its_branch_survives(self):
        """Зацепка делом: слово попало в ветку, а не в имя места и не в тело."""
        self.assertFalse(suggest.incidental(event(), words_of.words(
            "что было в ветке demo-branch")))

    def test_an_event_caught_by_its_tool_survives(self):
        self.assertFalse(suggest.incidental(event(), ["bash"]))

    def test_an_event_caught_only_by_the_place_is_incidental(self):
        self.assertTrue(suggest.incidental(event(), [PROJECT]))

    def test_an_event_caught_only_inside_the_command_is_incidental(self):
        marked = event(content='{"command": "grep %s ."}' % MARKS[0])
        self.assertTrue(suggest.incidental(marked, [MARKS[0]]))

    @SLOW
    @given(mark=st.sampled_from(MARKS))
    def test_a_verbatim_question_sifts_nothing(self, mark):
        marked = event(content='{"command": "grep %s ."}' % mark)
        self.assertTrue(suggest.incidental(marked, [mark]))
        self.assertFalse(suggest.incidental(marked, [mark], verbatim=True))


class TestAnEventAskedByItsDeedGetsThrough(unittest.TestCase):
    """Путь «по делу» на живом хранилище, а не на выдуманной записи.

    Иначе единственным свидетельством, что отсев не режет вид, остаётся чистая
    функция: на настоящем архиве отсев неотличим от «событий не отдаём никогда».
    Здесь вопрос называет ветку — она не объясняется именем проекта, дословного
    вопрос не просит, и событие обязано дойти.
    """

    @SLOW
    @given(mark=st.sampled_from(MARKS), edited=st.sampled_from(EDITED))
    def test_a_question_about_the_branch_keeps_its_events(self, mark, edited):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, edited, mark))
            self.assertFalse(suggest.asks_verbatim(BY_DEED),
                             "вопрос попал в исключение, путь «по делу» не проверен")
            _, kept, raw = out(BY_DEED)
            self.assertIn("Event", kinds(kept),
                          "событие по делу не дошло: %s" % kinds(kept))


class TestTheSiftOnlyTakesAway(unittest.TestCase):
    """Свойства самого отсева: он убирает, не добавляет и не переставляет."""

    ROWS = st.lists(st.one_of(
        st.builds(lambda t: (None, t, event(content=t)), st.text(min_size=1, max_size=40)),
        st.builds(lambda t: (0.9, t, {"object_type": "Fact", "content": t}),
                  st.text(min_size=1, max_size=40))), max_size=8)

    @SLOW
    @given(rows=ROWS, question=st.text(max_size=40))
    def test_sift_keeps_order_and_only_removes(self, rows, question):
        got = suggest.sift(rows, question)
        self.assertLessEqual(len(got), len(rows))
        self.assertEqual(got, [row for row in rows if row in got])
        self.assertEqual(suggest.sift(got, question), got)

    @SLOW
    @given(rows=ROWS, question=st.text(max_size=40))
    def test_sift_never_drops_a_fact(self, rows, question):
        kept = suggest.sift(rows, question)
        was = [r for r in rows if (r[2] or {}).get("object_type") != "Event"]
        self.assertEqual([r for r in kept
                          if (r[2] or {}).get("object_type") != "Event"], was)


class TestTheSiftNeverCostsKnowledge(unittest.TestCase):
    """Ни один факт и ни один эпизод от отсева не пропадает.

    Обратное — свободное место: команда, ушедшая из выдачи, освобождает потолок,
    и на её место приходит знание. Поэтому сравнение одностороннее.
    """

    @SLOW
    @given(mark=st.sampled_from(MARKS), edited=st.sampled_from(EDITED))
    def test_nothing_distilled_is_lost(self, mark, edited):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, edited, mark))
            raw = port.door().read(PLAIN, mode="raw")
            rough = suggest.gate(suggest.pieces(raw), min_score=0.0)
            fine = suggest.gate(suggest.sift(suggest.pieces(raw), PLAIN),
                                min_score=0.0)
            distilled = lambda kept: {t for _, t, r in kept
                                      if (r or {}).get("object_type") != "Event"}
            self.assertTrue(distilled(rough) <= distilled(fine))


class TestOneRuleForWhatAWordIs(unittest.TestCase):
    """Поиск и отсев делят слово вопроса одним правилом.

    Разойдись они — отсев считал бы попаданием не то, чем поиск нашёл запись,
    и оба остались бы правы поодиночке.
    """

    def test_the_store_takes_its_words_from_the_domain(self):
        self.assertIs(db.words, words_of.words)

    @SLOW
    @given(question=st.text(max_size=60))
    def test_the_sift_asks_the_same_question_as_the_search(self, question):
        self.assertEqual(suggest.terms_of(question), words_of.words(question))


class TestTheMeasureCountsKnowledgeNotSubstrings(unittest.TestCase):
    """Замер и выдача судят одним кодом: найденным считается то, что могло дойти."""

    def raw_of(self, records):
        return json.dumps(records, ensure_ascii=False)

    def test_a_word_only_inside_a_command_is_not_a_find(self):
        raw = self.raw_of([event(content='{"command": "grep runlog.py ."}')])
        case = {"id": "x", "query": PLAIN, "expect": ["runlog.py"]}
        known, _cut = suggest.knowledge(raw, PLAIN)
        got = evaluate.judge(case, "", known, None, raw=raw)
        self.assertFalse(got["found_in_answer"])
        self.assertTrue(got["false_find"])

    def test_the_run_asks_the_sift_and_not_the_raw_answer(self):
        """Проверка на самой проводке замера.

        Судья умеет считать по отсеянному, но считать по нему обязан прогон:
        проверка одного судьи проходит и на прогоне, который подаёт ему сырой
        ответ целиком.
        """
        raw = self.raw_of([event(content='{"command": "grep runlog.py ."}')])
        case = {"id": "мутация", "kind": "fact", "query": PLAIN,
                "expect": ["runlog.py"]}
        with mock.patch.object(evaluate.suggest, "suggest",
                               return_value=("", [], raw)):
            got = evaluate.run_case(case, "raw", 0.5)
        self.assertFalse(got["found_in_answer"])
        self.assertTrue(got["false_find"])

    def test_the_run_reports_how_much_the_sift_took_away(self):
        """Объём среза виден числом: иначе перетянутый отсев выглядит успехом."""
        raw = self.raw_of([event(content='{"command": "grep runlog.py ."}'),
                           {"object_type": "Fact", "project": PROJECT,
                            "content": "В проекте demo правился файл db.py"}])
        case = {"id": "срез", "kind": "fact", "query": PLAIN, "expect": ["db.py"]}
        with mock.patch.object(evaluate.suggest, "suggest",
                               return_value=("", [], raw)):
            got = evaluate.run_case(case, "raw", 0.5)
        self.assertEqual(got["sifted"], 1)

    def test_the_run_still_counts_a_find_that_is_knowledge(self):
        raw = self.raw_of([{"object_type": "Fact", "project": PROJECT,
                            "content": "В проекте demo правился файл runlog.py"}])
        case = {"id": "знание", "kind": "fact", "query": PLAIN,
                "expect": ["runlog.py"]}
        with mock.patch.object(evaluate.suggest, "suggest",
                               return_value=("", [], raw)):
            got = evaluate.run_case(case, "raw", 0.5)
        self.assertTrue(got["found_in_answer"])
        self.assertFalse(got["false_find"])

    def test_a_case_found_by_half_is_a_loss_and_not_garbage(self):
        """Одно слово пережило отсев, второе нет. Это потеря, а не мусор.

        Считать такой случай ложной находкой значит завышать долю отсеянного и
        списывать в мусор настоящую потерю на извлечении.
        """
        raw = self.raw_of([
            {"object_type": "Fact", "project": PROJECT,
             "content": "В проекте demo правился файл CONTEXT.md"},
            event(content='{"command": "grep config.yml ."}')])
        case = {"id": "половина", "query": PLAIN,
                "expect": ["CONTEXT.md", "config.yml"]}
        known, _cut = suggest.knowledge(raw, PLAIN)
        got = evaluate.judge(case, "", known, None, raw=raw)
        self.assertFalse(got["found_in_answer"])
        self.assertFalse(got["false_find"])

    def test_a_word_in_a_fact_is_a_find(self):
        raw = self.raw_of([{"object_type": "Fact", "project": PROJECT,
                            "content": "В проекте demo правился файл runlog.py"}])
        case = {"id": "x", "query": PLAIN, "expect": ["runlog.py"]}
        known, _cut = suggest.knowledge(raw, PLAIN)
        got = evaluate.judge(case, "", known, None, raw=raw)
        self.assertTrue(got["found_in_answer"])
        self.assertFalse(got["false_find"])


if __name__ == "__main__":
    unittest.main()
