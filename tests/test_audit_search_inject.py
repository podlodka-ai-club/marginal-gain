#!/usr/bin/env python3
"""Аудит поиска и вброса: `pipeline.suggest.attend`.

Запуск: python3 -m unittest tests.test_audit_search_inject -v

«Поиск» — запрос, все кандидаты с их весом, что срезано порогом и каким.
«Вброс» — что именно подано агенту и какой формой. Оба шага пишутся из одной и
той же точки (`attend`/`mute`) — той, где сейчас уже пишется лента, и по той
же причине: заход подсказки решается там, а не восстанавливается снаружи.

Мутации, на которых проверки обязаны краснеть:
  * не писать «поиск» при молчании (below_threshold/not_found/…)   → TestSearchIsAuditedOnSilence
  * не писать «поиск» при удачном вбросе                            → TestSearchIsAuditedOnInjection
  * терять причину среза или кандидатов                             → TestSearchNamesTheCutAndCandidates
  * не писать «вброс» вовсе или не называть форму                   → TestInjectNamesTheTextAndVoice
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import audit, ledger
from pipeline import suggest, understand
from storage import local, port

TALK = "разговор-1"


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


class Answering:
    """Дверь, отвечающая заданным. Та же оснастка, что в test_ledger.py."""

    name = "local"

    def __init__(self, answer=""):
        self.answer = answer

    def read(self, query, mode="single"):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def write(self, text, wait=False):
        return port.door().write(text, wait)

    def write_objects(self, records, relations=(), op="create"):
        return port.door().write_objects(records, relations, op)


def fact_piece(content, score=0.9):
    body = dict(object_type="Fact", fact_type="project_state", subject="демо",
               scope="project", content="%s Оценка уверенности: %.2f" % (content, score))
    return json.dumps([body], ensure_ascii=False)


class TestSearchIsAuditedOnSilence(unittest.TestCase):
    def test_a_below_threshold_silence_leaves_a_failed_search_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering(fact_piece("правился db.py", score=0.01))
            suggest.attend("db.py", session_id=TALK, door=door, record=False)
            got = audit.rows(where=base, step="search")
            self.assertEqual(len(got), 1)
            self.assertFalse(got[0]["ok"])
            self.assertEqual(got[0]["output"]["reason"], "below_threshold")
            self.assertEqual(got[0]["input"]["query"], "db.py")

    def test_a_not_found_silence_still_leaves_a_row_with_empty_candidates(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering("")
            suggest.attend("db.py", session_id=TALK, door=door, record=False)
            got = audit.rows(where=base, step="search")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["output"]["candidates"], [])
            self.assertIn(got[0]["output"]["reason"], ("not_found", "disabled"))

    def test_no_inject_row_is_left_when_memory_stayed_silent(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering("")
            suggest.attend("db.py", session_id=TALK, door=door, record=False)
            self.assertEqual(audit.rows(where=base, step="inject"), [])


class TestSearchIsAuditedOnInjection(unittest.TestCase):
    def test_a_successful_attend_leaves_an_ok_search_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering(fact_piece("правился db.py", score=0.9))
            text, kept, why = suggest.attend("db.py", session_id=TALK, door=door,
                                             record=False)
            self.assertTrue(text)
            self.assertIsNone(why)
            got = audit.rows(where=base, step="search")
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0]["ok"])
            self.assertEqual(got[0]["output"]["kept"], len(kept))


class TestSearchNamesTheCutAndCandidates(unittest.TestCase):
    def test_the_candidate_that_did_not_pass_is_still_named(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering(fact_piece("случайное", score=0.02))
            suggest.attend("db.py", session_id=TALK, door=door, record=False)
            row = audit.rows(where=base, step="search")[0]
            self.assertEqual(len(row["output"]["candidates"]), 1)
            self.assertAlmostEqual(row["output"]["candidates"][0]["score"], 0.02, places=2)


class TestInjectNamesTheText(unittest.TestCase):
    def test_the_injected_text_is_recorded_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = Answering(fact_piece("правился db.py", score=0.9))
            text, kept, _ = suggest.attend("db.py", session_id=TALK, door=door,
                                           record=False)
            row = audit.rows(where=base, step="inject")[-1]
            self.assertEqual(row["output"]["text"], text)

    def test_a_different_voice_shapes_the_recorded_text_differently(self):
        """Форма не называется отдельным полем — она видна по очертанию текста."""
        seen = {}
        for name in ("plain", "directive", "inline"):
            with tempfile.TemporaryDirectory() as tmp, store(tmp) as base, \
                 mock.patch.dict(os.environ, {"XMEM_VOICE": name}):
                door = Answering(fact_piece("правился db.py", score=0.9))
                suggest.attend("db.py", session_id=TALK, door=door, record=False)
                row = audit.rows(where=base, step="inject")[-1]
                seen[name] = row["output"]["text"]
        self.assertEqual(len(set(seen.values())), len(seen),
                         "разные формы дали одинаковый текст в аудите")

    def test_suggest_still_touches_only_the_entry_point_of_voice(self):
        """Инвариант, который эта задача обязана не тронуть: одна точка входа."""
        import ast
        root = Path(__file__).resolve().parent.parent
        tree = ast.parse((root / "pipeline" / "suggest.py").read_text(encoding="utf-8"))
        touched = {node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute)
                  and isinstance(node.value, ast.Name) and node.value.id == "voice"}
        self.assertEqual({"render"}, touched)


if __name__ == "__main__":
    unittest.main()
