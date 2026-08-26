#!/usr/bin/env python3
"""Проверки пути записи. Запуск: python3 -m unittest test_write_path -v

Замер показал ноль вклада памяти. Разбор упёрся в то, что база наполнена не
продуктом, а ручными пробами: 625 узлов против 15 в архиве. Эти проверки
задают вопрос, на который до сих пор отвечали словами, — доходит ли запись до
хранилища, если её никто не ведёт за руку.

Наполнять базу в обход конвейера нельзя: тогда замер меряет наполнение, а не
продукт. Поэтому сначала красный тест, потом починка модуля записи.
"""
import json, os, re, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

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
    """Пути к модулям, которые хуки зовут. Их и проверяем на существование."""
    found = []
    for script in sorted((HERE / "hooks").glob("*.sh")):
        body = script.read_text(encoding="utf-8")
        for match in re.finditer(r'python3\s+"([^"]+)"', body):
            found.append((script.name, match.group(1)))
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

    def test_session_row_appears(self):
        """Разговор целиком тоже запись схемы, и её не пишет никто."""
        self.run_module(save, ["save.py", "--send"])
        self.assertGreater(self.counts()["Session"], 0, "разговор не записан")


if __name__ == "__main__":
    unittest.main()
