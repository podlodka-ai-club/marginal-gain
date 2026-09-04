#!/usr/bin/env python3
"""Аудит переживает песочницу — своей, постоянной базой.

Запуск: python3 -m pytest tests/test_audit_persists.py -q

Аудит шагов конвейера жил в базе песочницы (`storage.audit`, таблица в том же
файле, что и факты), а песочницу после прогона убирают целиком. Данные, ради
которых аудит и заводился — что именно вбросили и что ответил агент, — исчезали
вместе с ней: непройденную пару разобрать было не по чему, только по памяти
человека, смотревшего на экран во время прогона.

Решение — не выгрузка в файл, а отдельная постоянная база: `Sandbox.audit_db`
живёт вне каталога песочницы (`self.root`), рядом с журналом прогонов, и её
не касается снос `Sandbox.close()`. Путь ходам передаётся окружением
(`XMEM_AUDIT_PATH`, `storage.audit.path`), и по нему же классы «дали не то» /
«отдала, не применил» можно пересчитать позже, запросом к базе, а не только
из уже посчитанного поля строки.

Стенд (`live.run`/`live.main`) здесь не поднимается — лимит на такие проверки
уже выбран тремя в `tests/test_full_run.py` (см. `tests/test_suite_shape.py`).
Всё ниже проверяет `Sandbox` и `judge_one` напрямую, своей песочницей на диске,
без единого хода агента.

Свойства:

1. `Sandbox.audit_db` по умолчанию — файл рядом с журналом прогонов (в корне
   репозитория), не внутри `self.root`, и не совпадает с ним ни при каком
   `root`, включая относительный и вложенный в один и тот же каталог.
2. `env()` называет ходам путь к аудиту явно (`XMEM_AUDIT_PATH`), тем же
   объектом, что хранит `self.audit_db`, — не пересчитывает его отдельно.
3. `Sandbox.close()` не трогает `audit_db`: файл, лежащий там до `close()`,
   лежит там и после, даже когда каталог песочницы снесён.
4. `check()` отказывает так же, как и для `root`, если названный `audit_db`
   задевает живое состояние пользователя.
5. Аудит несёт дословный ответ агента второй сессии шагом `reply` — писать
   его больше некому: конец хода там снят намеренно.
6. Класс провала («дали не то» / «отдала, не применил») можно восстановить
   запросом к аудиту постфактум — тем же способом, каким его считает `bucket`,
   а не догадкой по ответу.

Мутации, на которых проверки обязаны краснеть:
  * положить audit_db внутрь self.root по умолчанию      → TestAuditDbLivesOutsideTheSandbox
  * не прокинуть XMEM_AUDIT_PATH в окружение ходов        → TestAuditDbLivesOutsideTheSandbox
  * снести audit_db вместе с песочницей в close()         → TestCloseNeverTouchesTheAuditDb
  * не писать шаг `reply` второй сессии в аудит           → TestTheSecondSessionAnswerIsAudited
  * не проверять audit_db в check() на живое состояние    → TestCheckGuardsTheAuditDbToo
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live
from storage import audit


# --- 1. путь по умолчанию — вне песочницы ------------------------------------

class TestAuditDbLivesOutsideTheSandbox(unittest.TestCase):
    """Свойства 1 и 2. Своя постоянная база, а не файл внутри `self.root`."""

    def test_the_default_lives_next_to_the_run_journal(self):
        box = live.Sandbox(root="/tmp/не-открывается")
        self.assertEqual(box.audit_db, live.DEFAULT_AUDIT_DB)
        self.assertTrue(box.audit_db.is_relative_to(live.ROOT))

    def test_the_default_is_never_inside_the_sandbox_root(self):
        box = live.Sandbox(root="/tmp/не-открывается")
        self.assertFalse(box.audit_db.is_relative_to(box.root))

    def test_a_named_audit_db_is_kept_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "своя.db"
            box = live.Sandbox(root="/tmp/не-открывается", audit_db=named)
            self.assertEqual(box.audit_db, named)

    def test_env_names_the_audit_path_by_the_same_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "своя.db"
            box = live.Sandbox(root="/tmp/не-открывается", audit_db=named)
            self.assertEqual(box.env({})["XMEM_AUDIT_PATH"], str(box.audit_db))

    def test_the_report_names_where_the_audit_lives(self):
        """Человеку, читающему отчёт, незачем угадывать путь к аудиту."""
        box = live.Sandbox(root="/tmp/не-открывается")
        report = live.Report(box, live.Agent.name, [])
        self.assertIn(str(box.audit_db), report.text())


# --- 3. снос песочницы не задевает аудит -------------------------------------

class TestCloseNeverTouchesTheAuditDb(unittest.TestCase):
    """Свойство 3. `close()` сносит `self.root` целиком, `audit_db` — никогда."""

    def test_a_file_at_the_audit_path_survives_the_sandbox_teardown(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_db = Path(tmp) / "аудит.db"
            audit.record("mark", input={"a": 1}, where=audit_db)
            self.assertTrue(audit_db.exists())

            box = live.Sandbox(root=Path(tmp) / "sandbox", audit_db=audit_db)
            box.open()
            self.assertTrue(box.root.exists())
            box.close()

            self.assertFalse(box.root.exists(), "песочница не снесена")
            self.assertTrue(audit_db.exists(),
                            "снос песочницы задел постоянную базу аудита")
            self.assertEqual(len(audit.rows(where=audit_db)), 1,
                             "запись аудита пропала вместе с песочницей")

    def test_the_audit_db_is_not_created_inside_the_removed_root(self):
        """Даже когда файл ещё не существовал: путь остаётся вне снесённого дерева."""
        with tempfile.TemporaryDirectory() as tmp:
            audit_db = Path(tmp) / "новый-аудит.db"
            box = live.Sandbox(root=Path(tmp) / "sandbox", audit_db=audit_db)
            box.open()
            box.close()
            self.assertFalse(audit_db.is_relative_to(Path(tmp) / "sandbox"))


# --- 4. check() бережёт аудит так же, как root -------------------------------

class TestCheckGuardsTheAuditDbToo(unittest.TestCase):
    """Свойство 4. Названная база аудита не имеет права попасть в живое состояние."""

    def test_an_audit_db_inside_live_state_is_refused(self):
        """Root безобидный (иначе он же и раскраснил бы проверку про своё) —
        падать обязан именно audit_db."""
        with tempfile.TemporaryDirectory() as tmp:
            box = live.Sandbox(root=Path(tmp) / "sandbox",
                               audit_db=live.LIVE_STATE / "аудит.db")
            with self.assertRaises(live.UnsafeRun) as caught:
                box.check()
            self.assertIn("аудит", str(caught.exception))

    def test_the_live_state_path_itself_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = live.Sandbox(root=Path(tmp) / "sandbox", audit_db=live.LIVE_STATE)
            with self.assertRaises(live.UnsafeRun) as caught:
                box.check()
            self.assertIn("аудит", str(caught.exception))

    def test_an_ordinary_audit_db_passes_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = live.Sandbox(root=Path(tmp) / "sandbox",
                               audit_db=Path(tmp) / "audit.db")
            box.check()   # не должно бросить

    def test_an_audit_db_inside_the_sandbox_root_is_refused(self):
        """Аудит обязан пережить снос — лечь внутрь `root` ему нельзя: `close()`
        сносит `self.root` целиком, вместе со всем, что там лежит."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sandbox"
            box = live.Sandbox(root=root, audit_db=root / "audit.db")
            with self.assertRaises(live.UnsafeRun) as caught:
                box.check()
            self.assertIn("аудит", str(caught.exception))

    def test_the_sandbox_root_itself_as_audit_db_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sandbox"
            box = live.Sandbox(root=root, audit_db=root)
            with self.assertRaises(live.UnsafeRun) as caught:
                box.check()
            self.assertIn("аудит", str(caught.exception))

    def test_a_relative_not_yet_existing_audit_db_inside_live_state_is_refused(self):
        """Относительный путь, которого ещё нет на диске, обязан разрешиться до
        абсолютного и пройти ту же проверку — иначе живое состояние молча
        проскакивает мимо, пока за него не отвечает ни один существующий файл.

        Условная версия (`.resolve()` только если файл уже есть, как раньше
        было и для `audit_db`) сравнивала бы здесь неразрешённый относительный
        путь с абсолютным `LIVE_STATE` — и не совпало бы никогда.

        `LIVE_STATE` подменён своим временным каталогом: настоящее живое
        состояние пользователя эта проверка трогать не имеет права даже
        `mkdir`-ом, не то что записью.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            # `.resolve()` сразу: `tempfile` на macOS отдаёт путь через
            # `/var/folders/...`, а `/var` — симлинк на `/private/var`.
            # Не разреши его здесь — и `Path.cwd()` внутри `resolve()` ниже
            # вернёт уже расправленный путь, который не совпадёт с этим же
            # каталогом, взятым как есть: тест сравнивал бы два имени одного
            # и того же места и красил бы себя, а не код.
            fake_live_state = (Path(fake_home) / "live-state").resolve()
            fake_live_state.mkdir(parents=True)
            was = os.getcwd()
            os.chdir(fake_live_state)
            try:
                with mock.patch.object(live, "LIVE_STATE", fake_live_state), \
                     tempfile.TemporaryDirectory() as tmp:
                    box = live.Sandbox(root=Path(tmp) / "sandbox",
                                       audit_db=Path("ещё-не-созданный/audit.db"))
                    with self.assertRaises(live.UnsafeRun) as caught:
                        box.check()
                    self.assertIn("аудит", str(caught.exception))
            finally:
                os.chdir(was)

    def test_a_relative_not_yet_existing_audit_db_elsewhere_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            was = os.getcwd()
            os.chdir(tmp)
            try:
                root = Path(tmp) / "sandbox"
                box = live.Sandbox(root=root, audit_db=Path("ещё-нет/audit.db"))
                box.check()   # не должно бросить — путь свежий и снаружи LIVE_STATE
                self.assertFalse((Path(tmp) / "ещё-нет" / "audit.db").exists())
            finally:
                os.chdir(was)


# --- 5. ответ второй сессии — шагом `reply` ----------------------------------

class TestTheSecondSessionAnswerIsAudited(unittest.TestCase):
    """Свойство 5. Конец хода снят на второй сессии — записывает сам замер."""

    def _reply_rows(self, tmp, said="Ответ дословно: MacBook Pro M5"):
        audit_db = Path(tmp) / "audit.db"
        state = Path(tmp) / "state"
        state.mkdir()
        box = mock.Mock(audit_db=audit_db, state=state, run_id="test-run")
        pair = {"id": "макбук", "aim": "apply", "task": {"say": "какой у меня ноутбук?"},
               "expect": ["M5"], "forbid": []}
        reply = live.Reply(text=said, session_id="talk-x", cost=0.0, error=None)
        live.judge_one(box, pair, reply)
        return audit.rows(where=audit_db, session_id="talk-x", step="reply")

    def test_the_verbatim_answer_lands_in_the_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._reply_rows(tmp, said="Ответ дословно: MacBook Pro M5")
            self.assertEqual(len(rows), 1)
            self.assertIn("MacBook Pro M5",
                          rows[0]["output"]["replies"][0])

    def test_the_row_carries_the_run_id_explicitly(self):
        """`XMEM_RUN_ID` в окружении процесса замера не называется вовсе — оно
        только в словаре для чужих подпроцессов (`Sandbox.env()`). Без явного
        `run=box.run_id` этот шаг осел бы с пустым `run_id`, неотличимый от
        строки вне всякого прогона, хотя прогон у неё есть.
        """
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._reply_rows(tmp)
            self.assertEqual(rows[0]["run_id"], "test-run")

    def test_the_answer_is_readable_text_not_an_opaque_blob(self):
        """Человек должен прочитать ответ глазами, не расшифровывать формат."""
        with tempfile.TemporaryDirectory() as tmp:
            said = "Твой рабочий ноутбук — MacBook Pro M5, 16 гигабайт."
            rows = self._reply_rows(tmp, said=said)
            self.assertEqual(rows[0]["output"]["replies"], [said])


# --- 6. класс провала восстановим запросом к аудиту --------------------------

class TestTheOutcomeClassIsRecoverableFromTheAuditAlone(unittest.TestCase):
    """Свойство 6. «Дали не то» и «отдала, не применил» видны и без поля строки.

    Тот же вопрос, что решает `bucket()` по `expect_in_feed`, можно задать
    напрямую базе: искало ли ожидаемое слово в тексте шага `inject` этой
    сессии. Совпадение двух путей — это и есть довод, почему постоянный аудит
    не подменяет признак в журнале, а подтверждает его независимо.
    """

    def _fed_text(self, audit_db, session_id):
        rows = audit.rows(where=audit_db, session_id=session_id, step="inject")
        return (rows[-1]["output"] or {}).get("text", "") if rows else ""

    def test_wrong_fed_is_visible_as_a_missing_word_in_the_inject_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_db = Path(tmp) / "audit.db"
            audit.record("inject", session_id="s", input={"kept": 1},
                        output={"text": "диагональ 14 дюймов, куплен вчера"},
                        where=audit_db)
            fed = self._fed_text(audit_db, "s")
            self.assertNotIn("m5", fed.lower())

            state = Path(tmp) / "state"
            state.mkdir()
            from domain import ledger
            ledger.injected("s", "2026-01-01T00:00:00Z", log=state / "ledger.jsonl")
            box = mock.Mock(audit_db=audit_db, state=state, run_id="test-run")
            pair = {"id": "макбук", "aim": "apply", "task": {"say": "?"},
                   "expect": ["M5"], "forbid": []}
            reply = live.Reply(text="у тебя 14-дюймовый экран", session_id="s",
                              cost=0.0, error=None)
            row = live.judge_one(box, pair, reply)
            self.assertEqual(live.bucket(row), live.WRONG_FED)

    def test_right_fed_but_ignored_is_visible_as_the_word_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_db = Path(tmp) / "audit.db"
            audit.record("inject", session_id="s", input={"kept": 1},
                        output={"text": "рабочий ноутбук: MacBook Pro M5"},
                        where=audit_db)
            fed = self._fed_text(audit_db, "s")
            self.assertIn("m5", fed.lower())

            state = Path(tmp) / "state"
            state.mkdir()
            from domain import ledger
            ledger.injected("s", "2026-01-01T00:00:00Z", log=state / "ledger.jsonl")
            box = mock.Mock(audit_db=audit_db, state=state, run_id="test-run")
            pair = {"id": "макбук", "aim": "apply", "task": {"say": "?"},
                   "expect": ["M5"], "forbid": []}
            reply = live.Reply(text="у тебя хороший ноутбук", session_id="s",
                              cost=0.0, error=None)
            row = live.judge_one(box, pair, reply)
            self.assertEqual(live.bucket(row), live.UNUSED)


if __name__ == "__main__":
    unittest.main()
