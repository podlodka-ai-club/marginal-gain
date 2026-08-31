#!/usr/bin/env python3
"""Эпизод нумеруется по разговору и связан с разговором.

Запуск: python3 -m unittest tests.test_episode_identity -v

Номер эпизода считался внутри файла архива, а ключ у эпизода — разговор плюс
номер. Разговор часто разложен по нескольким файлам (66 из 160 на 2026-08-31):
второй файл начинал счёт заново, и его первый эпизод затирал первый эпизод
первого файла. Так из 1944 эпизодов архива в хранилище доезжало 1736.

Это тот же класс, что уже чинили дважды: у событий — счётчиком по разговору,
у фактов — подписью по файлу. Ключ обязан различать то, что различно.

Свойствами, а не примерами: важно не «два файла дали четыре эпизода», а
«сколько эпизодов в архиве, столько строк в хранилище, и номера внутри
разговора идут подряд, чем бы архив ни был нарезан».
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import save, understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "episode-identity"

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

REQUESTS = st.sampled_from(["Отвечай кратко", "Посмотри, что там с базой",
                            "Почини порт"])

# Разговоры нарочно повторяются между файлами: ровно на этом ломался ключ.
SESSIONS = st.sampled_from(["разговор-1", "разговор-2", ""])

# Кусок архива: файл, в нём разговор и сколько в нём эпизодов.
CHUNKS = st.lists(st.tuples(SESSIONS, st.integers(min_value=1, max_value=3)),
                  min_size=1, max_size=4)


def rows(session, count):
    out = []
    for number in range(count):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD,
                "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": "Почини порт"}))
        out.append(dict(head, type="assistant", message={"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "%s/файл-%d.py" % (CWD, number)}},
            {"type": "text", "text": "Готово."}]}))
    return out


def archive(root, shape):
    """Файл на кусок. Один разговор может лечь в несколько файлов."""
    out = []
    for number, (session, count) in enumerate(shape):
        path = Path(root) / ("разговор-%02d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(session, count):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.append(path)
    return out


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(save, "STATE", Path(tmp) / "save.json"), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()


def episodes_in(base):
    """Строки эпизодов: (разговор, номер) -> запись."""
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return {(r["session_id"], r["episode_number"]): dict(r)
                for r in conn.execute("SELECT * FROM episode")}
    finally:
        conn.close()


def links_of(base, relation):
    conn = sqlite3.connect(str(base))
    try:
        found = {}
        for link_id, role, kind, key in conn.execute(
                "SELECT link_id, role, object_type, object_key FROM links "
                "WHERE relation = ?", (relation,)):
            found.setdefault(link_id, {})[role] = (kind, json.loads(key))
        return found
    finally:
        conn.close()


def has_row(base, table, key):
    where = " AND ".join('"%s" = ?' % name for name in key)
    conn = sqlite3.connect(str(base))
    try:
        return bool(conn.execute('SELECT 1 FROM "%s" WHERE %s' % (table, where),
                                 list(key.values())).fetchone())
    finally:
        conn.close()


def digest(files, **kwargs):
    kwargs.setdefault("dry", False)
    return understand.digest(files, door=port.door(), **kwargs)


def wanted(shape):
    """Сколько эпизодов у каждого разговора во всём архиве."""
    out = Counter()
    for session, count in shape:
        out[session or "unknown"] += count
    return out


class TestOneNumberPerEpisodeOfATalk(unittest.TestCase):
    """Номер эпизода уникален в разговоре, чем бы архив ни был нарезан."""

    @SLOW
    @given(shape=CHUNKS)
    def test_every_episode_of_the_archive_gets_a_row(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = digest(archive(tmp, shape))
            self.assertEqual(len(episodes_in(base)), sum(wanted(shape).values()),
                             "эпизоды затирают друг друга")
            self.assertEqual(got["episodes"], sum(wanted(shape).values()))

    @SLOW
    @given(shape=CHUNKS)
    def test_the_numbers_of_a_talk_run_without_gaps(self, shape):
        """Номера разговора — подряд от единицы: дыра значит потерянный эпизод."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            by_talk = {}
            for (talk, number) in episodes_in(base):
                by_talk.setdefault(talk, []).append(number)
            for talk, count in wanted(shape).items():
                self.assertEqual(sorted(by_talk.get(talk, [])),
                                 list(range(1, count + 1)), talk)

    @SLOW
    @given(shape=CHUNKS)
    def test_a_second_pass_keeps_the_same_numbers(self, shape):
        """Повтор не двигает номера: иначе каждый заход плодил бы эпизоды."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            was = episodes_in(base)
            digest(files, reset=True)
            self.assertEqual(set(episodes_in(base)), set(was))

    @SLOW
    @given(shape=CHUNKS, more=st.integers(min_value=1, max_value=2))
    def test_a_growing_archive_continues_the_count(self, shape, more):
        """Дописали разговор — номера продолжаются, прежние не двигаются."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            was = set(episodes_in(base))
            talk = shape[0][0] or "unknown"
            extra = Path(tmp) / "разговор-99.jsonl"
            with extra.open("a", encoding="utf-8") as fh:
                for line in rows(shape[0][0], more):
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            digest(files + [extra])
            now = set(episodes_in(base))
            self.assertTrue(was <= now, "прежние эпизоды сдвинулись или пропали")
            self.assertEqual(len(now), len(was) + more)


class TestTheEpisodeBelongsToItsTalk(unittest.TestCase):
    """Связь session_episodes: эпизод перестаёт быть островом."""

    @SLOW
    @given(shape=CHUNKS)
    def test_a_link_per_written_episode(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            self.assertEqual(len(links_of(base, "session_episodes")),
                             len(episodes_in(base)))

    @SLOW
    @given(shape=CHUNKS)
    def test_both_ends_of_the_link_are_rows(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            found = links_of(base, "session_episodes")
            self.assertTrue(found, "связей разговор — эпизод нет вовсе")
            for link_id, ends in found.items():
                self.assertEqual(set(ends), {"session", "episode"})
                self.assertEqual(ends["session"][0], "Session")
                self.assertEqual(ends["episode"][0], "Episode")
                self.assertTrue(has_row(base, "episode", ends["episode"][1]),
                                "связь %s ссылается на эпизод, которого нет" % link_id)

    @SLOW
    @given(shape=CHUNKS)
    def test_the_link_names_the_talk_of_the_episode(self, shape):
        """Конец связи — тот же разговор, что у эпизода, а не соседний."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            found = links_of(base, "session_episodes")
            self.assertTrue(found, "связей разговор — эпизод нет вовсе")
            for ends in found.values():
                self.assertEqual(ends["session"][1]["session_id"],
                                 ends["episode"][1]["session_id"])


class TestTheEpisodeOwnsItsEvents(unittest.TestCase):
    """Связь episode_events. Её концы считают два разных модуля.

    Номера событий знает сохранение, границы эпизодов — понимание. Связь
    сходится только если оба считают по одному правилу, поэтому свойства здесь
    гоняют оба прохода по одному архиву и спрашивают хранилище, а не код: конец
    связи обязан быть строкой, которая в базе лежит.
    """

    @SLOW
    @given(shape=CHUNKS)
    def test_both_ends_of_every_link_are_rows(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            save.ingest(files, dry=False, door=port.door())
            digest(files)
            found = links_of(base, "episode_events")
            self.assertTrue(found, "связей эпизод — событие нет вовсе")
            for link_id, ends in found.items():
                self.assertEqual(set(ends), {"episode", "event"})
                self.assertTrue(has_row(base, "episode", ends["episode"][1]),
                                "связь %s зовёт эпизод, которого нет" % link_id)
                self.assertTrue(has_row(base, "event", ends["event"][1]),
                                "связь %s зовёт событие, которого нет: %s"
                                % (link_id, ends["event"][1]))

    @SLOW
    @given(shape=CHUNKS)
    def test_every_event_of_a_talk_belongs_to_one_episode(self, shape):
        """Событие принадлежит ровно одному эпизоду, и это эпизод его разговора."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            save.ingest(files, dry=False, door=port.door())
            digest(files)
            found = links_of(base, "episode_events")
            self.assertTrue(found, "связей эпизод — событие нет вовсе")
            owners = {}
            for ends in found.values():
                event = (ends["event"][1]["session_id"],
                         ends["event"][1]["sequence_number"])
                self.assertNotIn(event, owners, "событие попало в два эпизода")
                owners[event] = ends["episode"][1]
                self.assertEqual(ends["episode"][1]["session_id"], event[0])

    @SLOW
    @given(shape=CHUNKS)
    def test_the_talk_of_the_archive_loses_no_event(self, shape):
        """Все события разговора, кроме предисловия, разобраны по эпизодам.

        Предисловие — записи до первой реплики человека: эпизода у них нет и
        быть не может. Всё остальное обязано найти свой эпизод, иначе половина
        разговора выпадает из графа молча.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            save.ingest(files, dry=False, door=port.door())
            digest(files)
            conn = sqlite3.connect(str(base))
            try:
                events = {(r[0], r[1]) for r in conn.execute(
                    "SELECT session_id, sequence_number FROM event")}
            finally:
                conn.close()
            linked = {(e["event"][1]["session_id"], e["event"][1]["sequence_number"])
                      for e in links_of(base, "episode_events").values()}
            self.assertEqual(events - linked, set(),
                             "события без эпизода: %s" % sorted(events - linked)[:3])


if __name__ == "__main__":
    unittest.main()
