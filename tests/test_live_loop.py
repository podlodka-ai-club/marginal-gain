#!/usr/bin/env python3
"""Конец хода замыкает петлю: связи пополняются, исход отмечается.

Запуск: python3 -m unittest tests.test_live_loop -v

Оба прохода — связи между фактами и отметка исхода вставки — были написаны и
остались ручными командами. Хук конца хода звал очередь и понимание, и только.
В живой работе это значит: граф застыл на том, что посчитали руками, а поле
«помогла ли память» не заполняется ни разу — ради него петля и строилась.

Проверки тут двух родов. Первый: проход умеет дёшево понять, что делать нечего —
конец хода зовётся каждый ход, и заход вхолостую обязан стоить ничего. Второй:
хук действительно зовёт оба прохода, а не только то, что звал раньше.
"""
import contextlib, json, os, re, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from pipeline import associate, suggest, understand
from storage import local, port

HERE = Path(__file__).resolve().parent.parent
CWD = "/home/person/dev/demo"
BRANCH = "live-loop"
TALK = "разговор-1"

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])


def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD,
                "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": "Посмотри, что там с базой"}))
        blocks = [{"type": "tool_use", "name": "Edit",
                   "input": {"file_path": "%s/%s" % (CWD, name)}} for name in spec]
        blocks.append({"type": "text", "text": "Готово."})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape, name="разговор.jsonl"):
    path = Path(root) / name
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(TALK, shape):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(associate, "STATE", Path(tmp) / "associate.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def cards(base):
    if not Path(base).exists():
        return 0
    conn = sqlite3.connect(str(base))
    try:
        return conn.execute("SELECT count(*) FROM association").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


class TestAnIdlePassCostsNothing(unittest.TestCase):
    """Заход, которому нечего делать, не открывает ни одного файла.

    Конец хода зовётся каждый ход. Проход по связям считает вес по всему архиву,
    то есть открывает три сотни файлов; делай он это на каждом ходе — цена хода
    выросла бы на секунды ради работы, которой нет.
    """

    @SLOW
    @given(shape=st.lists(st.lists(st.sampled_from(["db.py", "port.py", "run.py"]),
                                   min_size=2, max_size=3, unique=True),
                          min_size=1, max_size=2))
    def test_the_second_pass_opens_no_files(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, shape)
            understand.digest(files, door=port.door(), dry=False)
            associate.build(files, door=port.door(), dry=False)

            opened = []
            real = associate.episodes_from_file

            def counting(path):
                opened.append(path)
                return real(path)

            with mock.patch.object(associate, "episodes_from_file", counting):
                got = associate.build(files, door=port.door(), dry=False)
            self.assertEqual(opened, [], "холостой заход перечитал архив")
            self.assertTrue(got.get("idle"), "заход не назвался холостым")

    def test_a_changed_archive_wakes_the_pass_again(self):
        """Архив дорос — проход просыпается и досчитывает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, [["db.py", "port.py"]])
            understand.digest(files, door=port.door(), dry=False)
            associate.build(files, door=port.door(), dry=False)
            was = cards(base)

            files += archive(tmp, [["run.py", "api.py"]], name="разговор-2.jsonl")
            understand.digest(files, door=port.door(), dry=False)
            got = associate.build(files, door=port.door(), dry=False)
            self.assertFalse(got.get("idle"), "проход не заметил новых файлов")
            self.assertGreater(cards(base), was)

    def test_a_capped_run_does_not_close_the_archive(self):
        """Потолок оставляет карточки ненаписанными — архив закрывать рано.

        Закрой мы его, остаток не досчитался бы никогда: следующий заход увидел
        бы неизменившийся архив и вышел холостым.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, [["db.py", "port.py", "run.py"]])
            understand.digest(files, door=port.door(), dry=False)
            associate.build(files, door=port.door(), dry=False, limit=1)
            self.assertEqual(cards(base), 1)
            got = associate.build(files, door=port.door(), dry=False)
            self.assertFalse(got.get("idle"), "архив закрыт под потолком")
            self.assertGreater(cards(base), 1)

    def test_a_dry_run_does_not_close_the_archive(self):
        """Холостой прогон ничего не пишет и потому ничего не закрывает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            files = archive(tmp, [["db.py", "port.py"]])
            understand.digest(files, door=port.door(), dry=False)
            associate.build(files, dry=True)
            got = associate.build(files, door=port.door(), dry=False)
            self.assertFalse(got.get("idle"),
                             "проба на сухую отняла архив у записи")


class TestTheOutcomeIsSettledAtTheEndOfTheTurn(unittest.TestCase):
    """Отметка исхода идёт по тем же файлам, что разбирает понимание."""

    def test_an_injection_of_this_talk_gets_its_mark(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, [["db.py"]])
            understand.digest(files, door=port.door(), dry=False)
            door = port.door()
            # Раньше эпизода: отметка смотрит на первый ход, начавшийся после
            # вставки. Эпизоды архива здесь стоят на полуночи.
            suggest.note_injection(TALK, "Из памяти", (), door=door,
                                   at="2026-08-27T23:00:00Z")
            suggest.settle(files, door=door)
            conn = sqlite3.connect(str(base))
            try:
                row = conn.execute("SELECT session_outcome, helped FROM memoryinjection"
                                   ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], "done")
            self.assertEqual(row[1], 1)


class TestTheCommandsAcceptWhatTheHookPasses(unittest.TestCase):
    """Хук зовёт командную строку. Флаг, которого нет, роняет проход молча.

    Хук пишет в журнал и всегда выходит нулём: несуществующий ключ не заметен
    ни в разговоре, ни по коду возврата. Поэтому проверяем разбор аргументов
    ровно в той форме, в какой их подставляет хук.
    """

    def test_settle_takes_the_only_flag(self):
        body = (HERE / "hooks" / "on_stop.sh").read_text(encoding="utf-8")
        self.assertIn("--settle --only", body)
        parser = suggest.parser()
        args = parser.parse_args(["--settle", "--only", "/tmp/проект/"])
        self.assertTrue(args.settle)
        self.assertEqual(args.only, "/tmp/проект/")

    def test_the_association_pass_takes_send(self):
        parser = associate.parser()
        args = parser.parse_args(["--send"])
        self.assertFalse(args.dry)

    def test_settle_looks_only_at_the_named_archive(self):
        """Сужение до проекта: чужие разговоры хук не разбирает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            mine = Path(tmp) / "мой"
            other = Path(tmp) / "чужой"
            mine.mkdir(); other.mkdir()
            archive(mine, [["db.py"]])
            archive(other, [["port.py"]])
            self.assertEqual([p.parent.name for p in suggest.transcripts(str(mine) + "/")],
                             ["мой"])


class TestTheHookCallsBothPasses(unittest.TestCase):
    """Хук конца хода. Написанный и не подключённый проход — то же, что ненаписанный.

    Проверяем текст хука, а не поведение: запускать живой конец хода из теста
    значит писать в хранилище пользователя. Проверка грубая, но ловит ровно то,
    ради чего задача: вызова нет вовсе.
    """

    def setUp(self):
        self.body = (HERE / "hooks" / "on_stop.sh").read_text(encoding="utf-8")

    def test_the_association_pass_is_called(self):
        self.assertIn("pipeline.associate", self.body,
                      "связи не пополняются: граф застынет на том, что посчитали руками")

    def test_the_settle_pass_is_called(self):
        self.assertIn("--settle", self.body,
                      "исход вставки не отмечается: поле helped останется пустым")

    def test_both_go_after_understanding(self):
        """Порядок: связи ставятся на факты, которые понимание только что записало."""
        order = [self.body.index("pipeline.drain"),
                 self.body.index("pipeline.understand"),
                 self.body.index("pipeline.associate")]
        self.assertEqual(order, sorted(order), "проходы идут не по порядку")

    def test_the_hook_still_guards_the_gate(self):
        """Ворота на месте: хук не имеет права работать в чужом проекте."""
        self.assertIn("live ||", self.body)


if __name__ == "__main__":
    unittest.main()
