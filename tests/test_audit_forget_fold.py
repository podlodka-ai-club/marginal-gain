#!/usr/bin/env python3
"""Аудит забывания и уплотнения: `Repository.lapse` / `Repository.fold`.

Запуск: python3 -m unittest tests.test_audit_forget_fold -v

«Забывание и уплотнение» из задачи — что просрочено, что свёрнуто, что
удалено. Проверяется тем же приёмом, что и остальной аудит: свойство, а не
пример, и мутация должна красить проверку.

Мутации, на которых проверки обязаны краснеть:
  * не писать строку аудита, когда просрочивать/сворачивать было нечего  → TestForgetIsAuditedEvenWhenEmpty, TestFoldIsAuditedEvenWhenEmpty
  * писать аудит и на холостом прогоне (dry=True)                       → TestDryRunWritesNoAudit
  * не называть в выходе, какие именно записи выбыли/свернулись          → TestForgetNamesTheMovedFacts, TestFoldNamesTheMergedFacts
"""
import contextlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import audit, lifespan, models
from pipeline import consolidate, forget, suggest, understand
from storage import local, port

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])

CWD = "/home/person/dev/demo"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    audit.reset(base)
    env = {"XMEM_BACKEND": "local", "XMEM_DISABLED": "", "XMEM_LOCAL_PATH": str(base),
          "XMEM_STATE_DIR": str(Path(tmp) / "state")}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()
            audit.reset(base)


def fact(name, at, mode=None, kind="project_state", scope="project"):
    return models.Fact(fact_type=kind, subject="%s/%s" % (CWD, name), scope=scope,
                       content="правился файл %s" % name, project="demo",
                       updated_at=lifespan.stamp(at), valid_until=lifespan.until(at, mode))


def put(door, *records):
    door.write_objects(list(records))


class TestForgetIsAuditedEvenWhenEmpty(unittest.TestCase):
    def test_a_sweep_that_moves_nothing_still_leaves_a_row(self):
        """«Ничего не найдено» — тоже действие: строка есть даже без переклада."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(hours=1)))
            got = audit.rows(where=base, step="forget")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["output"]["moved"], [])
            self.assertTrue(got[0]["ok"])

    @SLOW
    @given(names=st.lists(st.sampled_from(["a.py", "b.py", "c.py", "d.py"]),
                          min_size=1, max_size=4, unique=True))
    def test_the_moved_facts_are_named_in_the_output(self, names):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            for name in names:
                put(door, fact(name, T0, mode="short"))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            row = audit.rows(where=base, step="forget")[-1]
            subjects = sorted(m["subject"] for m in row["output"]["moved"])
            self.assertEqual(subjects, sorted("%s/%s" % (CWD, n) for n in names))


class TestForgetNamesTheMovedFacts(unittest.TestCase):
    def test_content_travels_with_the_audit_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            row = audit.rows(where=base, step="forget")[-1]
            self.assertEqual(row["output"]["moved"][0]["content"], "правился файл db.py")


class TestDryRunWritesNoAudit(unittest.TestCase):
    def test_a_dry_sweep_is_not_an_action(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)), dry=True)
            self.assertEqual(audit.rows(where=base, step="forget"), [])

    def test_a_dry_fold_is_not_an_action(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0), fact("db-copy.py", T0))
            consolidate.fold(door=door, now=lifespan.stamp(T0), dry=True)
            self.assertEqual(audit.rows(where=base, step="fold"), [])


class TestFoldIsAuditedEvenWhenEmpty(unittest.TestCase):
    def test_a_fold_that_merges_nothing_still_leaves_a_row(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            got = audit.rows(where=base, step="fold")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["output"]["merges"], [])


class TestFoldNamesTheMergedFacts(unittest.TestCase):
    def test_duplicate_content_folds_into_one_and_the_row_names_both_sides(self):
        """Дубль — та же тема, охват, проект и посимвольно то же содержание."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            one = models.Fact(fact_type="project_state", subject="db.py",
                              scope="project", project="demo", content="одно и то же",
                              updated_at=lifespan.stamp(T0))
            two = models.Fact(fact_type="project_state", subject="db2.py",
                              scope="project", project="demo", content="одно и то же",
                              updated_at=lifespan.stamp(T0 + timedelta(hours=1)))
            put(door, one, two)
            consolidate.fold(door=door, now=lifespan.stamp(T0 + timedelta(days=1)))
            row = audit.rows(where=base, step="fold")[-1]
            self.assertEqual(len(row["output"]["merges"]), 1)
            merge = row["output"]["merges"][0]
            self.assertIn(merge["kept"], (one.identity(), two.identity()))
            self.assertEqual(len(merge["merged"]), 1)
            self.assertEqual(merge["content"], "одно и то же")


if __name__ == "__main__":
    unittest.main()
