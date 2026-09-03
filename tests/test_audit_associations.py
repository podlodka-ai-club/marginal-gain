#!/usr/bin/env python3
"""Аудит связей: что с чем связано и по какому правилу.

Запуск: python3 -m unittest tests.test_audit_associations -v

Мутации, на которых проверки обязаны краснеть:
  * не писать аудит на холостом прогоне (`dry=True`)      → TestDryBuildIsSilent
  * терять повод (cue), которым связаны концы             → TestLinkNamesTheCue
  * писать аудит, когда карточек не появилось              → TestEmptyBuildWritesNoRow (сдвиг: пустой граф ≠ отказ шага, а его отсутствие)
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from storage import audit
from domain import models
from pipeline import associate, understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "audit-associations"


def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD, "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": spec["request"]}))
        blocks = [{"type": "tool_use", "name": "Edit",
                  "input": {"file_path": "%s/%s" % (CWD, name)}}
                 for name in spec["names"]]
        blocks.append({"type": "text", "text": "Готово."})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape):
    out = []
    for number, (session, specs) in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(session, specs):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.append(path)
    return out


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    audit.reset(base)
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()
            audit.reset(base)


SHAPE_WITH_A_PAIR = [("разговор-1", [
    {"request": "Посмотри, что там с базой", "names": ["db.py", "port.py"]}])]

SHAPE_WITHOUT_A_PAIR = [("разговор-1", [
    {"request": "Посмотри, что там с базой", "names": ["db.py"]}])]


def run(files, **kwargs):
    understand.digest(files, door=port.door(), dry=False)
    kwargs.setdefault("dry", False)
    return associate.build(files, door=port.door(), **kwargs)


class TestLinkNamesTheCue(unittest.TestCase):
    def test_a_same_episode_pair_is_audited_with_its_cue_and_weight(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, SHAPE_WITH_A_PAIR))
            got = audit.rows(where=base, step="link")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["input"]["cue"], "same_episode")
            self.assertEqual(got[0]["output"]["weight"], 1.0)
            self.assertTrue(got[0]["ok"])

    def test_the_two_ends_named_are_the_two_facts_that_were_touched(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, SHAPE_WITH_A_PAIR))
            row = audit.rows(where=base, step="link")[0]
            ends = {row["input"]["source"], row["input"]["target"]}
            self.assertTrue(any("db.py" in e for e in ends))
            self.assertTrue(any("port.py" in e for e in ends))


class TestEmptyBuildWritesNoRow(unittest.TestCase):
    def test_a_single_fact_episode_makes_no_pair_and_no_audit_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, SHAPE_WITHOUT_A_PAIR))
            self.assertEqual(audit.rows(where=base, step="link"), [])


class TestDryBuildIsSilent(unittest.TestCase):
    def test_a_dry_build_writes_no_audit_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, SHAPE_WITH_A_PAIR)
            understand.digest(files, door=port.door(), dry=False)
            associate.build(files, door=port.door(), dry=True)
            self.assertEqual(audit.rows(where=base, step="link"), [])


class TestASecondPassAuditsAgain(unittest.TestCase):
    """Повторный проход по тому же архиву не находит нового поводом — тоже данные."""

    def test_the_idle_second_pass_writes_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, SHAPE_WITH_A_PAIR)
            run(files)
            first = len(audit.rows(where=base, step="link"))
            associate.build(files, door=port.door(), dry=False)
            self.assertEqual(len(audit.rows(where=base, step="link")), first,
                             "неизменившийся архив не должен переписывать связи")


if __name__ == "__main__":
    unittest.main()
