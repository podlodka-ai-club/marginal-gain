#!/usr/bin/env python3
"""Обход графа умеет всякая наша дверь, а не только локальная база.

Запуск: python3 -m unittest tests.test_graph_network -v

За дверью `storage/port.py` четыре пути наружу. Шаг по связям был написан
только у локальной базы; у `api` и `sdk` его не было вовсе, `suggest.near`
ловил AttributeError и отдавал пустоту. Снаружи это выглядело как «граф не
помогает», хотя граф просто не читали: смена хранилища молча меняла то, что
память умеет.

Проверяем свойствами, а не примером: важно не «в этом случае сосед пришёл», а
«на одном наборе обе двери дают одних соседей», «спрошенный не приходит сам к
себе в соседи», «порядок по весу», «выдуманных записей не бывает», «ключ с
кавычкой не рвёт вопрос». Отдельно — что путь отступления цел: чужая дверь,
обхода не умеющая, работает молча и без ошибок.

Служба здесь поддельная, но отвечает по контракту сервиса: `raw-tables`, то
есть `{"columns": [{"name": …}], "rows": [[…]]}` строкой. Данные под ней — та
же локальная база, что и у второй половины сравнения, и обычный поиск она
ведёт тем же `db.Repository`: сравнивать надо шаг по графу, а не два разных
поиска по двум разным наборам.
"""
import contextlib, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import associate, suggest, understand
from storage import api, db, graph, local, port, sdk

CWD = "/home/person/dev/demo"
BRANCH = "graph-network"

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])

FAST = settings(deadline=None, max_examples=60)

NAMES = ["db.py", "port.py", "run.py", "api.py"]


# --- стенд: архив, локальная база, поддельная служба -------------------------

def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-29T%02d:00:00Z" % (number % 24)
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


def table(columns, rows_):
    """Ответ службы в режиме raw-tables — ровно той формы, что у сервиса."""
    return json.dumps({"columns": [{"name": n, "type": "string"} for n in columns],
                       "rows": [list(r) for r in rows_]}, ensure_ascii=False)


def quoted(query):
    """Значения, которые вопрос назвал. Служба видит только их — как text-to-SQL.

    Кавычка внутри значения удвоена, как в SQL: тема факта это свободный текст,
    и апостроф в ней вполне реален.
    """
    out, rest = [], query
    while "'" in rest:
        _, _, rest = rest.partition("'")
        value, _, rest = rest.partition("'")
        while rest.startswith("'"):
            more, _, rest = rest[1:].partition("'")
            value += "'" + more
        out.append(value)
    return out


class Service:
    """Поддельный xmemory поверх настоящей базы. Считает заданные вопросы.

    Три вида вопроса: про связи, про факты по подписи и обычный поиск словами.
    Поиск отдаёт то же, что отдал бы локальный путь: половины сравнения должны
    отличаться шагом по графу, а не выдачей поиска.
    """

    def __init__(self, path):
        self.repo = db.Repository(str(path))
        self.asked = []

    def close(self):
        self.repo.close()

    def read(self, query, mode="single-answer", timeout=None):
        self.asked.append((query, mode))
        keys = quoted(query)
        if "Association" in query:
            if not keys:
                return ""
            holes = ", ".join("?" * len(keys))
            with self.repo.lock:
                found = self.repo.conn.execute(
                    "SELECT source_key, target_key, weight FROM association "
                    "WHERE source_key IN (%s) OR target_key IN (%s) "
                    "ORDER BY weight DESC" % (holes, holes), keys + keys).fetchall()
            return table(["source_key", "target_key", "weight"],
                         [tuple(r) for r in found]) if found else ""
        if keys:
            names = ["fact_type", "subject", "scope", "content", "project"]
            want = []
            for key in keys:
                end = models.Fact.of_identity(key)
                with self.repo.lock:
                    row = self.repo.conn.execute(
                        "SELECT * FROM fact WHERE fact_type = ? AND subject = ? "
                        "AND scope = ?",
                        (end.fact_type, end.subject, end.scope)).fetchone()
                if row is not None:
                    want.append([dict(row).get(n) for n in names])
            return table(names, want) if want else ""
        found = self.repo.search(query)
        return json.dumps(found, ensure_ascii=False) if found else ""


class Answers:
    """Служба, отвечающая заданным набором связей и фактов. Без базы вовсе."""

    def __init__(self, links, facts):
        self.links, self.facts = links, facts

    def read(self, query, mode="single-answer", timeout=None):
        keys = quoted(query)
        if "Association" in query:
            return table(["source_key", "target_key", "weight"],
                         [l for l in self.links if l[0] in keys or l[1] in keys])
        out = []
        for key in keys:
            if key in self.facts:
                end = models.Fact.of_identity(key)
                out.append([end.fact_type, end.subject, end.scope, self.facts[key]])
        return table(["fact_type", "subject", "scope", "content"], out) if out else ""


class Deaf:
    """Дверь без обхода: такова консоль, и она в строю."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, text, wait=False):
        return self.inner.write(text, wait=wait)

    def read(self, query, mode="single"):
        return self.inner.read(query, mode=mode)


@contextlib.contextmanager
def network(base, reader=None):
    """Настоящая сетевая дверь, у которой за читателем стоит та же база.

    Подменяется только `api.read` — сам шаг, разбор ответа и выбор пути в
    `port.door` остаются рабочими, иначе проверка зеленела бы на коде, который
    по графу не ходит.
    """
    service = Service(base)
    try:
        with mock.patch.object(api, "read", reader or service.read):
            yield port.door("api"), service
    finally:
        service.close()


def texts(kept):
    return [text for _, text, _ in kept]


def by_graph(kept):
    return [record for _, _, record in kept
            if isinstance(record, dict) and record.get("via_graph")]


def scored_subjects(kept):
    return sorted((score, record.get("subject") or "")
                  for score, _, record in kept
                  if isinstance(record, dict) and record.get("via_graph"))


def subjects(records):
    return [r.get("subject") or "" for r in records]


def identities(pairs):
    return sorted("%s|%s|%s" % (r.get("fact_type"), r.get("subject"), r.get("scope"))
                  for r, _ in pairs)


# --- свойства самого шага ----------------------------------------------------

KEY = st.builds(lambda t, s, c: "%s|%s|%s" % (t, s, c),
                st.sampled_from(["project_state", "user", "workflow"]),
                st.text(alphabet="abcdef'|/. ", min_size=1, max_size=8),
                st.sampled_from(["project", "global"]))


class TestTheStepItself(unittest.TestCase):
    """Свойства шага, не зависящие от того, чья база под службой."""

    @given(keys=st.lists(KEY, min_size=1, max_size=4, unique=True),
           others=st.lists(KEY, min_size=1, max_size=6, unique=True),
           weights=st.lists(st.floats(0.1, 9.0), min_size=1, max_size=6))
    @FAST
    def test_the_asked_one_never_comes_back_as_its_own_neighbour(self, keys, others,
                                                                 weights):
        others = [o for o in others if o not in keys]
        links = [(keys[0], o, weights[i % len(weights)]) for i, o in enumerate(others)]
        links += [(keys[0], keys[0], 5.0)]
        facts = {o: "сосед" for o in others}
        facts[keys[0]] = "источник"
        got = graph.neighbours(Answers(links, facts).read, keys, limit=10)
        self.assertNotIn(keys[0], identities(got))

    @given(others=st.lists(KEY, min_size=1, max_size=8, unique=True),
           weights=st.lists(st.floats(0.1, 9.0), min_size=1, max_size=8),
           limit=st.integers(min_value=1, max_value=5))
    @FAST
    def test_heavy_links_come_first_and_the_ceiling_holds(self, others, weights, limit):
        key = "user|источник|global"
        others = [o for o in others if o != key]
        links = [(key, o, weights[i % len(weights)]) for i, o in enumerate(others)]
        got = graph.neighbours(Answers(links, {o: "с" for o in others}).read,
                               [key], limit=limit)
        self.assertLessEqual(len(got), limit)
        got_weights = [w for _, w in got]
        self.assertEqual(got_weights, sorted(got_weights, reverse=True))

    @given(others=st.lists(KEY, min_size=1, max_size=6, unique=True))
    @FAST
    def test_a_link_without_its_row_brings_nobody(self, others):
        """Связь пережила факт: конец есть, строки нет. Выдумывать нечего."""
        key = "user|источник|global"
        others = [o for o in others if o != key]
        links = [(key, o, 1.0) for o in others]
        got = graph.neighbours(Answers(links, {}).read, [key], limit=10)
        self.assertEqual(got, [])

    @given(others=st.lists(KEY, min_size=1, max_size=6, unique=True))
    @FAST
    def test_every_neighbour_is_a_row_the_service_reported(self, others):
        key = "user|источник|global"
        others = [o for o in others if o != key]
        facts = {o: "содержимое %d" % i for i, o in enumerate(others)}
        links = [(key, o, 1.0 + i) for i, o in enumerate(others)]
        got = graph.neighbours(Answers(links, facts).read, [key], limit=10)
        for name in identities(got):
            self.assertIn(name, facts)
        for record, _ in got:
            self.assertEqual(record.get("object_type"), "Fact")
            self.assertTrue(record.get("content"))

    @given(others=st.lists(KEY, min_size=1, max_size=6, unique=True))
    @FAST
    def test_a_neighbour_arrives_no_matter_which_end_of_the_link_it_sits_on(self, others):
        """Связь ненаправленная: сосед приходит и слева, и справа от источника."""
        key = "user|источник|global"
        others = [o for o in others if o != key]
        facts = {o: "сосед" for o in others}
        left = graph.neighbours(
            Answers([(key, o, 1.0 + i) for i, o in enumerate(others)], facts).read,
            [key], limit=10)
        right = graph.neighbours(
            Answers([(o, key, 1.0 + i) for i, o in enumerate(others)], facts).read,
            [key], limit=10)
        self.assertEqual(identities(left), identities(right))

    def test_a_key_with_a_quote_survives_the_question(self):
        """Ключ с кавычкой не должен рвать вопрос: тема — свободный текст."""
        key = "user|о'ключ|global"
        other = "user|сосед|global"
        got = graph.neighbours(Answers([(key, other, 2.0)], {other: "текст"}).read,
                               [key], limit=10)
        self.assertEqual(identities(got), [other])

    def test_a_key_with_the_separator_inside_the_subject_survives_the_round_trip(self):
        """Тема с разделителем: разбор с краёв, иначе сосед теряется молча."""
        key = "user|a|b|global"
        other = "workflow|c|d|project"
        got = graph.neighbours(Answers([(key, other, 2.0)], {other: "текст"}).read,
                               [key], limit=10)
        self.assertEqual(identities(got), [other])


class TestTheServiceMayAnswerAnything(unittest.TestCase):
    """Читатель отвечает и прозой, и пустотой, и мусором. Шаг молчит, не падает."""

    def test_silence_is_no_neighbours(self):
        self.assertEqual(graph.neighbours(lambda *a, **k: "", ["user|a|global"]), [])

    def test_prose_instead_of_a_table_is_no_neighbours(self):
        self.assertEqual(graph.neighbours(lambda *a, **k: "ничего не нашлось",
                                          ["user|a|global"]), [])

    def test_columns_the_step_did_not_ask_for_are_no_neighbours(self):
        answer = table(["что-то", "ещё"], [["a", "b"]])
        self.assertEqual(graph.neighbours(lambda *a, **k: answer, ["user|a|global"]), [])

    def test_links_without_their_weight_are_no_neighbours(self):
        """Веса нет — порядок соседей задать нечем, а придумать его нельзя.

        Факт при этом лежит на месте и пришёл бы: не будь сверки колонок,
        сосед уехал бы в выдачу с весом, взятым из ниоткуда.
        """
        answers = [table(["source_key", "target_key"],
                         [["user|a|global", "user|b|global"]]),
                   table(["fact_type", "subject", "scope", "content"],
                         [["user", "b", "global", "сосед"]])]

        def read(query, mode="single-answer", timeout=None):
            return answers.pop(0)

        self.assertEqual(graph.neighbours(read, ["user|a|global"]), [])

    def test_a_row_that_does_not_fill_its_columns_brings_nobody(self):
        """Строка короче своих колонок — не строка: половина полей не пришла."""
        answers = [table(["source_key", "target_key", "weight"],
                         [["user|a|global", "user|b|global", 2.0]]),
                   table(["fact_type", "subject", "scope", "content"],
                         [["user", "b", "global"]])]

        def read(query, mode="single-answer", timeout=None):
            return answers.pop(0)

        self.assertEqual(graph.neighbours(read, ["user|a|global"]), [])

    def test_nothing_asked_when_there_are_no_keys(self):
        calls = []

        def read(query, mode="single-answer", timeout=None):
            calls.append(query)
            return ""

        self.assertEqual(graph.neighbours(read, []), [])
        self.assertEqual(calls, [])

    def test_the_facts_are_not_asked_about_when_no_link_came_back(self):
        """Пустой ответ про связи закрывает шаг: второй вопрос задавать не о чем."""
        calls = []

        def read(query, mode="single-answer", timeout=None):
            calls.append(query)
            return ""

        self.assertEqual(graph.neighbours(read, ["user|a|global"]), [])
        self.assertEqual(len(calls), 1)

    def test_the_step_asks_for_raw_tables(self):
        """Пересказ словами разобрать нельзя: спрашиваем сырой результат."""
        modes = []

        def read(query, mode="single-answer", timeout=None):
            modes.append(mode)
            return ""

        graph.neighbours(read, ["user|a|global"])
        self.assertEqual(modes, ["raw-tables"])


# --- обе двери на одном наборе ----------------------------------------------

class TestBothDoorsGiveTheSameNeighbours(unittest.TestCase):
    """Одна выдача на одном наборе: локальная дверь и сетевая согласны."""

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=4,
                                   unique=True), min_size=1, max_size=3))
    def test_the_same_set_gives_the_same_neighbours(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, shape))
            here = port.door()
            keys = suggest.keys_of(suggest.pieces(here.read(shape[0][0], mode="raw")))
            self.assertTrue(keys, "прямых попаданий нет, сравнивать нечего")
            mine = here.neighbours(keys, limit=suggest.MAX_NEAR)
            with network(base) as (there, _):
                theirs = there.neighbours(keys, limit=suggest.MAX_NEAR)
            self.assertTrue(mine, "локальная дверь соседей не дала")
            self.assertEqual(identities(theirs), identities(mine))

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=3,
                                   unique=True), min_size=1, max_size=3))
    def test_the_suggestion_reads_the_graph_through_the_network_door(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, shape))
            with network(base) as (there, _):
                _, kept, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                             door=there)
            self.assertTrue(by_graph(kept), "сетевая дверь соседей не принесла")

    def test_a_file_edited_together_arrives_over_the_network(self):
        """Спросили про один файл — пришёл и тот, что правился вместе с ним."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, [["db.py", "port.py"]]))
            with network(base) as (there, _):
                _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                             door=there)
            near = subjects(by_graph(kept))
            self.assertTrue(any("port.py" in one for one in near),
                            "сосед по графу не пришёл: %s" % near)

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(NAMES), min_size=2, max_size=3,
                                   unique=True), min_size=1, max_size=3))
    def test_the_whole_suggestion_matches_door_to_door(self, shape):
        """Правило одно на обе двери: те же соседи и то же затухание.

        Сравниваются оценки вместе с темами: разъедься затухание — половины
        разойдутся цифрой при одинаковом наборе имён.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, shape))
            here = port.door()
            _, mine, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                         door=here)
            with network(base) as (there, _):
                _, theirs, _ = suggest.suggest(shape[0][0], mode="raw", min_score=0.0,
                                               door=there)
            self.assertEqual(scored_subjects(theirs), scored_subjects(mine))


class TestBothNetworkPathsCanWalk(unittest.TestCase):
    """Правило одно на оба сетевых пути: и прямой HTTP, и клиент."""

    def test_the_http_path_asks_its_own_reader(self):
        answers = Answers([("user|a|global", "user|b|global", 2.0)],
                          {"user|b|global": "сосед"})
        with mock.patch.object(api, "read", answers.read):
            got = api.neighbours(["user|a|global"], limit=3)
        self.assertEqual(identities(got), ["user|b|global"])

    def test_the_client_path_asks_its_own_reader(self):
        answers = Answers([("user|a|global", "user|b|global", 2.0)],
                          {"user|b|global": "сосед"})
        with mock.patch.object(sdk, "read", answers.read):
            got = sdk.neighbours(["user|a|global"], limit=3)
        self.assertEqual(identities(got), ["user|b|global"])

    def test_both_network_paths_answer_the_same(self):
        answers = Answers([("user|a|global", "user|b|global", 2.0),
                           ("user|c|global", "user|a|global", 5.0)],
                          {"user|b|global": "сосед", "user|c|global": "второй"})
        with mock.patch.object(api, "read", answers.read):
            over_http = api.neighbours(["user|a|global"], limit=5)
        with mock.patch.object(sdk, "read", answers.read):
            over_client = sdk.neighbours(["user|a|global"], limit=5)
        self.assertEqual(identities(over_http), identities(over_client))
        self.assertEqual([w for _, w in over_http], [w for _, w in over_client])

    def test_the_network_door_reaches_the_adapter(self):
        """Дверь спрашивает умение, а не имя: обход обязан доехать до адаптера."""
        answers = Answers([("user|a|global", "user|b|global", 2.0)],
                          {"user|b|global": "сосед"})
        with mock.patch.object(api, "read", answers.read):
            door = port.door("api")
            self.assertEqual(identities(door.neighbours(["user|a|global"], limit=3)),
                             ["user|b|global"])

    def test_every_our_door_can_walk(self):
        """Умение — свойство двери, а не имя пути. Проверяем все наши пути."""
        for name in ("api", "sdk", "local"):
            self.assertTrue(hasattr(port.door(name), "neighbours"), name)
            self.assertTrue(callable(getattr(port.ADAPTERS[name](), "neighbours",
                                             None)), name)


class TestTheSilentPathStays(unittest.TestCase):
    """Дверь, которая обхода не умеет, работает молча и без ошибок."""

    def test_the_console_door_has_no_walk_at_all(self):
        self.assertFalse(hasattr(port.door("cli"), "neighbours"))

    def test_a_path_without_the_walk_is_refused_by_name_not_by_crash(self):
        door = port.StructuredDoor(mock.Mock(spec=["read", "write_text"]), "чужой")
        with self.assertRaises(AttributeError):
            door.neighbours(["user|a|global"])

    def test_the_suggestion_survives_a_door_without_the_walk(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [["db.py", "port.py"]]))
            live = port.door()
            _, rich, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=live)
            _, plain, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                          door=Deaf(live))
            self.assertTrue(by_graph(rich), "живая дверь соседей не дала")
            self.assertEqual(by_graph(plain), [])
            self.assertEqual(texts(plain),
                             [t for t, r in zip(texts(rich), [x[2] for x in rich])
                              if not (isinstance(r, dict) and r.get("via_graph"))])

    def test_a_reader_that_fails_leaves_the_search_alone(self):
        """Читатель упал на обходе — подсказка отдаёт то, что нашла поиском."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, [["db.py", "port.py"]]))
            plain = Service(base)

            def angry(query, mode="single-answer", timeout=None):
                if "Association" in query:
                    raise api.ApiError("читатель недоступен")
                return plain.read(query, mode=mode, timeout=timeout)

            try:
                with network(base, reader=angry) as (there, _):
                    _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                                 door=there)
            finally:
                plain.close()
            self.assertTrue(kept, "поиск потерялся вместе с обходом")
            self.assertEqual(by_graph(kept), [])


if __name__ == "__main__":
    unittest.main()
