#!/usr/bin/env python3
"""Факт уходит структурой и держится за свой эпизод.

Запуск: python3 -m unittest tests.test_episode_facts -v

Разговор уже ложился в хранилище структурой, а факт из того же прохода уходил
прозой: ключ ему выводил разборщик текста, и связи с эпизодом не возникало
вовсе. В базе на 2026-08-30 было 85 522 связи разговор — событие и ни одной
эпизод — факт. Факт без эпизода не с чем сопоставить, поэтому ни ассоциация,
ни всплытие по зацепке на нём не строятся.

Проверки заданы свойствами, а не примерами. Правил тут немного, но краёв
много: эпизод без фактов, факт без эпизода, порог, отсеявший всё, повторный
заход по тому же архиву, дверь без структурной записи. Перечисляя края руками,
перечислишь не все — а пропущенный край здесь стоит потерянной связи.
"""
import contextlib, io, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import understand
from storage import db, local, port

CWD = "/home/person/dev/demo"
BRANCH = "episode-facts"

# Перебор ходит по диску и разбирает архив: срок примера меряет скорость диска,
# а не наш код.
SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

# Просьбы человека. Первые три попадают в темы предпочтений из
# archive/extract.PREF_TOPICS, последняя не попадает ни в одну: эпизод без
# единого факта — такой же край, как эпизод с фактами.
REQUESTS = st.sampled_from([
    "Отвечай кратко, длинные ответы не читаю",
    "Не выдумывай, перепроверь по документации",
    "Задавай вопросы по одному",
    "Посмотри, что там с базой",
])

FILES = st.lists(st.sampled_from(["%s/db.py" % CWD, "%s/port.py" % CWD,
                                  "/home/person/.claude/settings.json"]),
                 max_size=3, unique=True)

REPLIES = st.sampled_from([
    "Готово.",
    "Готово. Документация https://example.org/db тут.",
])

ERRORS = st.sampled_from(["", "FileNotFoundError: db.py", "exit code 1"])

# Разговор без идентификатора в архиве встречается: у ключа эпизода не должно
# быть ни одной пустой половины, иначе запись некуда положить.
SESSIONS = st.sampled_from(["разговор-1", "разговор-2", ""])

EPISODES = st.builds(lambda request, files, reply, error: {
    "request": request, "files": files, "reply": reply, "error": error},
    request=REQUESTS, files=FILES, reply=REPLIES, error=ERRORS)

# Архив: список файлов, у каждого свой разговор и от одного до трёх эпизодов.
# Разговоры разные: два файла под одним именем — это один ключ эпизода на двоих,
# и такое схлопывание меряется отдельно, а не примешивается к каждому свойству.
ARCHIVES = st.lists(st.tuples(SESSIONS, st.lists(EPISODES, min_size=1, max_size=3)),
                    min_size=1, max_size=2, unique_by=lambda item: item[0])


def rows(session, specs):
    """Строки транскрипта по описанию эпизодов."""
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        out.append({"type": "user", "sessionId": session, "timestamp": stamp,
                    "cwd": CWD, "gitBranch": BRANCH,
                    "message": {"content": spec["request"]}})
        blocks = [{"type": "tool_use", "name": "Edit", "input": {"file_path": target}}
                  for target in spec["files"]]
        if spec["error"]:
            blocks.append({"type": "tool_result", "is_error": True,
                           "content": spec["error"]})
        blocks.append({"type": "text", "text": spec["reply"]})
        out.append({"type": "assistant", "sessionId": session, "timestamp": stamp,
                    "cwd": CWD, "gitBranch": BRANCH,
                    "message": {"content": blocks}})
    return out


def archive(root, shape):
    """Архив заданной формы. Отдаёт пути файлов в том же порядке."""
    out = []
    for number, (session, specs) in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(session, specs):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.append(path)
    return out


class Spy:
    """Настоящая дверь, которая помнит, чем её звали.

    Обёртка, а не подделка: свойства спрашивают и про форму вызова, и про то,
    что от него осталось в базе. Подделка ответила бы только на первое.
    """

    def __init__(self, inner):
        self.inner, self.texts, self.batches = inner, [], []

    def write(self, text, wait=False):
        self.texts.append(text)
        return self.inner.write(text, wait=wait)

    def write_objects(self, records, relations=()):
        self.batches.append((list(records), list(relations)))
        return self.inner.write_objects(records, relations)

    def read(self, query, mode="single"):
        return self.inner.read(query, mode=mode)

    def records(self, kind):
        return [r for batch, _ in self.batches for r in batch if isinstance(r, kind)]

    def pairs(self):
        """Сколько разных связей эпизод — факт должно получиться из записанного.

        Ключ факта — тип, тема и охват, содержание в него не входит. Два факта
        одного эпизода с общей темой — например два препятствия одного проекта —
        это одна строка и одна связь, а не две. Считать связи по числу фактов
        значит требовать от базы того, чего схема не обещала.
        """
        out = set()
        for batch, _ in self.batches:
            episode = [r for r in batch if isinstance(r, models.Episode)]
            for fact in (r for r in batch if isinstance(r, models.Fact)):
                out.add((tuple(episode[0].key().items()),
                         tuple(fact.key().items())))
        return out


class TextOnly:
    """Дверь без структурной записи. Такова консоль, и она остаётся в строю."""

    def __init__(self):
        self.texts = []

    def write(self, text, wait=False):
        self.texts.append(text)
        return ""


@contextlib.contextmanager
def store(tmp):
    """Своя база на прогон. Адаптер держит репозиторий на процесс — закрываем."""
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()


def relations(base):
    """Связи базы: имя связи -> сколько их. Строка на конец, связь на link_id."""
    conn = sqlite3.connect(str(base))
    try:
        return {name: number for name, number in conn.execute(
            "SELECT relation, count(DISTINCT link_id) FROM links GROUP BY relation")}
    finally:
        conn.close()


def endpoints(base, relation):
    """Концы связей: link_id -> роль -> (тип объекта, ключ словарём)."""
    conn = sqlite3.connect(str(base))
    try:
        found = {}
        for link_id, role, object_type, object_key in conn.execute(
                "SELECT link_id, role, object_type, object_key FROM links "
                "WHERE relation = ?", (relation,)):
            found.setdefault(link_id, {})[role] = (object_type, json.loads(object_key))
        return found
    finally:
        conn.close()


def counts(base):
    repo = db.Repository(base)
    try:
        return repo.counts()
    finally:
        repo.close()


def has_row(base, cls, key):
    """Лежит ли в базе строка с таким первичным ключом."""
    where = " AND ".join('"%s" = ?' % name for name in key)
    conn = sqlite3.connect(str(base))
    try:
        return bool(conn.execute('SELECT 1 FROM "%s" WHERE %s'
                                 % (cls.OBJECT.lower(), where),
                                 list(key.values())).fetchone())
    finally:
        conn.close()


def digest(files, door, **kwargs):
    kwargs.setdefault("dry", False)
    return understand.digest(files, door=door, **kwargs)


class TestFactGoesAsStructure(unittest.TestCase):
    """Факт уходит записью схемы, а не прозой, — тем же путём, что разговор."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_structured_door_is_never_asked_to_write_text(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, shape)
            spy = Spy(port.door())
            got = digest(files, spy)
            self.assertEqual(spy.texts, [],
                             "структурная дверь получила прозу: %s"
                             % (spy.texts[:1] or [""])[0][:120])
            self.assertEqual(len(spy.records(models.Fact)), got["facts"])
            self.assertEqual(len(spy.records(models.Episode)), got["episodes"])

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_fact_key_is_ours_not_guessed(self, shape):
        """Ключ записи совпадает с ключом схемы, который назвали мы сами."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, shape)
            spy = Spy(port.door())
            digest(files, spy)
            want = set()
            for path in files:
                for episode in understand.episodes_from_file(path):
                    for fact, _ in understand.marked_or_guessed(episode)[0]:
                        want.add((fact[0], fact[1], fact[2]))
            got = {(r.fact_type, r.subject, r.scope)
                   for r in spy.records(models.Fact)}
            self.assertEqual(got, want)

    @SLOW
    @given(shape=ARCHIVES)
    def test_every_key_half_is_filled(self, shape):
        """Ни одна половина первичного ключа не уходит пустой."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            spy = Spy(port.door())
            digest(archive(tmp, shape), spy)
            for batch, _ in spy.batches:
                for record in batch:
                    for name, value in record.key().items():
                        self.assertNotIn(value, (None, ""),
                                         "%s: пустое поле ключа %s"
                                         % (record.OBJECT, name))


class TestEpisodeAndFactAreLinked(unittest.TestCase):
    """Связь episode_facts: у каждого записанного факта есть свой эпизод."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_store_gets_one_link_per_written_fact(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            spy = Spy(port.door())
            got = digest(archive(tmp, shape), spy)
            self.assertEqual(relations(base).get("episode_facts", 0), len(spy.pairs()))
            self.assertEqual(bool(relations(base).get("episode_facts", 0)),
                             bool(got["facts"]),
                             "факты записаны, а связей под них нет")

    def test_a_plain_archive_leaves_links_behind(self):
        """Один прогон на обычном разговоре: связей стало больше нуля."""
        shape = [("разговор-1", [{"request": "Отвечай кратко",
                                  "files": ["%s/db.py" % CWD],
                                  "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = digest(archive(tmp, shape), Spy(port.door()))
            self.assertGreater(got["facts"], 0)
            self.assertGreater(relations(base).get("episode_facts", 0), 0,
                               "в базе нет ни одной связи эпизод — факт")

    @SLOW
    @given(shape=ARCHIVES)
    def test_both_ends_of_every_link_are_rows_in_the_store(self, shape):
        """Конец связи — не выдуманный ключ, а строка, которая лежит в базе."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape), Spy(port.door()))
            for link_id, ends in endpoints(base, "episode_facts").items():
                self.assertEqual(set(ends), {"episode", "fact"})
                self.assertEqual(ends["episode"][0], "Episode")
                self.assertEqual(ends["fact"][0], "Fact")
                self.assertTrue(has_row(base, models.Episode, ends["episode"][1]),
                                "связь %s ссылается на эпизод, которого нет" % link_id)
                self.assertTrue(has_row(base, models.Fact, ends["fact"][1]),
                                "связь %s ссылается на факт, которого нет" % link_id)

    @SLOW
    @given(shape=ARCHIVES, threshold=st.sampled_from([0.0, 0.2, 0.5, 2.0]))
    def test_the_threshold_moves_facts_and_links_together(self, shape, threshold):
        """Отсеянный порогом факт не пишется — и связи под него не появляется."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            spy = Spy(port.door())
            got = digest(archive(tmp, shape), spy, min_score=threshold)
            self.assertEqual(relations(base).get("episode_facts", 0), len(spy.pairs()))
            if threshold > 1.0:
                self.assertEqual(got["facts"], 0)
                self.assertEqual(relations(base).get("episode_facts", 0), 0)
                self.assertGreater(got["episodes"], 0,
                                   "порог съел не только факты, но и эпизоды")

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_second_pass_adds_nothing(self, shape):
        """Повтор по тому же архиву не плодит ни строк, ни связей."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files, Spy(port.door()))
            was, links_was = counts(base), relations(base)
            digest(files, Spy(port.door()), reset=True)
            self.assertEqual(counts(base), was)
            self.assertEqual(relations(base), links_was)


class TestAFactCarriesOnlyWhatIsTrueOfIt(unittest.TestCase):
    """Поля записи факта. Ошибка здесь тише всего: запись ложится и находится."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_global_fact_belongs_to_no_project(self, shape):
        """Предпочтение человека — про человека, а не про проект.

        Поиск взвешивает `project` наравне с темой, а вес факта выше всех
        прочих: приписанное предпочтение всплывает первым на любой вопрос про
        этот проект и вытесняет то, ради чего вопрос задан.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            spy = Spy(port.door())
            digest(archive(tmp, shape), spy)
            for record in spy.records(models.Fact):
                if record.scope == "global":
                    self.assertIsNone(record.project,
                                      "глобальному факту приписан проект %s"
                                      % record.project)
                else:
                    self.assertEqual(record.project, Path(CWD).name)


class TestTheReportKeepsSecrets(unittest.TestCase):
    """Отчёт на экране уходит в журнал хука на каждом ходе."""

    def test_a_secret_in_the_request_is_not_printed(self):
        secret = "xmem_LiveKeyDoNotPrint12345"
        shape = [("разговор-1", [{"request": "Отвечай кратко, ключ %s" % secret,
                                  "files": [], "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            spy = Spy(port.door())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                digest(archive(tmp, shape), spy, verbose=True)
            self.assertIn("EPISODE", out.getvalue(), "отчёт вообще не печатался")
            self.assertNotIn(secret, out.getvalue())
            # Поле записи держит исходную строку, вычистка стоит на выходе
            # (Record.values). Спрашиваем то, что уходит наружу, а не поле.
            for record in spy.records(models.Episode):
                self.assertNotIn(secret, json.dumps(record.mutation(),
                                                    ensure_ascii=False))
            conn = sqlite3.connect(str(base))
            try:
                rows = conn.execute("SELECT title, summary FROM episode").fetchall()
            finally:
                conn.close()
            self.assertTrue(rows, "эпизод не записан, проверять нечего")
            for row in rows:
                self.assertNotIn(secret, " ".join(x or "" for x in row))


class TestFactsOfDifferentFilesStayApart(unittest.TestCase):
    """Ключ факта не знает про содержание — знать его должна подпись.

    Правка двух файлов в одном эпизоде даёт два факта, и подписаны они путями
    файлов, а не проектом: в базе две строки. Раньше подписью служило имя
    проекта, обе правки ложились в одну строку, и вторая затирала первую.
    Свойствами это меряется в `tests/test_fact_identity.py`, здесь стоит
    один пример — чтобы счётчик прохода и содержимое базы сверялись явно.
    """

    def test_two_edits_of_one_episode_become_two_rows(self):
        shape = [("разговор-1", [{"request": "Посмотри, что там с базой",
                                  "files": ["%s/db.py" % CWD, "%s/port.py" % CWD],
                                  "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            spy = Spy(port.door())
            got = digest(archive(tmp, shape), spy)
            keys = {tuple(r.key().items()) for r in spy.records(models.Fact)}
            self.assertEqual(got["facts"], 2, "правки перестали давать по факту")
            self.assertEqual(len(keys), 2, "два файла снова делят одну подпись")
            self.assertEqual(counts(base)["Fact"], 2)
            self.assertEqual(relations(base).get("episode_facts", 0), 2)

    def test_one_key_still_means_one_row(self):
        """Где подпись всё же общая, схлопывание остаётся — и это не случайность.

        Препятствие подписано проектом: два разных препятствия одного проекта
        это одна строка, последняя. Трогать ключ ради содержания нельзя —
        `Association.source_key` адресует факт строкой `fact_type|subject|scope`.
        Проверка держит границу задачи: свою подпись получил файл, а не всё
        подряд.
        """
        shape = [("разговор-1", [{"request": "Посмотри, что там с базой",
                                  "files": [], "reply": "Готово.",
                                  "error": "FileNotFoundError: db.py"},
                                 {"request": "Посмотри, что там с базой",
                                  "files": [], "reply": "Готово.",
                                  "error": "FileNotFoundError: port.py"}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            spy = Spy(port.door())
            got = digest(archive(tmp, shape), spy)
            keys = {tuple(r.key().items()) for r in spy.records(models.Fact)}
            self.assertEqual(got["facts"], 2)
            self.assertEqual(len(keys), 1, "ключ препятствия стал знать содержание")
            self.assertEqual(counts(base)["Fact"], 1)


class TestTextDoorKeepsWorking(unittest.TestCase):
    """У консоли структурной записи нет. Она не должна остаться без записи."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_door_without_write_objects_still_gets_the_text(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = TextOnly()
            got = digest(archive(tmp, shape), door)
            self.assertEqual(len([t for t in door.texts if t.startswith("Episode")]),
                             got["episodes"])
            self.assertEqual(len([t for t in door.texts if t.startswith("Fact.")]),
                             got["facts"])
            self.assertEqual(len(door.texts), got["episodes"] + got["facts"])


class TestBothPathsSayTheSame(unittest.TestCase):
    """Проза и структура описывают один эпизод. Разъехаться им нельзя."""

    # Чего в структурной записи нет намеренно: пустое поле не шлём, а проза на
    # его месте пишет заглушку.
    STUBS = ("none", "unknown")

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_record_matches_the_rendered_text(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, shape)
            spy = Spy(port.door())
            digest(files, spy)
            written = {(r.session_id, r.episode_number): r
                       for r in spy.records(models.Episode)}
            for path in files:
                for episode in understand.episodes_from_file(path):
                    parsed = local.parse_text(understand.render_episode(episode))
                    self.assertIsNotNone(parsed, "проза эпизода перестала разбираться")
                    values = local._typed(*parsed)
                    record = written[(values["session_id"], values["episode_number"])]
                    for name, value in values.items():
                        if value in self.STUBS:
                            continue
                        self.assertEqual(getattr(record, name), value,
                                         "поле %s разошлось между прозой и записью" % name)


class TestLinkChecksItsEnds(unittest.TestCase):
    """Связь сверяет типы концов до отправки. Иначе битая ссылка ляжет в базу."""

    SAMPLES = {
        "Session": models.Session(session_id="s"),
        "Episode": models.Episode(session_id="s", episode_number=1,
                                  title="t", outcome="done"),
        "Event": models.Event(session_id="s", sequence_number=1,
                              event_type="user_message", content="c"),
        "Fact": models.Fact(fact_type="user", subject="s", scope="global",
                            content="c"),
        "Association": models.Association(source_key="a", target_key="b",
                                          cue="same_episode", weight=1.0),
        "MemoryInjection": models.MemoryInjection(session_id="s",
                                                  injected_at="2026-08-28T00:00:00Z"),
    }

    @given(episode=st.sampled_from(sorted(SAMPLES)),
           fact=st.sampled_from(sorted(SAMPLES)))
    def test_only_an_episode_and_a_fact_are_let_through(self, episode, fact):
        ends = {"episode": self.SAMPLES[episode], "fact": self.SAMPLES[fact]}
        if (episode, fact) == ("Episode", "Fact"):
            self.assertIn("relation_mutation", models.link("episode_facts", **ends))
            return
        with self.assertRaises(models.SchemaError):
            models.link("episode_facts", **ends)

    @given(name=st.sampled_from(models.Fact.KEY))
    def test_a_fact_with_a_hollow_key_is_not_linkable(self, name):
        """Ключ факта задаём мы. Дыра в нём — не связь, а ссылка в никуда."""
        fact = models.Fact(fact_type="user", subject="кто", scope="global",
                           content="c")
        setattr(fact, name, "")
        with self.assertRaises(models.SchemaError):
            models.link("episode_facts", episode=self.SAMPLES["Episode"], fact=fact)


if __name__ == "__main__":
    unittest.main()
