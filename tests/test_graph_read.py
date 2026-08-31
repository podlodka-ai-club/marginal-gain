#!/usr/bin/env python3
"""Подсказка добирает связанное, а не только найденное.

Запуск: python3 -m unittest tests.test_graph_read -v

Граф связей заполнен — 1302 карточки на архиве, — а выдача его не читала:
поиск смотрел таблицы объектов и ни разу не заглядывал в связи. Всё, ради чего
граф строился, не работало: «что правилось вместе», «чем это кончилось».
Механизм без потребителя — та же болезнь, что была у ключей.

Правило шага: один шаг с затуханием. Сосед не может весить больше источника,
прямое попадание идёт первым и соседом не вытесняется, потолки на число кусков
и объём — прежние. Два шага и транзитивность здесь не делаются.

Свойствами: важно не «в этом примере пришёл сосед», а «сосед всегда ниже
источника, всегда один шаг, и дверь без обхода работает как раньше».
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import associate, suggest, understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "graph-read"

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

NAMES = ["db.py", "port.py", "run.py", "api.py"]


def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD,
                "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": "Посмотри, что там с базой"}))
        blocks = [{"type": "tool_use", "name": "Edit",
                   "input": {"file_path": "%s/%s" % (CWD, name)}}
                  for name in spec]
        blocks.append({"type": "text", "text": "Готово."})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape, session="разговор-1"):
    path = Path(root) / "разговор.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(session, shape):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def fill(files):
    """Полный конвейер: эпизоды и факты, потом связи между фактами."""
    understand.digest(files, door=port.door(), dry=False)
    associate.build(files, door=port.door(), dry=False)


class Deaf:
    """Дверь, которая обхода графа не умеет. Такова сеть, и она в строю."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, text, wait=False):
        return self.inner.write(text, wait=wait)

    def write_objects(self, records, relations=()):
        return self.inner.write_objects(records, relations)

    def read(self, query, mode="single"):
        return self.inner.read(query, mode=mode)


def texts(kept):
    return [text for _, text, _ in kept]


def by_graph(kept):
    """Только то, что добрано шагом по графу, а не найдено поиском.

    Спрашивать «есть ли в выдаче слово port.py» бесполезно: поиск находит его
    и сам, в тексте соседнего факта, и проверка проходит на коде, который по
    графу не ходит вовсе. Первая версия этих проверок так и зеленела.
    """
    return [record for _, _, record in kept
            if isinstance(record, dict) and record.get("via_graph")]


def subjects(records):
    return [r.get("subject") or "" for r in records]


class TestTheNeighbourComesAlong(unittest.TestCase):
    """Спросили про один файл — пришёл и тот, что правился вместе с ним."""

    def test_a_file_edited_together_shows_up(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [["db.py", "port.py"]]))
            door = port.door()
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door)
            self.assertIn("db.py", " ".join(texts(kept)), "прямого попадания нет")
            near = subjects(by_graph(kept))
            self.assertTrue(any("port.py" in one for one in near),
                            "сосед по графу не пришёл: %s" % near)

    def test_the_neighbour_weighs_less_than_the_hit(self):
        """Затухание: сосед весит вдвое меньше того, от кого пришёл.

        Дверь здесь поддельная: настоящая база оценок не хранит (их дописывает
        только текстовый путь), и на ней это свойство невыразимо — проверка
        свелась бы к сравнению двух констант. Спрашиваем сам шаг.
        """
        source = {"object_type": "Fact", "fact_type": "project_state",
                  "subject": "/x/db.py", "scope": "project", "content": "db"}
        neighbour = {"object_type": "Fact", "fact_type": "project_state",
                     "subject": "/x/port.py", "scope": "project", "content": "port"}

        class OneNeighbour:
            def neighbours(self, keys, limit=10):
                return [(neighbour, 3.0)]

        got = suggest.near([(0.9, "db", source)], OneNeighbour())
        self.assertEqual(len(got), 1)
        self.assertLess(got[0][0], 0.9)
        self.assertEqual(got[0][0], round(0.9 * suggest.DAMPING, 4))

    def test_a_neighbour_gets_no_score_when_the_hit_has_none(self):
        """Выдуманное число выглядит как измеренное. Лучше молчать.

        В хранилище оценок нет, и сосед был единственной строкой выдачи с
        цифрой: слабейшее в ответе выглядело как единственное проверенное.
        """
        neighbour = {"object_type": "Fact", "fact_type": "project_state",
                     "subject": "/x/port.py", "scope": "project", "content": "port"}

        class OneNeighbour:
            def neighbours(self, keys, limit=10):
                return [(neighbour, 3.0)]

        source = {"object_type": "Fact", "fact_type": "project_state",
                  "subject": "/x/db.py", "scope": "project", "content": "db"}
        got = suggest.near([(None, "db", source)], OneNeighbour())
        self.assertEqual([score for score, _, _ in got], [None])
        self.assertNotIn("уверенность", suggest.render(got))

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=4,
                                   unique=True), min_size=1, max_size=3))
    def test_the_hit_always_comes_before_its_neighbours(self, shape):
        """Прямое попадание первым: сосед добирается, а не вытесняет."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            _, kept, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                         door=door)
            self.assertTrue(by_graph(kept), "соседей нет вовсе, порядок проверять не на чем")
            seen_neighbour = False
            for score, text, record in kept:
                near = isinstance(record, dict) and record.get("via_graph")
                if near:
                    seen_neighbour = True
                elif seen_neighbour:
                    self.fail("прямое попадание встало после соседа")


class TestOneStepOnly(unittest.TestCase):
    """Один шаг. Сосед соседа — это уже другой разговор."""

    def test_a_neighbour_of_a_neighbour_stays_out(self):
        # Два эпизода: db.py с port.py, затем port.py с run.py. run.py — сосед
        # соседа, и приходить он не должен.
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [["db.py", "port.py"], ["port.py", "run.py"]]))
            door = port.door()
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door)
            near = subjects(by_graph(kept))
            self.assertTrue(any("port.py" in one for one in near),
                            "прямой сосед не пришёл: %s" % near)
            self.assertFalse(any("run.py" in one for one in near),
                             "пришёл сосед соседа: %s" % near)

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=3,
                                   unique=True), min_size=1, max_size=3))
    def test_every_neighbour_is_a_row_of_the_store(self, shape):
        """Сосед — запись, которая лежит в базе, а не выдуманный ключ."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, shape))
            door = port.door()
            _, kept, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                         door=door)
            conn = sqlite3.connect(str(base))
            try:
                known = {"%s|%s|%s" % row for row in conn.execute(
                    "SELECT fact_type, subject, scope FROM fact")}
            finally:
                conn.close()
            near = by_graph(kept)
            self.assertTrue(near, "ни одного соседа: проверять нечего")
            for record in near:
                self.assertIn("%s|%s|%s" % (record["fact_type"], record["subject"],
                                            record["scope"]), known)


class TestTheDeafDoorStillWorks(unittest.TestCase):
    """Дверь без обхода графа работает как раньше — молча и без ошибок."""

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=3,
                                   unique=True), min_size=1, max_size=3))
    def test_the_outcome_matches_the_plain_search(self, shape):
        """Выдача глухой двери — ровно та же, что у поиска без шага по графу.

        Сравниваем с настоящей дверью, а не просто смотрим на отсутствие
        пометки: без сравнения проверка проходит и на коде, который по графу
        не ходит вовсе.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            live = port.door()
            _, rich, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                         door=live)
            _, plain, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                          door=Deaf(live))
            self.assertTrue(by_graph(rich), "живая дверь соседей не дала")
            self.assertEqual(by_graph(plain), [])
            self.assertEqual(texts(plain),
                             [t for t, r in zip(texts(rich), [x[2] for x in rich])
                              if not (isinstance(r, dict) and r.get("via_graph"))])

    def test_an_empty_graph_changes_nothing(self):
        """Связей нет — выдача ровно та же, что и была."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, [["db.py"]])
            understand.digest(files, door=port.door(), dry=False)  # без связей
            door = port.door()
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door)
            self.assertTrue(kept)
            for _, _, record in kept:
                if isinstance(record, dict):
                    self.assertNotIn("via_graph", record)


class TestTheCeilingsHold(unittest.TestCase):
    """Потолки на число кусков и объём соседи не отменяют."""

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=4,
                                   unique=True), min_size=1, max_size=4))
    def test_no_more_pieces_than_allowed(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door)
            self.assertLessEqual(len(kept), suggest.MAX_ITEMS)
            self.assertLessEqual(sum(len(t) for t in texts(kept)),
                                 suggest.MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
