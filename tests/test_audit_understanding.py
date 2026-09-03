#!/usr/bin/env python3
"""Аудит понимания: ответ агента дословно, разметка, каждый факт-кандидат.

Запуск: python3 -m unittest tests.test_audit_understanding -v

Ступень «факт в БД» отчитывалась одним словом. Здесь на каждый разобранный
эпизод ложится по строке аудита на каждый из трёх разных вопросов: что модель
ответила дословно («ответ агента»), что маппер оставил и отбросил и почему
(«разметка»), что случилось с каждым отдельным фактом-кандидатом — записан или
отклонён порогом веса («факты»).

Мутации, на которых проверки обязаны краснеть:
  * не писать аудит на холостом прогоне                          → TestDryRunIsSilent
  * терять причину, по которой маппер отбросил единицу            → TestMarkNamesTheDropReason
  * не различать записанный факт и отклонённый порогом            → TestFactRowsMatchTheOutcome
  * не нести дословный ответ агента в строке аудита                → TestReplyIsVerbatim
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from storage import audit
from domain import marks
from pipeline import suggest, understand
from storage import local, port

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])

CWD = "/home/person/dev/demo"
BRANCH = "audit-understanding"


def mark_block(units):
    lines = [marks.XMD1_BEGIN] + [json.dumps(u, ensure_ascii=False) for u in units] \
        + [marks.XMD1_END]
    return "\n".join(lines)


def unit(subject, value, source="stated", confidence=0.9, kind="preference"):
    return {"type": kind, "subject": subject, "predicate": "любит", "value": value,
           "source": source, "confidence": confidence}


def rows(session, reply_text):
    stamp = "2026-08-28T09:00:00Z"
    head = {"sessionId": session, "timestamp": stamp, "cwd": CWD, "gitBranch": BRANCH}
    out = [dict(head, type="user", message={"content": "Запомни, пожалуйста"})]
    out.append(dict(head, type="assistant",
                    message={"content": [{"type": "text", "text": reply_text}]}))
    return out


def archive(root, session, reply_text, name="разговор.jsonl"):
    path = Path(root) / name
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(session, reply_text):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "memory.db"
        local.close()
        audit.reset(self.base)
        self._env = mock.patch.dict(os.environ, {
            "XMEM_BACKEND": "local", "XMEM_DISABLED": "",
            "XMEM_LOCAL_PATH": str(self.base),
            "XMEM_STATE_DIR": str(self.tmp / "state")})
        self._env.__enter__()
        self._state = mock.patch.object(understand, "STATE", self.tmp / "understand.json")
        self._state.__enter__()
        self._sug = mock.patch.object(suggest, "LOG", self.tmp / "suggest-log.jsonl")
        self._sug.__enter__()

    def tearDown(self):
        self._sug.__exit__(None, None, None)
        self._state.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        local.close()
        audit.reset(self.base)
        self._tmp.cleanup()

    def digest(self, files):
        return understand.digest(files, archive=files, door=port.door(), dry=False,
                                 min_score=0.0)


class TestReplyIsVerbatim(Base):
    def test_the_raw_reply_text_is_recorded_as_is(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань")])
        files = archive(self.tmp, "разговор-1", text)
        self.digest(files)
        rows_ = audit.rows(where=self.base, step="reply")
        self.assertEqual(len(rows_), 1)
        self.assertEqual(rows_[0]["output"]["replies"], [text])
        self.assertEqual(rows_[0]["session_id"], "разговор-1")


class TestMarkNamesTheDropReason(Base):
    def test_a_unit_below_confidence_is_dropped_with_a_named_reason(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань", confidence=0.1)])
        files = archive(self.tmp, "разговор-1", text)
        self.digest(files)
        row = audit.rows(where=self.base, step="mark")[-1]
        self.assertEqual(row["output"]["kept"], 0)
        self.assertEqual(row["output"]["dropped"], {"confidence": 1})
        self.assertFalse(row["ok"])

    def test_a_kept_unit_is_marked_ok(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань")])
        files = archive(self.tmp, "разговор-1", text)
        self.digest(files)
        row = audit.rows(where=self.base, step="mark")[-1]
        self.assertEqual(row["output"]["kept"], 1)
        self.assertTrue(row["ok"])

    @SLOW
    @given(confidences=st.lists(st.floats(min_value=0.0, max_value=1.0, width=32),
                                min_size=1, max_size=4))
    def test_the_dropped_count_matches_the_confidence_filter(self, confidences):
        units = [unit("тема-%d" % i, "значение", confidence=c)
                 for i, c in enumerate(confidences)]
        text = "Готово. %s" % mark_block(units)
        files = archive(self.tmp, "разговор-1", text)
        self.digest(files)
        row = audit.rows(where=self.base, step="mark")[-1]
        expect_dropped = sum(1 for c in confidences if c < marks.XMD1_MIN_CONFIDENCE)
        expect_kept = len(confidences) - expect_dropped
        self.assertEqual(row["output"]["kept"], expect_kept)
        self.assertEqual(sum(row["output"]["dropped"].values()), expect_dropped)


class TestFactRowsMatchTheOutcome(Base):
    def test_a_fact_that_passes_the_threshold_is_written_and_marked_ok(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань")])
        files = archive(self.tmp, "разговор-1", text)
        self.digest(files)
        rows_ = audit.rows(where=self.base, step="fact")
        self.assertEqual(len(rows_), 1)
        self.assertTrue(rows_[0]["ok"])
        self.assertTrue(rows_[0]["output"]["written"])
        # Ключ — subject и predicate вместе (см. domain/marks.py:xmd1_unit):
        # разные атрибуты одной темы не должны делить ключ факта.
        self.assertEqual(rows_[0]["input"]["subject"], "5:город: любит")

    def test_a_fact_below_the_score_threshold_is_refused_with_a_reason(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань")])
        files = archive(self.tmp, "разговор-1", text)
        got = understand.digest(files, archive=files, door=port.door(), dry=False,
                                min_score=99.0)
        self.assertEqual(got["skipped"], 1)
        rows_ = audit.rows(where=self.base, step="fact")
        self.assertEqual(len(rows_), 1)
        self.assertFalse(rows_[0]["ok"])
        self.assertFalse(rows_[0]["output"]["written"])
        self.assertIn("below", rows_[0]["output"]["reason"])


class TestDryRunIsSilent(Base):
    def test_a_dry_pass_writes_no_audit_row(self):
        text = "Готово. %s" % mark_block([unit("город", "Казань")])
        files = archive(self.tmp, "разговор-1", text)
        understand.digest(files, archive=files, door=port.door(), dry=True, min_score=0.0)
        for step in ("reply", "mark", "fact"):
            self.assertEqual(audit.rows(where=self.base, step=step), [],
                             "холостой прогон не должен писать шаг %r" % step)


class TestTemplateFactsAreAuditedToo(Base):
    """Факт без разметки (вырезанный шаблоном) — тот же путь до аудита фактов."""

    def test_a_template_extracted_fact_still_gets_a_fact_row(self):
        files = archive(self.tmp, "разговор-1", "Готово.")
        # Реплика человека, попадающая в тему предпочтений archive.extract.
        rows_txt = [
            {"type": "user", "sessionId": "разговор-1", "timestamp": "2026-08-28T09:00:00Z",
             "cwd": CWD, "gitBranch": BRANCH,
             "message": {"content": "Отвечай кратко, длинные ответы не читаю"}},
            {"type": "assistant", "sessionId": "разговор-1", "timestamp": "2026-08-28T09:00:01Z",
             "cwd": CWD, "gitBranch": BRANCH,
             "message": {"content": [{"type": "text", "text": "Готово."}]}},
        ]
        path = self.tmp / "разговор-2.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for line in rows_txt:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        understand.digest([path], archive=[path], door=port.door(), dry=False, min_score=0.0)
        self.assertTrue(audit.rows(where=self.base, step="fact"),
                        "факт, вырезанный шаблоном, не попал в аудит")


if __name__ == "__main__":
    unittest.main()
