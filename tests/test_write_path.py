#!/usr/bin/env python3
"""Проверки пути записи. Запуск: python3 -m unittest tests.test_write_path -v

Замер показал ноль вклада памяти. Разбор упёрся в то, что база наполнена не
продуктом, а ручными пробами: 625 узлов против 15 в архиве. Эти проверки
задают вопрос, на который до сих пор отвечали словами, — доходит ли запись до
хранилища, если её никто не ведёт за руку.

Наполнять базу в обход конвейера нельзя: тогда замер меряет наполнение, а не
продукт. Поэтому сначала красный тест, потом починка модуля записи.
"""
import contextlib, json, os, re, signal, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from infra import locks
from pipeline import drain
from pipeline import save
from domain import models
from storage import db
from pipeline import understand
from storage import local

HERE = Path(__file__).resolve().parent.parent

SESSION = "test-session-1"
TRANSCRIPT = [
    {"type": "user", "sessionId": SESSION, "timestamp": "2026-08-26T10:00:00Z",
     "cwd": "/home/person/dev/job-hunt", "gitBranch": "memory-encoder",
     "message": {"content": "Отвечай кратко. Почини разбор в db.py"}},
    {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-08-26T10:00:30Z",
     "cwd": "/home/person/dev/job-hunt", "gitBranch": "memory-encoder",
     "message": {"content": [
         {"type": "tool_use", "name": "Edit",
          "input": {"file_path": "/home/person/dev/job-hunt/db.py"}}]}},
    {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-08-26T10:01:00Z",
     "cwd": "/home/person/dev/job-hunt", "gitBranch": "memory-encoder",
     "message": {"content": [
         {"type": "text", "text": "Готово, правил db.py. Документация "
                                  "https://example.org/db тут."}]}},
]


def hook_targets():
    """Модули, которые зовут хуки. Точки входа теперь зовутся через -m.

    Раскрывает bash, а не мы: хук сам вычисляет корень от своего файла и сам
    кладёт его в PYTHONPATH, и проверять надо то, что получится у него.
    """
    found = []
    for script in sorted((HERE / "hooks").glob("*.sh")):
        if script.name == "common.sh":
            continue
        body = script.read_text(encoding="utf-8")
        for module in re.findall(r"python3 -m ([\w.]+)", body):
            found.append((script.name, module))
        for match in re.finditer(r'python3\s+"([^"]+)"', body):
            target = match.group(1)
            done = subprocess.run(
                ["bash", "-c", 'source "%s"; printf "%%s" "%s"'
                 % (HERE / "hooks" / "common.sh", target)],
                capture_output=True, text=True)
            found.append((script.name, done.stdout.strip()))
    return found


class TestHooksReachTheirModules(unittest.TestCase):
    """Хук, который зовёт несуществующий файл, не пишет ничего и молчит."""

    def test_every_hook_calls_a_module_that_exists(self):
        targets = hook_targets()
        self.assertTrue(targets, "в хуках не нашлось ни одного вызова модуля")
        broken = []
        for name, target in targets:
            if "/" in target or target.endswith(".py"):
                where = Path(os.path.expandvars(target.replace("$HOME", str(Path.home()))))
            else:
                where = HERE.joinpath(*target.split(".")).with_suffix(".py")
            if not where.exists():
                broken.append((name, target))
        self.assertEqual(broken, [], "хук зовёт несуществующий модуль")

    def test_hooks_can_actually_import_what_they_call(self):
        """Мало чтобы файл лежал: -m должен найти его из каталога хука."""
        for name, target in hook_targets():
            if "/" in target or target.endswith(".py"):
                continue
            done = subprocess.run(
                ["bash", "-c", 'source "%s"; cd /; python3 -c "import %s"'
                 % (HERE / "hooks" / "common.sh", target)],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0,
                             "%s: %s" % (name, done.stderr[-300:]))


class TestHooksDoNotDependOnAbsentTools(unittest.TestCase):
    """Хук, зовущий отсутствующую команду, молчит и выходит с нулём.

    Ровно это уже случалось с чужим путём к корню, и лечилось common.sh.
    Второй раз то же самое устроил timeout(1): на macOS его нет, конвейер
    падает со 127, stderr уходит в /dev/null, хук выходит нулём. Читающая
    половина не делала ничего и не жаловалась.
    """

    TOOLS = ("timeout", "gtimeout", "flock", "nohup", "python3", "bash")

    def test_no_hook_depends_on_a_command_this_machine_lacks(self):
        import shutil
        needed = set()
        for script in sorted((HERE / "hooks").glob("*.sh")):
            body = script.read_text(encoding="utf-8")
            for tool in self.TOOLS:
                if re.search(r"(?<![\w/-])%s\s" % tool, body):
                    needed.add(tool)
        missing = sorted(t for t in needed if shutil.which(t) is None)
        self.assertEqual(missing, [], "хуки зовут то, чего на машине нет")

    def test_the_read_hook_actually_speaks(self):
        """Сквозь: факт в локальной базе, запрос в хук, подсказка на выходе."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            env = dict(os.environ, XMEM_BACKEND="local", XMEM_LOCAL_PATH=str(base),
                       PYTHONPATH=str(HERE), XMEM_LIVE="1")
            seed = ("import os,sys; sys.path.insert(0,%r);\n"
                    "from storage import local\n"
                    "from domain import models\n"
                    "local.close()\n"
                    "local.write_objects([models.Fact(fact_type='project_state',"
                    "subject='demo', scope='project',"
                    "content='В проекте demo правился файл alpha.py').mutation()])\n"
                    "local.close()\n") % str(HERE)
            done = subprocess.run([sys.executable, "-c", seed], env=env,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr[-400:])

            payload = json.dumps({"prompt": "какие файлы правились в demo",
                                  "session_id": "hook-1"}, ensure_ascii=False)
            got = subprocess.run(["bash", str(HERE / "hooks" / "on_prompt_read.sh")],
                                 input=payload, env=env, capture_output=True, text=True)
            self.assertEqual(got.returncode, 0)
            self.assertIn("alpha.py", got.stdout,
                          "читающий хук ничего не сказал: %r" % got.stdout[:200])


class TestQueueIsConsumed(unittest.TestCase):
    """Очередь, которую никто не читает, это потерянная запись, а не запись."""

    def test_something_reads_the_queue_the_hook_writes(self):
        producer = HERE / "hooks" / "on_prompt.py"
        readers = []
        for path in sorted(HERE.rglob("*.py")) + sorted((HERE / "hooks").glob("*")):
            if path == producer or path.name.startswith("test_"):
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "queue.jsonl" in body or "QUEUE" in body:
                readers.append(path.name)
        self.assertTrue(readers, "очередь пишется, но её никто не читает")

    def test_queue_address_follows_the_environment(self):
        """Адрес очереди задаётся переменной, иначе его не подменить в проверке."""
        import importlib
        with mock.patch.dict(os.environ, {"XMEM_QUEUE_PATH": "/tmp/своя-очередь.jsonl"}):
            again = importlib.reload(drain)
            try:
                self.assertEqual(str(again.QUEUE), "/tmp/своя-очередь.jsonl")
            finally:
                importlib.reload(again)

    def test_producer_and_consumer_agree_on_the_address(self):
        """Два адреса очереди в двух файлах разошлись бы молча.

        Проверять надо сам адрес, а не строку импорта в исходнике: ревью
        нашло, что прежняя проверка проходила и на коде, где адреса
        расходятся.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hook_on_prompt", HERE / "hooks" / "on_prompt.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(Path(module.QUEUE), Path(drain.QUEUE))

    def test_fallback_address_follows_the_same_variable(self):
        """Запасной адрес слушает ту же переменную, иначе разойдётся при сбое."""
        body = (HERE / "hooks" / "on_prompt.py").read_text(encoding="utf-8")
        after = body.split("except Exception:", 1)[1]
        self.assertIn("XMEM_QUEUE_PATH", after)

    def test_the_stop_hook_runs_the_queue_consumer(self):
        """Потребитель, которого никто не зовёт, чинит очередь только в тестах."""
        body = (HERE / "hooks" / "on_stop.sh").read_text(encoding="utf-8")
        self.assertIn("pipeline.drain", body)


class TestWritePathReachesTheStore(unittest.TestCase):
    """Сквозная проверка: разговор на входе, записи схемы в хранилище."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        archive = root / "projects" / "job-hunt"
        archive.mkdir(parents=True)
        with (archive / ("%s.jsonl" % SESSION)).open("w", encoding="utf-8") as fh:
            for line in TRANSCRIPT:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.archive = root / "projects"
        self.db = root / "memory.db"
        patch = mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(self.db)})
        patch.start()
        self.addCleanup(patch.stop)
        local.close()
        self.addCleanup(local.close)
        self.addCleanup(setattr, save, "STATE", save.STATE)

    def counts(self):
        repo = db.Repository(self.db)
        try:
            return repo.counts()
        finally:
            repo.close()

    def run_module(self, module, argv):
        # Книжки учёта обоих проходов уводим в свой каталог. Понимание тоже
        # ведёт отметку, и без подмены проверка писала бы в настоящую: своим
        # временным архивом в чужой книжке и разовым зелёным на нём.
        with mock.patch.object(module, "TRANSCRIPTS", self.archive), \
             mock.patch.object(locks, "PASS", Path(self.dir.name) / "save.lock"), \
             mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}), \
             mock.patch.object(save, "STATE", Path(self.dir.name) / "state.json"), \
             mock.patch.object(understand, "STATE",
                               Path(self.dir.name) / "understand.json"), \
             mock.patch.object(sys, "argv", argv):
            module.main()

    def test_understanding_writes_episodes_and_facts(self):
        """Модуль понимания обязан положить в хранилище узлы схемы."""
        self.run_module(understand, ["understand.py", "--send"])
        got = self.counts()
        self.assertGreater(got["Episode"], 0, "ни одного эпизода не легло")
        self.assertGreater(got["Fact"], 0, "ни одного факта не легло")

    def test_saving_writes_events_not_a_text_blob(self):
        """Модуль сохранения обязан класть Event, а не текст без разбора."""
        self.run_module(save, ["save.py", "--send"])
        got = self.counts()
        self.assertGreater(got["Event"], 0, "ни одного события не легло")

    def test_queue_consumer_brings_the_conversation_to_the_store(self):
        """Сквозь очередь: хук положил, потребитель забрал, записи легли."""
        queue = Path(self.dir.name) / "queue.jsonl"
        transcript = next(self.archive.rglob("*.jsonl"))
        with queue.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "user_message", "session_id": SESSION,
                                 "transcript_path": str(transcript)},
                                ensure_ascii=False) + "\n")
        with mock.patch.object(save, "TRANSCRIPTS", self.archive), \
             mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}), \
             mock.patch.object(save, "STATE", Path(self.dir.name) / "state.json"):
            got = drain.drain(queue, dry=False)
        self.assertEqual(got["transcripts"], 1)
        self.assertGreater(self.counts()["Event"], 0, "очередь не довела запись")
        self.assertFalse(drain.taken_path(queue).exists(),
                         "разобранное осталось лежать и разберётся повторно")

    def test_queue_written_during_the_run_is_not_lost(self):
        """Хук пишет в очередь, пока идёт запись. Подмена файла это сохраняет."""
        queue = Path(self.dir.name) / "queue.jsonl"
        transcript = next(self.archive.rglob("*.jsonl"))
        queue.write_text(json.dumps({"transcript_path": str(transcript)}) + "\n",
                         encoding="utf-8")

        def append_meanwhile(*args, **kwargs):
            with queue.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"transcript_path": "/пришло/позже"},
                                    ensure_ascii=False) + "\n")
            return {"sent": 0, "unfinished": []}

        with mock.patch.object(save, "TRANSCRIPTS", self.archive), \
             mock.patch.object(save, "STATE", Path(self.dir.name) / "state.json"), \
             mock.patch.object(save, "ingest", side_effect=append_meanwhile):
            drain.drain(queue, dry=False)
        self.assertIn("пришло/позже", queue.read_text(encoding="utf-8"))

    def test_queue_survives_a_failed_write(self):
        """Запись не удалась — очередь остаётся, её заберёт следующий заход."""
        queue = Path(self.dir.name) / "queue.jsonl"
        transcript = next(self.archive.rglob("*.jsonl"))
        line = json.dumps({"transcript_path": str(transcript)}, ensure_ascii=False)
        queue.write_text(line + "\n", encoding="utf-8")
        with mock.patch.object(save, "TRANSCRIPTS", self.archive), \
             mock.patch.object(save, "STATE", Path(self.dir.name) / "state.json"), \
             mock.patch.object(save, "deliver", side_effect=RuntimeError("сеть легла")):
            with self.assertRaises(RuntimeError):
                drain.drain(queue, dry=False)
        self.assertIn("transcript_path",
                      drain.taken_path(queue).read_text(encoding="utf-8"))

    def test_sequence_number_is_unique_within_a_conversation(self):
        """Разговор часто разложен по нескольким файлам архива.

        Пока номер события считался по файлу, второй файл начинал с нуля и
        затирал события первого по ключу (session_id, sequence_number).
        """
        second = self.archive / "job-hunt" / ("%s-part2.jsonl" % SESSION)
        with second.open("w", encoding="utf-8") as fh:
            for line in TRANSCRIPT:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.run_module(save, ["save.py", "--send"])
        repo = db.Repository(self.db)
        try:
            rows = repo.conn.execute(
                "SELECT count(*), count(DISTINCT sequence_number) FROM event "
                "WHERE session_id = ?", (SESSION,)).fetchone()
        finally:
            repo.close()
        self.assertEqual(rows[0], rows[1], "номера событий столкнулись")
        self.assertGreater(rows[0], len(TRANSCRIPT), "второй файл не записался")

    def test_saved_conversation_can_be_found(self):
        """Запись, которую не находит ни один запрос, это не память.

        Поиск знал только про Fact и Episode, а сохранение пишет Session и
        Event: разговор ложился в базу и не находился никогда.
        """
        self.run_module(save, ["save.py", "--send"])
        repo = db.Repository(self.db)
        try:
            found = repo.search("Какие файлы правились в проекте job-hunt?")
        finally:
            repo.close()
        self.assertTrue(found, "сохранённый разговор не находится")
        self.assertIn("Event", {row["object_type"] for row in found})

    def test_dry_run_does_not_eat_the_archive(self):
        """Холостой прогон не имеет права метить архив прочитанным.

        Пометив, он делает следующую настоящую запись пустой: файлы уже
        «прочитаны», и разница выглядит как пустой архив, а не как потеря.
        """
        self.run_module(save, ["save.py"])           # без --send
        self.run_module(save, ["save.py", "--send"])
        self.assertGreater(self.counts()["Event"], 0,
                           "холостой прогон съел архив")

    def test_session_row_appears(self):
        """Разговор целиком тоже запись схемы, и её не пишет никто."""
        self.run_module(save, ["save.py", "--send"])
        self.assertGreater(self.counts()["Session"], 0, "разговор не записан")


if __name__ == "__main__":
    unittest.main()


class TestLimitDoesNotDuplicate(unittest.TestCase):
    """Потолок --limit не должен ни терять записи, ни писать их дважды.

    hooks/on_stop.sh зовёт `python3 -m pipeline.drain --send --limit 200` на
    каждом ходе агента. Пока отметка о прочитанном двигалась только при
    полностью разобранном файле, а счётчики разговора — при каждой пачке,
    длинный транскрипт перечитывался с начала каждый раз и ложился заново под
    новыми ключами (session_id, sequence_number).
    """

    RECORDS = 5

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.archive = root / "projects" / "demo"
        self.archive.mkdir(parents=True)
        self.transcript = self.archive / "long.jsonl"
        with self.transcript.open("w", encoding="utf-8") as fh:
            for n in range(self.RECORDS):
                fh.write(json.dumps({
                    "type": "user", "sessionId": "long-1",
                    "timestamp": "2026-08-20T10:0%d:00Z" % n,
                    "cwd": "/home/p/dev/demo", "gitBranch": "main",
                    "message": {"content": "сообщение номер %d" % n},
                }, ensure_ascii=False) + "\n")
        self.db = root / "memory.db"
        patch = mock.patch.dict(os.environ,
                                {"XMEM_LOCAL_PATH": str(self.db), "XMEM_BACKEND": "local"})
        patch.start()
        self.addCleanup(patch.stop)
        local.close()
        self.addCleanup(local.close)
        self.state = mock.patch.object(save, "STATE", root / "state.json")
        self.state.start()
        self.addCleanup(self.state.stop)

    def rows(self):
        repo = db.Repository(self.db)
        try:
            return repo.conn.execute(
                "SELECT session_id, sequence_number, content FROM Event "
                "ORDER BY sequence_number").fetchall()
        finally:
            repo.close()

    def test_repeated_limited_runs_store_each_record_once(self):
        for _ in range(4):
            save.ingest([self.transcript], limit=2, dry=False)
        rows = self.rows()
        self.assertEqual(len(rows), self.RECORDS,
                         "записей в хранилище не столько, сколько в транскрипте")
        keys = [(r[0], r[1]) for r in rows]
        self.assertEqual(len(set(keys)), len(keys), "ключи повторяются")
        self.assertEqual(sorted(r[2] for r in rows),
                         sorted("сообщение номер %d" % n for n in range(self.RECORDS)))

    def test_cursor_moves_with_what_was_delivered(self):
        save.ingest([self.transcript], limit=2, dry=False)
        cursor = save.load_state()["files"][str(self.transcript)]
        self.assertGreater(cursor.get("offset", 0), 0,
                           "отметка не сдвинулась, хотя записи ушли")
        self.assertLess(cursor["offset"], self.transcript.stat().st_size,
                        "отметка ушла дальше доставленного")

    def test_a_finished_file_is_not_read_again(self):
        save.ingest([self.transcript], dry=False)
        before = self.rows()
        save.ingest([self.transcript], dry=False)
        self.assertEqual(self.rows(), before, "дочитанный файл разобрался повторно")


class TestSessionStartDoesNotDrift(unittest.TestCase):
    """Начало разговора не должно уезжать вперёд с каждым куском.

    Разговор часто разложен по нескольким файлам архива, и Session собирается
    из того куска, что попался в текущую пачку. Пока ON CONFLICT затирал
    started_at, начало сессии двигалось на время последнего дописанного
    события — и любой запрос по времени над Session отвечал неправдой.

    Ходим через ту же дверь, что и боевой код: upsert сам не коммитит, коммит
    стоит на границе пачки, и голый вызов ничего бы не доказал.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "memory.db"
        patch = mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(self.db)})
        patch.start()
        self.addCleanup(patch.stop)
        local.close()
        self.addCleanup(local.close)

    def put(self, **fields):
        local.write_objects([models.Session(session_id="s", **fields).mutation()])

    def stored(self):
        repo = db.Repository(self.db)
        try:
            return repo.conn.execute(
                "SELECT started_at, ended_at FROM session").fetchall()[0]
        finally:
            repo.close()

    def test_later_chunk_does_not_move_the_start_forward(self):
        self.put(started_at="2026-08-01T10:00:00Z", project="demo")
        self.put(started_at="2026-08-02T12:00:00Z", project="demo")
        self.assertEqual(self.stored()[0], "2026-08-01T10:00:00Z",
                         "начало разговора уехало вперёд")

    def test_earlier_chunk_arriving_late_pulls_the_start_back(self):
        """Файлы архива не обязаны приходить по порядку."""
        self.put(started_at="2026-08-02T12:00:00Z")
        self.put(started_at="2026-08-01T10:00:00Z")
        self.assertEqual(self.stored()[0], "2026-08-01T10:00:00Z")

    def test_the_end_still_moves_forward(self):
        """Конец разговора обязан двигаться: он и означает «последнее, что было»."""
        self.put(ended_at="2026-08-01T10:00:00Z")
        self.put(ended_at="2026-08-02T12:00:00Z")
        self.assertEqual(self.stored()[1], "2026-08-02T12:00:00Z")

    def test_other_fields_still_take_the_newest_value(self):
        """Правило касается только начала, а не всей записи."""
        self.put(started_at="2026-08-01T10:00:00Z", git_branch="old")
        self.put(started_at="2026-08-02T12:00:00Z", git_branch="new")
        repo = db.Repository(self.db)
        try:
            got = repo.conn.execute("SELECT git_branch FROM session").fetchall()[0][0]
        finally:
            repo.close()
        self.assertEqual(got, "new")


class TestSecondRunBowsOut(unittest.TestCase):
    """Второй заход при занятом замке уходит, а не встаёт в очередь.

    Прежний хук проверял замок сам: `flock -n`, занято — не запускаться. Новый
    полагается на alone(), а тот берёт блокирующий LOCK_EX. Если запись
    пережила ход (у сетевого пути срок 180 с), каждый следующий конец хода
    порождал ещё один фоновый python, и все они ждали очереди, чтобы потом по
    очереди разобрать один и тот же транскрипт.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        lock = mock.patch.object(locks, "PASS", self.root / "save.lock")
        lock.start()
        self.addCleanup(lock.stop)
        self.queue = self.root / "queue.jsonl"
        self.queue.write_text(json.dumps({"transcript_path": "/нет/такого"}) + "\n",
                              encoding="utf-8")

    @contextlib.contextmanager
    def within(self, seconds=3):
        """Срок на сам замер: зависание должно падать, а не висеть.

        Без него проверка блокирующего замка вешает всю батарею, и красный
        тест перестаёт быть красным — он просто не заканчивается.
        """
        def ring(signum, frame):
            raise AssertionError("заход завис на занятом замке дольше %s с" % seconds)
        was = signal.signal(signal.SIGALRM, ring)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, was)

    def test_busy_lock_returns_instead_of_waiting(self):
        import fcntl, time
        drain.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with locks.PASS.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.time()
            with self.within(3):
                got = drain.drain(self.queue, dry=False)
            waited = time.time() - started
        self.assertLess(waited, 2.0, "второй заход ждал вместо того, чтобы уйти")
        self.assertTrue(got.get("busy"), "заход не сказал, что замок занят")
        self.assertEqual(got["written"], 0)

    def test_busy_run_does_not_touch_the_queue(self):
        import fcntl
        drain.STATE_DIR.mkdir(parents=True, exist_ok=True)
        before = self.queue.read_text(encoding="utf-8")
        with locks.PASS.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.within(3):
                drain.drain(self.queue, dry=False)
        self.assertEqual(self.queue.read_text(encoding="utf-8"), before)
        self.assertFalse(drain.taken_path(self.queue).exists(),
                         "занятый заход всё же подменил очередь")

    def test_free_lock_still_works(self):
        got = drain.drain(self.queue, dry=False)
        self.assertFalse(got.get("busy"))


class TestBrokenBytesDoNotMoveTheCursor(unittest.TestCase):
    """Негодный байт в транскрипте не должен сдвигать отметку.

    При errors="replace" такой байт становится U+FFFD, а тот кодируется
    обратно тремя байтами. Пока смещение считалось перекодировкой строки,
    отметка уезжала относительно настоящего места, и следующий заход садился
    в середину строки — теряя или задваивая записи молча.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.transcript = self.root / "bad.jsonl"
        good = [json.dumps({
            "type": "user", "sessionId": "b-1",
            "timestamp": "2026-08-20T10:0%d:00Z" % n,
            "cwd": "/home/p/dev/demo", "gitBranch": "main",
            "message": {"content": "строка %d" % n}}, ensure_ascii=False).encode("utf-8")
            for n in range(3)]
        # Настоящий негодный байт внутри значения второй строки — именно
        # сырой 0xFF, а не символ U+00FF: тот в UTF-8 вполне годен и ничего
        # бы не доказал. JSON остаётся разбираемым, а перекодировка строки
        # перестаёт совпадать с её длиной в файле.
        good[1] = good[1].replace("строка 1".encode("utf-8"),
                                  b"\xff" + "строка 1".encode("utf-8"))
        self.transcript.write_bytes(b"\n".join(good) + b"\n")

    def test_cursor_lands_exactly_at_the_end_of_the_file(self):
        from archive.transcripts import read_new
        items, cursor = read_new(self.transcript, {})
        self.assertEqual(cursor["offset"], self.transcript.stat().st_size,
                         "отметка не совпала с размером файла")
        self.assertEqual(len(items), 3)

    def test_second_pass_finds_nothing_new(self):
        from archive.transcripts import read_new
        _, cursor = read_new(self.transcript, {})
        again, _ = read_new(self.transcript, cursor)
        self.assertEqual(again, [], "дочитанный файл отдал записи повторно")

    def test_every_item_offset_is_a_real_line_boundary(self):
        from archive.transcripts import read_new
        items, _ = read_new(self.transcript, {})
        data = self.transcript.read_bytes()
        for item in items:
            edge = item["cursor"]["offset"]
            self.assertTrue(edge == len(data) or data[edge - 1:edge] == b"\n",
                            "отметка %d не на границе строки" % edge)


class TestQueueKeepsRotating(unittest.TestCase):
    """Очередь обязана ротироваться даже когда разбор упёрся в потолок.

    Файл подмены не снимался, если сработал --limit, а take() не подменяет
    очередь, пока подмена лежит. Один длинный транскрипт заклинивал очередь
    навсегда: хук писал в неё, читать её больше не приходил никто.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.archive = root / "projects" / "demo"
        self.archive.mkdir(parents=True)
        self.transcript = self.archive / "long.jsonl"
        with self.transcript.open("w", encoding="utf-8") as fh:
            for n in range(6):
                fh.write(json.dumps({
                    "type": "user", "sessionId": "q-1",
                    "timestamp": "2026-08-20T10:0%d:00Z" % n,
                    "cwd": "/home/p/dev/demo", "gitBranch": "main",
                    "message": {"content": "строка %d" % n},
                }, ensure_ascii=False) + "\n")
        self.queue = root / "queue.jsonl"
        self.queue.write_text(json.dumps(
            {"kind": "user_message", "session_id": "q-1",
             "transcript_path": str(self.transcript)}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.db = root / "memory.db"
        patch = mock.patch.dict(os.environ,
                                {"XMEM_LOCAL_PATH": str(self.db), "XMEM_BACKEND": "local"})
        patch.start()
        self.addCleanup(patch.stop)
        local.close()
        self.addCleanup(local.close)
        st = mock.patch.object(save, "STATE", root / "state.json")
        st.start()
        self.addCleanup(st.stop)
        lock = mock.patch.object(locks, "PASS", root / "save.lock")
        lock.start()
        self.addCleanup(lock.stop)

    def test_taken_is_released_even_when_the_limit_hits(self):
        drain.drain(self.queue, limit=2, dry=False)
        self.assertFalse(drain.taken_path(self.queue).exists(),
                         "подмена осталась — очередь больше не ротируется")

    def test_unfinished_transcript_comes_back_to_the_queue(self):
        drain.drain(self.queue, limit=2, dry=False)
        left = drain.read_queue(self.queue)
        self.assertEqual([i.get("transcript_path") for i in left],
                         [str(self.transcript)],
                         "недочитанный транскрипт не вернулся в очередь")

    def test_repeated_drains_finish_the_transcript(self):
        for _ in range(5):
            drain.drain(self.queue, limit=2, dry=False)
        repo = db.Repository(self.db)
        try:
            rows = repo.conn.execute(
                "SELECT session_id, sequence_number FROM Event").fetchall()
        finally:
            repo.close()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(set(rows)), 6, "ключи повторяются")
        self.assertEqual(drain.read_queue(self.queue), [],
                         "очередь не опустела, хотя транскрипт дочитан")
