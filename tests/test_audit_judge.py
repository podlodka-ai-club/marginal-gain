#!/usr/bin/env python3
"""Аудит оценки: применена подсказка или нет и по какому признаку решено.

Запуск: python3 -m unittest tests.test_audit_judge -v

Ответ про пользу подсказки снимается двумя способами — `pipeline.suggest.settle`
(догадка по архиву, способ `transcript`) и `pipeline.suggest.harvest` (ответ
агента вместе с вбросом, способ `inline`), см. ADR 0012. Оба зовут
`domain.ledger.helped` и оба обязаны оставить строку аудита: признак и есть
способ съёма, он объясняет, «по какому признаку решено».

Мутации, на которых проверки обязаны краснеть:
  * не писать аудит в одном из двух способов съёма       → TestSettleIsAudited, TestHarvestIsAudited
  * терять признак (source) в строке аудита               → TestTheSourceIsTheCriterion
  * путать «да» и «нет/неизвестно» одним полем ok          → TestOkMatchesTheVerdict
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, marks
from pipeline import suggest, understand
from storage import audit, local, port

CWD = "/home/person/dev/demo"
BRANCH = "audit-judge"


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    audit.reset(base)
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"), \
         mock.patch.object(ledger, "LOG", Path(tmp) / "ledger.jsonl"):
        try:
            yield base
        finally:
            local.close()
            audit.reset(base)


def rows(session, request, reply):
    stamp = "2026-08-28T09:00:00Z"
    head = {"sessionId": session, "timestamp": stamp, "cwd": CWD, "gitBranch": BRANCH}
    return [dict(head, type="user", message={"content": request}),
           dict(head, type="assistant",
                message={"content": [{"type": "text", "text": reply}]})]


def archive(root, session, request, reply, name="разговор.jsonl"):
    path = Path(root) / name
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(session, request, reply):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


class TestSettleIsAudited(unittest.TestCase):
    def test_a_completed_turn_after_the_injection_leaves_a_yes_judge_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            talk, at = "разговор-1", "2026-08-28T08:59:00Z"
            suggest.remember(suggest.injection_of(talk, "подсказка", at=at),
                             log=suggest.LOG)
            files = archive(tmp, talk, "Отвечай кратко, длинные ответы не читаю",
                            "Готово.")
            suggest.settle(files, door=port.door(), log=suggest.LOG)
            got = audit.rows(where=base, step="judge")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["input"]["source"], "transcript")
            self.assertEqual(got[0]["output"]["verdict"], "yes")
            self.assertTrue(got[0]["ok"])

    def test_settling_with_nothing_to_settle_writes_no_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            suggest.settle([], door=port.door(), log=suggest.LOG)
            self.assertEqual(audit.rows(where=base, step="judge"), [])


class TestHarvestIsAudited(unittest.TestCase):
    def test_an_inline_answer_leaves_a_judge_row_with_its_own_source(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            talk, at = "разговор-1", "2026-08-28T08:59:00Z"
            suggest.remember(suggest.injection_of(talk, "подсказка", at=at),
                             log=suggest.LOG)
            key = ledger.key_of(talk, at)
            reply = "Готово. %s\n%s\n%s" % (
                marks.XMD1_BEGIN,
                json.dumps({"injection": key, "used": "yes"}, ensure_ascii=False),
                marks.XMD1_END)
            files = archive(tmp, talk, "Запомни, пожалуйста", reply)
            suggest.harvest(files, log=suggest.LOG)
            got = audit.rows(where=base, step="judge")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["input"]["source"], "inline")
            self.assertEqual(got[0]["output"]["verdict"], "yes")

    def test_an_answer_by_an_unknown_key_writes_no_judge_row(self):
        """Чужой ключ не наш заход — писать про него аудиту нечего."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            reply = "Готово. %s\n%s\n%s" % (
                marks.XMD1_BEGIN,
                json.dumps({"injection": "чужой|ключ", "used": "yes"}, ensure_ascii=False),
                marks.XMD1_END)
            files = archive(tmp, "разговор-1", "Запомни, пожалуйста", reply)
            suggest.harvest(files, log=suggest.LOG)
            self.assertEqual(audit.rows(where=base, step="judge"), [])


class TestTheSourceIsTheCriterion(unittest.TestCase):
    def test_settle_and_harvest_are_told_apart_by_source(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            talk, at = "разговор-1", "2026-08-28T08:59:00Z"
            suggest.remember(suggest.injection_of(talk, "подсказка", at=at),
                             log=suggest.LOG)
            files = archive(tmp, talk, "Отвечай кратко, длинные ответы не читаю",
                            "Готово.")
            suggest.settle(files, door=port.door(), log=suggest.LOG)
            row = audit.rows(where=base, step="judge")[0]
            self.assertEqual(row["session_id"], talk)
            self.assertEqual(row["input"]["injection"], ledger.key_of(talk, at))


class TestOkMatchesTheVerdict(unittest.TestCase):
    def test_a_blocked_turn_is_a_no_and_is_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            talk, at = "разговор-1", "2026-08-28T08:59:00Z"
            suggest.remember(suggest.injection_of(talk, "подсказка", at=at),
                             log=suggest.LOG)
            path = Path(tmp) / "разговор.jsonl"
            head = {"sessionId": talk, "timestamp": "2026-08-28T09:00:00Z",
                   "cwd": CWD, "gitBranch": BRANCH}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(
                    head, type="user",
                    message={"content": "Посмотри, что там с базой"}),
                    ensure_ascii=False) + "\n")
                fh.write(json.dumps(dict(
                    head, type="assistant",
                    message={"content": [
                        {"type": "tool_result", "is_error": True,
                         "content": "FileNotFoundError"},
                        {"type": "text", "text": "Не нашёл файл."}]}),
                    ensure_ascii=False) + "\n")
            suggest.settle([path], door=port.door(), log=suggest.LOG)
            row = audit.rows(where=base, step="judge")[0]
            self.assertEqual(row["output"]["verdict"], "no")
            self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
