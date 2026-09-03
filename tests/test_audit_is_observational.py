#!/usr/bin/env python3
"""Аудит не меняет исход прогона — он только наблюдает.

Запуск: python3 -m unittest tests.test_audit_is_observational -v

DoD задачи требует буквально: «тот же прогон без аудита даёт тот же исход».
Ключа «выключи аудит» нет и не будет — см. докстринг `storage.audit`:
наблюдение не имеет права быть тем, что можно выключить, иначе на выключенном
рубильнике разбор снова слепнет молча.

Проверяется поэтому не ключ, а сам довод буквально: шаг конвейера прогоняется
дважды на одинаковом входе — один раз с настоящей записью аудита, другой раз с
её точкой входа, подменённой на пустую операцию (то есть ровно «аудита нет
вовсе», без всякого рубильника), — и сравнивается всё, ЧТО НЕ ЕСТЬ таблицей
audit: факты, связи, переклады, свёртки, ответы подсказки, строки ленты. Если
это всё совпадает — аудит и вправду только наблюдал, а не участвовал в
решении.

Мутации, на которых проверки обязаны краснеть:
  * инструментация меняет входные данные шага перед вызовом основного кода
  * инструментация меняет, что шаг возвращает вызывающему
  * инструментация меняет содержимое таблиц, кроме audit
"""
import contextlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, lifespan, models
from pipeline import associate, consolidate, forget, suggest, understand
from storage import audit, db, local, port

CWD = "/home/person/dev/demo"
BRANCH = "audit-observational"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def store(tmp, mode=None):
    base = Path(tmp) / "memory.db"
    local.close()
    audit.reset(base)
    env = {"XMEM_BACKEND": "local", "XMEM_DISABLED": "", "XMEM_LOCAL_PATH": str(base),
          "XMEM_MEMORY": mode or "", "XMEM_STATE_DIR": str(Path(tmp) / "state")}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"), \
         mock.patch.object(ledger, "LOG", Path(tmp) / "ledger.jsonl"):
        try:
            yield base
        finally:
            local.close()
            audit.reset(base)


def rows_of(base, table):
    import sqlite3
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM "%s"' % table)]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def archive_rows(session, request, files, reply="Готово."):
    stamp = "2026-08-28T09:00:00Z"
    head = {"sessionId": session, "timestamp": stamp, "cwd": CWD, "gitBranch": BRANCH}
    out = [dict(head, type="user", message={"content": request})]
    blocks = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "%s/%s" % (CWD, n)}}
             for n in files]
    blocks.append({"type": "text", "text": reply})
    out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def write_archive(root, session, request, files, name="разговор.jsonl"):
    path = Path(root) / name
    with path.open("a", encoding="utf-8") as fh:
        for line in archive_rows(session, request, files):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


def run_with(patched, action, target=None):
    """`action(base)` дважды: с настоящим аудитом и с ней же, замененной на пустоту.

    `target` — какую точку входа подменить: по умолчанию `storage.audit.record`,
    которой пользуются понимание, связи, поиск и оценка. У забывания и свёртки
    своя запись через уже открытое соединение (`Repository._audit`, см.
    `storage/db.py` — иначе `storage.db` и `storage.audit` образовали бы кольцо
    импортов), и там подменять нужно её.

    Отдаёт пару результатов и пару снимков базы (без таблицы audit), чтобы
    вызывающий сравнил и то и другое.
    """
    module, name = target or (audit, "record")
    results, snapshots = [], []
    for use_real in (True, False):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            if use_real:
                got = action(tmp, base)
            else:
                with mock.patch.object(module, name, lambda *a, **k: None):
                    got = action(tmp, base)
            results.append(got)
            snapshots.append({t: rows_of(base, t) for t in patched})
    return results, snapshots


class TestUnderstandingIsUnaffected(unittest.TestCase):
    def test_the_same_archive_writes_the_same_facts_with_or_without_audit(self):
        def action(tmp, base):
            files = write_archive(tmp, "разговор-1",
                                  "Отвечай кратко, длинные ответы не читаю",
                                  ["db.py", "port.py"])
            return understand.digest(files, archive=files, door=port.door(),
                                     dry=False, min_score=0.0)

        results, snapshots = run_with(("fact", "episode", "session", "event"), action)
        self.assertEqual(results[0]["facts"], results[1]["facts"])
        self.assertEqual(results[0]["episodes"], results[1]["episodes"])
        for table in ("fact", "episode"):
            got0 = sorted(json.dumps(r, sort_keys=True, default=str) for r in snapshots[0][table])
            got1 = sorted(json.dumps(r, sort_keys=True, default=str) for r in snapshots[1][table])
            self.assertEqual(got0, got1, table)


class TestAssociationsAreUnaffected(unittest.TestCase):
    def test_the_same_archive_makes_the_same_graph(self):
        def action(tmp, base):
            files = write_archive(tmp, "разговор-1", "Посмотри, что там с базой",
                                  ["db.py", "port.py"])
            understand.digest(files, door=port.door(), dry=False)
            return associate.build(files, door=port.door(), dry=False)

        results, snapshots = run_with(("association",), action)
        self.assertEqual(results[0]["cards"], results[1]["cards"])
        got0 = sorted((r["source_key"], r["target_key"], r["cue"])
                     for r in snapshots[0]["association"])
        got1 = sorted((r["source_key"], r["target_key"], r["cue"])
                     for r in snapshots[1]["association"])
        self.assertEqual(got0, got1)


class TestForgetIsUnaffected(unittest.TestCase):
    def test_the_same_facts_lapse_the_same_way(self):
        def action(tmp, base):
            door = port.door()
            door.write_objects([models.Fact(
                fact_type="project_state", subject="%s/db.py" % CWD, scope="project",
                content="правился файл db.py", project="demo",
                updated_at=lifespan.stamp(T0), valid_until=lifespan.until(T0, "short"))])
            return forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))

        results, snapshots = run_with(("fact", "lapsedfact"), action,
                                      target=(db.Repository, "_audit"))
        self.assertEqual(results[0]["moved"], results[1]["moved"])
        self.assertEqual(len(snapshots[0]["lapsedfact"]), len(snapshots[1]["lapsedfact"]))
        self.assertEqual(snapshots[0]["fact"], snapshots[1]["fact"])


class TestFoldIsUnaffected(unittest.TestCase):
    def test_the_same_duplicates_fold_the_same_way(self):
        def action(tmp, base):
            door = port.door()
            door.write_objects([
                models.Fact(fact_type="project_state", subject="db.py", scope="project",
                           project="demo", content="одно и то же",
                           updated_at=lifespan.stamp(T0)),
                models.Fact(fact_type="project_state", subject="db2.py", scope="project",
                           project="demo", content="одно и то же",
                           updated_at=lifespan.stamp(T0 + timedelta(hours=1)))])
            return consolidate.fold(door=door, now=lifespan.stamp(T0 + timedelta(days=1)))

        results, snapshots = run_with(("fact", "lapsedfact"), action,
                                      target=(db.Repository, "_audit"))
        self.assertEqual(results[0]["folded"], results[1]["folded"])
        self.assertEqual(len(snapshots[0]["fact"]), len(snapshots[1]["fact"]))
        got0 = sorted(r.get("merged_into") or "" for r in snapshots[0]["lapsedfact"])
        got1 = sorted(r.get("merged_into") or "" for r in snapshots[1]["lapsedfact"])
        self.assertEqual(got0, got1)


class Answering:
    name = "local"

    def __init__(self, answer):
        self.answer = answer

    def read(self, query, mode="single"):
        return self.answer

    def write(self, text, wait=False):
        return port.door().write(text, wait)

    def write_objects(self, records, relations=(), op="create"):
        return port.door().write_objects(records, relations, op)


def fact_piece(content, score=0.9):
    body = dict(object_type="Fact", fact_type="project_state", subject="демо",
               scope="project", content="%s Оценка уверенности: %.2f" % (content, score))
    return json.dumps([body], ensure_ascii=False)


FROZEN = "2026-01-01T00:00:00+00:00"


class TestSearchAndInjectAreUnaffected(unittest.TestCase):
    def test_the_same_query_returns_the_same_text_and_candidates(self):
        # `at` заморожен: без него ключ вставки несёт «сейчас», и два прогона
        # разойдутся временем даже без единой правки от аудита — сравнение
        # мерило бы часы, а не то, что проверяет тест.
        def action(tmp, base):
            door = Answering(fact_piece("правился db.py", score=0.9))
            text, kept, why = suggest.attend("db.py", session_id="разговор-1",
                                             door=door, record=False, at=FROZEN)
            return {"text": text, "kept_texts": [t for _, t, _ in kept], "why": why}

        results, _ = run_with((), action)
        self.assertEqual(results[0], results[1])

    def test_a_silent_query_stays_silent_the_same_way(self):
        def action(tmp, base):
            door = Answering("")
            text, kept, why = suggest.attend("что-то", session_id="разговор-1",
                                             door=door, record=False, at=FROZEN)
            return {"text": text, "kept": kept, "why": why}

        results, _ = run_with((), action)
        self.assertEqual(results[0], results[1])


class TestJudgeIsUnaffected(unittest.TestCase):
    def test_settle_reaches_the_same_verdict_and_the_same_ledger(self):
        def action(tmp, base):
            talk, at = "разговор-1", "2026-08-28T08:59:00Z"
            suggest.remember(suggest.injection_of(talk, "подсказка", at=at),
                             log=suggest.LOG)
            files = write_archive(tmp, talk,
                                  "Отвечай кратко, длинные ответы не читаю", [])
            got = suggest.settle(files, door=port.door(), log=suggest.LOG)
            # `at` строки ленты — момент записи, не входной параметр; он и
            # без всякого аудита разойдётся между двумя прогонами по часам.
            # Сравниваем всё остальное поле в поле.
            stripped = [{k: v for k, v in row.items() if k != "at"}
                       for row in ledger.rows()]
            return {"settled": got["settled"], "logged": got["logged"],
                   "ledger": stripped}

        results, _ = run_with((), action)
        self.assertEqual(results[0]["settled"], results[1]["settled"])
        self.assertEqual(results[0]["logged"], results[1]["logged"])
        self.assertEqual(results[0]["ledger"], results[1]["ledger"])


if __name__ == "__main__":
    unittest.main()
