#!/usr/bin/env python3
"""Аудит первых двух ступеней: перехват сообщения и слив хода в архив.

Запуск: python3 -m unittest tests.test_audit_intercept_drain -v

«Перехват» — что пришло на UserPromptSubmit и из какой сессии (`hooks/on_prompt.py`).
«Слив хода в архив» — что реально легло структурной записью (`pipeline.save.send`,
зовёт его `pipeline.save.ingest` на настоящей записи).

Мутации, на которых проверки обязаны краснеть:
  * не писать аудит перехвата, когда очередь всё же приняла сообщение → TestIntercept
  * не писать аудит слива на настоящей записи                        → TestDrain
  * писать аудит слива на холостом прогоне                            → TestDryIngestIsSilent
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import audit
from pipeline import save
from storage import db, local

HERE = Path(__file__).resolve().parent.parent

SESSION = "разговор-перехват-1"
TRANSCRIPT = [
    {"type": "user", "sessionId": SESSION, "timestamp": "2026-08-26T10:00:00Z",
     "cwd": "/home/person/dev/demo", "gitBranch": "audit-intercept",
     "message": {"content": "Запомни: живу в Казани"}},
    {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-08-26T10:00:30Z",
     "cwd": "/home/person/dev/demo", "gitBranch": "audit-intercept",
     "message": {"content": [{"type": "text", "text": "Готово."}]}},
]


def load_on_prompt():
    spec = importlib.util.spec_from_file_location(
        "hook_on_prompt_audit", HERE / "hooks" / "on_prompt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIntercept(unittest.TestCase):
    def test_the_incoming_message_and_session_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            with mock.patch.dict(os.environ, {
                    "XMEM_LOCAL_PATH": str(base),
                    "XMEM_QUEUE_PATH": str(Path(tmp) / "queue.jsonl"),
                    "XMEM_STATE_DIR": str(Path(tmp) / "state")}):
                module = load_on_prompt()
                payload = {"session_id": "разговор-A", "prompt": "живу в Казани",
                          "cwd": "/home/person/dev/demo", "permission_mode": "default"}
                with mock.patch("sys.stdin.read"), \
                     mock.patch("json.load", return_value=payload):
                    module.main()
            got = audit.rows(where=base, step="intercept")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["session_id"], "разговор-A")
            self.assertEqual(got[0]["output"]["content"], "живу в Казани")

    def test_an_empty_stdin_writes_nothing(self):
        """Хук читает пустоту — main() выходит рано, и писать аудиту нечего."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            with mock.patch.dict(os.environ, {
                    "XMEM_LOCAL_PATH": str(base),
                    "XMEM_QUEUE_PATH": str(Path(tmp) / "queue.jsonl"),
                    "XMEM_STATE_DIR": str(Path(tmp) / "state")}):
                module = load_on_prompt()
                with mock.patch("json.load", side_effect=ValueError("bad json")):
                    module.main()
            self.assertEqual(audit.rows(where=base, step="intercept"), [])


class TestDrain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "memory.db"
        local.close()
        audit.reset(self.db)
        self._env = mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(self.db),
                                                  "XMEM_BACKEND": "local"})
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        local.close()
        audit.reset(self.db)
        self._tmp.cleanup()

    def transcript(self):
        path = self.tmp / ("%s.jsonl" % SESSION)
        with path.open("w", encoding="utf-8") as fh:
            for line in TRANSCRIPT:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def test_a_real_write_leaves_one_drain_row_per_session(self):
        path = self.transcript()
        got = save.ingest([path], dry=False)
        self.assertEqual(got["sent"], 2)
        rows_ = audit.rows(where=self.db, step="drain")
        self.assertEqual(len(rows_), 1)
        self.assertEqual(rows_[0]["session_id"], SESSION)
        self.assertEqual(len(rows_[0]["output"]["events"]), 2)
        self.assertTrue(rows_[0]["output"]["events"][0]["role"])

    def test_what_was_written_is_named_verbatim(self):
        path = self.transcript()
        save.ingest([path], dry=False)
        row = audit.rows(where=self.db, step="drain")[0]
        texts = [e["text"] for e in row["output"]["events"]]
        self.assertTrue(any("Казани" in t for t in texts))


class TestDryIngestIsSilent(unittest.TestCase):
    def test_a_dry_ingest_writes_no_audit_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memory.db"
            audit.reset(base)
            with mock.patch.dict(os.environ, {"XMEM_LOCAL_PATH": str(base)}):
                path = Path(tmp) / ("%s.jsonl" % SESSION)
                with path.open("w", encoding="utf-8") as fh:
                    for line in TRANSCRIPT:
                        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                save.ingest([path], dry=True)
            self.assertEqual(audit.rows(where=base, step="drain"), [])


if __name__ == "__main__":
    unittest.main()
