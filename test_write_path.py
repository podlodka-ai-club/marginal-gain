#!/usr/bin/env python3
"""Проверки пути записи. Запуск: python3 -m unittest test_write_path -v

Замер показал ноль вклада памяти. Разбор упёрся в то, что база наполнена не
продуктом, а ручными пробами: 625 узлов против 15 в архиве. Эти проверки
задают вопрос, на который до сих пор отвечали словами, — доходит ли запись до
хранилища, если её никто не ведёт за руку.

Наполнять базу в обход конвейера нельзя: тогда замер меряет наполнение, а не
продукт. Поэтому сначала красный тест, потом починка модуля записи.
"""
import json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

import drain
import save
import store
import understand
import xmem
import xmem_local

HERE = Path(__file__).resolve().parent

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
    """Пути к модулям, которые зовут хуки, уже с раскрытыми переменными.

    Раскрывает bash, а не мы: хук сам вычисляет корень от своего файла, и
    проверять надо то, что получится у него, а не то, что мы угадали.
    """
    found = []
    for script in sorted((HERE / "hooks").glob("*.sh")):
        if script.name == "common.sh":
            continue
        body = script.read_text(encoding="utf-8")
        for match in re.finditer(r'python3\s+"([^"]+)"', body):
            target = match.group(1)
            if "$" not in target:
                found.append((script.name, target))
                continue
            done = subprocess.run(
                ["bash", "-c", 'source "%s"; printf "%%s" "%s"'
                 % (HERE / "hooks" / "common.sh", target)],
                capture_output=True, text=True)
            found.append((script.name, done.stdout.strip()))
    return found


class TestHooksReachTheirModules(unittest.TestCase):
    """Хук, который зовёт несуществующий файл, не пишет ничего и молчит."""

    def test_every_hook_calls_a_file_that_exists(self):
        targets = hook_targets()
        self.assertTrue(targets, "в хуках не нашлось ни одного вызова модуля")
        broken = [(name, path) for name, path in targets
                  if not Path(os.path.expandvars(path.replace("$HOME", str(Path.home())))).exists()]
        self.assertEqual(broken, [], "хук зовёт несуществующий модуль")


class TestQueueIsConsumed(unittest.TestCase):
    """Очередь, которую никто не читает, это потерянная запись, а не запись."""

    def test_something_reads_the_queue_the_hook_writes(self):
        producer = HERE / "hooks" / "on_prompt.py"
        readers = []
        for path in sorted(HERE.glob("*.py")) + sorted((HERE / "hooks").glob("*")):
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
        self.assertIn("drain.py", body)


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
        xmem_local.close()
        self.addCleanup(xmem_local.close)
        self.addCleanup(setattr, save, "STATE", save.STATE)

    def counts(self):
        repo = store.Repository(self.db)
        try:
            return repo.counts()
        finally:
            repo.close()

    def run_module(self, module, argv):
        with mock.patch.object(module, "TRANSCRIPTS", self.archive), \
             mock.patch.object(xmem, "BACKEND", "local"), \
             mock.patch.object(save, "STATE", Path(self.dir.name) / "state.json"), \
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
             mock.patch.object(xmem, "BACKEND", "local"), \
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
            return 0

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
        repo = store.Repository(self.db)
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
        repo = store.Repository(self.db)
        try:
            found = repo.search("Какие файлы правились в проекте job-hunt?")
        finally:
            repo.close()
        self.assertTrue(found, "сохранённый разговор не находится")
        self.assertIn("Event", {row["object_type"] for row in found})

    def test_session_row_appears(self):
        """Разговор целиком тоже запись схемы, и её не пишет никто."""
        self.run_module(save, ["save.py", "--send"])
        self.assertGreater(self.counts()["Session"], 0, "разговор не записан")


if __name__ == "__main__":
    unittest.main()
