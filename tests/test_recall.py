#!/usr/bin/env python3
"""Найденное доходит до агента: квота по видам и кусок без служебного.

Запуск: python3 -m unittest tests.test_recall -v

Память находила нужное вдвое чаще, чем отдавала: 45 случаев из 100 против 21.
Две причины, обе в отборе.

Первая: событий в базе 45 тысяч, фактов 466. Отбор брал десять лучших строк без
разбора вида, и команда Bash, где слово встретилось пять раз, обгоняла факт, где
оно встретилось один. Приоритет объектов был множителем, а множитель проигрывает
частоте слова в длинном тексте.

Вторая: кусок уходил вместе со служебными полями — подпись, охват, вид, тип
записи. Для факта это дословный повтор его же текста, и в потолок влезало три
куска вместо восьми.

Свойствами: важно не «на этом запросе стало лучше», а «факт не вытесняется
событием никогда» и «кусок не несёт того, что уже сказано его текстом».
"""
import json, os, tempfile, unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import suggest
from storage import db

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

PROJECT = "demo"
NAMES = ["db.py", "port.py", "run.py"]


def repo_with(tmp, facts=1, events=40):
    """База, где событий много, а фактов мало — как в жизни.

    Событие нарочно сильнее факта по счёту: слово вопроса стоит у него и в
    проекте, и в тексте, а у факта — только в тексте. Без квоты по видам
    десяток таких событий закрывает собой всю выдачу, и факт не доезжает.
    """
    repo = db.Repository(Path(tmp) / "memory.db")
    rows = []
    for number in range(facts):
        name = NAMES[number % len(NAMES)]
        rows.append(models.Fact(
            fact_type="project_state", subject="/home/person/шкаф/%s" % name,
            scope="project",
            content="В проекте %s правился файл /home/person/шкаф/%s ради задачи: почини"
                    % (PROJECT, name)).mutation())
    for number in range(events):
        rows.append(models.Event(
            session_id="разговор-1", sequence_number=number,
            event_type="tool_call", tool_name="Bash", project=PROJECT,
            content="ls /home/person/dev/%s && cat README" % PROJECT).mutation())
    repo.apply(rows)
    return repo


class TestAFactIsNotCrowdedOutByEvents(unittest.TestCase):
    """Квота по видам: факту в выдаче место гарантировано, а не выторговано."""

    @SLOW
    @given(events=st.integers(min_value=10, max_value=60))
    def test_the_fact_is_in_the_answer_whatever_the_noise(self, events):
        with tempfile.TemporaryDirectory() as tmp:
            repo = repo_with(tmp, facts=1, events=events)
            try:
                found = repo.search("какие файлы правились в проекте demo")
            finally:
                repo.close()
            kinds = [r["object_type"] for r in found]
            self.assertIn("Fact", kinds,
                          "факт вытеснен событиями: %s" % kinds)

    @SLOW
    @given(facts=st.integers(min_value=1, max_value=3),
           events=st.integers(min_value=10, max_value=60))
    def test_every_fact_that_matches_is_returned(self, facts, events):
        """Подходящих фактов мало — значит все они обязаны доехать."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = repo_with(tmp, facts=facts, events=events)
            try:
                found = repo.search("какие файлы правились в проекте demo")
            finally:
                repo.close()
            self.assertEqual(len([r for r in found if r["object_type"] == "Fact"]),
                             facts)

    def test_events_are_not_thrown_away_either(self):
        """Квота — не запрет: событие остаётся в выдаче, просто не вместо факта."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = repo_with(tmp, facts=1, events=20)
            try:
                found = repo.search("ls README demo")
            finally:
                repo.close()
            self.assertTrue([r for r in found if r["object_type"] == "Event"])


class TestThePieceSaysOnlyWhatMatters(unittest.TestCase):
    """Кусок для агента: смысл без служебного."""

    FACT = {"object_type": "Fact", "fact_type": "project_state",
            "subject": "/home/person/dev/demo/db.py", "scope": "project",
            "project": "demo",
            "content": "В проекте demo правился файл /home/person/dev/demo/db.py"}

    def test_a_fact_is_its_content_and_nothing_else(self):
        piece = suggest._text(self.FACT)
        self.assertEqual(piece, self.FACT["content"])

    def test_the_service_fields_never_reach_the_agent(self):
        piece = suggest._text(self.FACT)
        for word in ("fact_type", "scope", "object_type", "subject:"):
            self.assertNotIn(word, piece)

    def test_an_episode_keeps_what_the_question_asks_about(self):
        """У эпизода смысл в полях: ветка и исход. На этом уже спотыкались."""
        piece = suggest._text({"object_type": "Episode", "session_id": "разговор-1",
                               "episode_number": 3, "title": "почини порт",
                               "git_branch": "memory-encoder", "outcome": "done",
                               "project": "demo"})
        self.assertIn("memory-encoder", piece)
        self.assertIn("done", piece)

    @SLOW
    @given(count=st.integers(min_value=1, max_value=8))
    def test_more_facts_fit_under_the_ceiling(self, count):
        """Сколько фактов влезает в потолок: служебное съедало места втрое больше."""
        facts = [{"object_type": "Fact", "fact_type": "project_state",
                  "subject": "/home/person/dev/demo/%d.py" % number,
                  "scope": "project", "project": "demo",
                  "content": "В проекте demo правился файл /home/person/dev/demo/%d.py"
                             % number}
                 for number in range(count)]
            # Каждый кусок — примерно 55 символов вместо 190 со служебным.
        items = [(None, suggest._text(f), f) for f in facts]
        kept = suggest.gate(items, min_score=0.0)
        self.assertEqual(len(kept), min(count, suggest.MAX_ITEMS))


if __name__ == "__main__":
    unittest.main()
