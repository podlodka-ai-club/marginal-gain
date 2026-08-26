#!/usr/bin/env python3
"""Проверки моделей и адаптеров. Запуск: python3 -m unittest test_adapters -v

Тесты писались после ревью, которое нашло две поломки исполнением, а не чтением:
чтение через адаптер возвращало не строку, и ссылка по ключу требовала полей,
которых у ссылки нет. Обе закрыты здесь.
"""
import json, os, unittest
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

import evaluate
import goldenset
import matrix
import models
import telemetry
import suggest
import xmem
import xmem_api


class TestKey(unittest.TestCase):
    def test_mutation_splits_key_and_values(self):
        f = models.Fact(fact_type="project_state", subject="evaluate.py",
                        scope="project", content="модуль оценки")
        body = f.mutation()["object_mutation"]["create"]
        self.assertEqual(body["key"],
                         {"fact_type": "project_state", "subject": "evaluate.py",
                          "scope": "project"})
        self.assertEqual(body["values"], {"content": "модуль оценки"})

    def test_empty_values_are_dropped(self):
        f = models.Fact(fact_type="user", subject="a", scope="global", content="c")
        self.assertNotIn("project", f.mutation()["object_mutation"]["create"]["values"])

    def test_identity_matches_key(self):
        f = models.Fact(fact_type="user", subject="a", scope="global", content="c")
        self.assertEqual(f.identity(), "user|a|global")


class TestValidation(unittest.TestCase):
    def test_enum_outside_schema_rejected(self):
        with self.assertRaises(models.SchemaError):
            models.Fact(fact_type="bogus", subject="a", scope="global",
                        content="c").mutation()

    def test_missing_required_rejected_on_create(self):
        with self.assertRaises(models.SchemaError):
            models.Episode(session_id="s", episode_number=1, title="t").mutation()

    def test_empty_key_rejected(self):
        with self.assertRaises(models.SchemaError):
            models.Session().mutation()

    def test_delete_needs_key_only(self):
        m = models.Fact(fact_type="user", subject="a", scope="global").mutation("delete")
        self.assertEqual(m["object_mutation"]["delete"]["key"]["subject"], "a")
        self.assertNotIn("values", m["object_mutation"]["delete"])

    def test_unknown_operation_rejected(self):
        with self.assertRaises(models.SchemaError):
            models.Session(session_id="s").mutation("upsert")


class TestRedaction(unittest.TestCase):
    """Структурный путь не должен стать дырой мимо redact, см. коммит 9c2d7fb."""

    def test_secret_in_values_is_masked(self):
        f = models.Fact(fact_type="user", subject="a", scope="global",
                        content="токен glpat-abcdefghijkl")
        got = f.mutation()["object_mutation"]["create"]["values"]["content"]
        self.assertNotIn("glpat-abcdefghijkl", got)

    def test_secret_in_key_is_masked(self):
        f = models.Fact(fact_type="external_resource", scope="global", content="c",
                        subject="https://oauth2:glpat-abcdefghijkl@git.example/x")
        got = f.mutation()["object_mutation"]["create"]["key"]["subject"]
        self.assertNotIn("glpat-abcdefghijkl", got)


class TestLink(unittest.TestCase):
    def test_endpoints_by_key_only(self):
        m = models.link("session_episodes",
                        session=models.Session(session_id="s"),
                        episode=models.Episode(session_id="s", episode_number=1))
        ends = m["relation_mutation"]["create"]["endpoints"]
        self.assertEqual({e["object_name"] for e in ends}, {"session", "episode"})

    def test_wrong_end_type_rejected(self):
        with self.assertRaises(models.SchemaError):
            models.link("episode_facts",
                        episode=models.Episode(session_id="s", episode_number=1),
                        fact=models.Episode(session_id="s", episode_number=2))

    def test_unknown_relation_rejected(self):
        with self.assertRaises(models.SchemaError):
            models.link("нет такой", session=models.Session(session_id="s"))


class TestBackend(unittest.TestCase):
    def test_structured_write_refused_on_cli(self):
        with mock.patch.object(xmem, "BACKEND", "cli"):
            with self.assertRaises(xmem.BackendError):
                xmem.write_objects([models.Session(session_id="s")])

    def test_unknown_backend_refused(self):
        with mock.patch.object(xmem, "BACKEND", "лишний"):
            with self.assertRaises(xmem.BackendError):
                xmem.read("что угодно")

    def test_read_mode_translated_to_service_name(self):
        stub = mock.Mock(read=mock.Mock(return_value=""))
        with mock.patch.object(xmem, "_adapter", return_value=stub):
            xmem.read("вопрос", mode="raw")
        self.assertEqual(stub.read.call_args.kwargs["mode"], "raw-tables")


class TestHttpAdapter(unittest.TestCase):
    def test_read_returns_text_not_dict(self):
        """Вызывающий разбирает ответ строкой — адаптер обязан её отдать."""
        with mock.patch.object(xmem_api, "_call",
                               return_value={"reader_result": {"rows": [[4]]}}):
            self.assertIsInstance(xmem_api.read("сколько"), str)

    def test_read_sends_service_field_name(self):
        """Сервис называет поле mode; read_mode он молча игнорирует."""
        with mock.patch.object(xmem_api, "_call", return_value={}) as call:
            xmem_api.read("вопрос", mode="raw-tables")
        self.assertEqual(call.call_args.args[1], {"query": "вопрос", "mode": "raw-tables"})

    def test_missing_answer_is_empty_text(self):
        with mock.patch.object(xmem_api, "_call", return_value={}):
            self.assertEqual(xmem_api.read("вопрос"), "")

    def test_empty_batch_refused(self):
        with self.assertRaises(xmem_api.ApiError):
            xmem_api.write_objects([])

    def test_empty_env_falls_back_to_default_address(self):
        """Пустая переменная в окружении не должна давать относительный адрес."""
        self.assertTrue(xmem_api.BASE.startswith("http"))


class TestSuggestSurvivesAdapterAnswer(unittest.TestCase):
    """Ревью нашло это исполнением: dict вместо строки убивал подсказку молча."""

    def test_pieces_parses_adapter_answer(self):
        answer = json.dumps(["Оценка уверенности: 0.90. Факт про модуль оценки."],
                            ensure_ascii=False)
        got = suggest.pieces(answer)
        self.assertTrue(got)
        self.assertGreaterEqual(got[0][0], 0.9)


class TestGoldenSet(unittest.TestCase):
    """Набор уезжает в репозиторий, поэтому личное в нём — не мелочь."""

    def test_home_path_replaced(self):
        got = goldenset.anonymize("правился файл %s/dev/x/y.py" % os.path.expanduser("~"))
        self.assertNotIn(os.path.expanduser("~"), got)
        self.assertIn("/home/person", got)

    def test_mail_replaced(self):
        self.assertNotIn("ivan.petrov@bank.ru",
                         goldenset.anonymize("писал на ivan.petrov@bank.ru вчера"))

    def test_address_password_replaced(self):
        got = goldenset.anonymize("https://oauth2:glpat-abcdefghijkl@git.example/x")
        self.assertNotIn("glpat-abcdefghijkl", got)

    def test_ip_replaced(self):
        self.assertNotIn("192.168.11.42", goldenset.anonymize("хост 192.168.11.42 упал"))

    def test_anonymize_is_stable(self):
        """Подмена детерминирована, иначе связи между записями рвутся."""
        text = "%s/dev/проект" % os.path.expanduser("~")
        self.assertEqual(goldenset.anonymize(text), goldenset.anonymize(text))

    def test_script_turns_carry_what_they_feed(self):
        """Реплика без привязки к случаю бесполезна: нечем проверить результат."""
        turns = json.load(open("eval-script.json", encoding="utf-8"))
        self.assertTrue(turns)
        self.assertTrue(all(t["feeds"] for t in turns))

    def test_script_is_ordered_by_time(self):
        """Иначе разговор про файл случается раньше, чем файл в нём появился."""
        turns = json.load(open("eval-script.json", encoding="utf-8"))
        stamps = [t["started_at"] for t in turns]
        self.assertEqual(stamps, sorted(stamps))

    def test_every_case_is_reachable_from_script(self):
        cases = json.load(open("eval-cases.json", encoding="utf-8"))
        turns = json.load(open("eval-script.json", encoding="utf-8"))
        fed = {c for t in turns for c in t["feeds"]}
        need = {c["id"] for c in cases if c["kind"] != "absence"}
        self.assertEqual(need - fed, set())

    def test_absence_cases_have_forbid(self):
        case = goldenset.case_absent(1, *goldenset.ABSENT[0])
        self.assertEqual(case["expect"], [])
        self.assertTrue(case["forbid"])


class TestTraceFile(unittest.TestCase):
    """Пишем настоящим emit в настоящий файл: подмена emit его не проверяет."""

    def log_rows(self, body, name):
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as tmp:
            log = _Path(tmp) / "trace.jsonl"
            with mock.patch.object(telemetry, "ENABLED", True), \
                 mock.patch.object(telemetry, "LOG", log), \
                 mock.patch.object(telemetry, "_HANDLE", None):
                body(telemetry.traced(name)(lambda: 1))
                telemetry.close()
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]

    def test_step_without_trace_is_marked_orphan(self):
        """Шаг в потоке теряет метку — потеря должна быть громкой, не тихой."""
        rows = self.log_rows(lambda step: step(), "одинокий-шаг")
        self.assertTrue(rows[0]["metadata"].get("orphan"))
        self.assertEqual(rows[0]["test_id"], "")
        self.assertIn("без метки случая: 1", telemetry.report(rows))

    def test_step_in_thread_loses_label_loudly(self):
        import threading as _t

        def body(step):
            with telemetry.Trace("в-потоке"):
                worker = _t.Thread(target=step)
                worker.start()
                worker.join()

        rows = self.log_rows(body, "шаг-в-потоке")
        self.assertTrue(rows[0]["metadata"].get("orphan"))

    def test_trace_links_every_step_to_its_case(self):
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            log = _Path(tmp) / "trace.jsonl"
            with mock.patch.object(telemetry, "ENABLED", True), \
                 mock.patch.object(telemetry, "LOG", log), \
                 mock.patch.object(telemetry, "_HANDLE", None):

                # Имя своё: счётчик вызовов общий на процесс, чужой шаг сдвинул бы его.
                @telemetry.traced("шаг-в-файл")
                def step():
                    return 1

                with telemetry.Trace("fact-0001") as tr:
                    step()
                    step()
                telemetry.close()
            rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]

        self.assertEqual(len(rows), 2)
        self.assertEqual({r["test_id"] for r in rows}, {"fact-0001"})
        self.assertEqual({r["trace_id"] for r in rows}, {tr.trace_id})
        self.assertEqual(rows[0]["call_count"], 1)
        self.assertEqual(rows[1]["call_count"], 2)
        self.assertTrue(all(r["timestamp"] for r in rows))
        self.assertEqual(sorted(rows[0]),
                         ["call_count", "duration_ms", "function_name", "metadata",
                          "phase", "run_id", "test_id", "timestamp", "trace_id"])


class TestTelemetry(unittest.TestCase):
    """Журнал уезжает наружу вместе с отчётами, личное в нём недопустимо."""

    def setUp(self):
        self.rows = []
        patch = mock.patch.object(telemetry, "ENABLED", True)
        patch.start()
        self.addCleanup(patch.stop)
        emit = telemetry.emit

        def capture(name, ms, meta=None, count=None):
            tr = telemetry.current()
            self.rows.append({"function_name": name, "duration_ms": ms,
                              "trace_id": tr.trace_id if tr else "",
                              "test_id": tr.test_id if tr else "",
                              "metadata": {k: telemetry.scrub(v)
                                           for k, v in (meta or {}).items()}})

        p2 = mock.patch.object(telemetry, "emit", capture)
        p2.start()
        self.addCleanup(p2.stop)

    def test_report_counts_one_run_not_the_whole_file(self):
        """Журнал дописывается: без метки прогона второй показывал бы двойное."""
        rows = [{"function_name": "шаг", "duration_ms": 1.0, "test_id": "a",
                 "run_id": "старый", "metadata": {}},
                {"function_name": "шаг", "duration_ms": 1.0, "test_id": "b",
                 "run_id": "новый", "metadata": {}}]
        only, tests = telemetry.summarize(rows, run_id="новый")
        self.assertEqual(only["шаг"]["calls"], 1)
        self.assertEqual(tests, {"b"})
        self.assertIn("строк: 1", telemetry.report(rows, run_id="новый"))

    def test_trace_exit_without_enter_does_not_crash(self):
        """Замер не имеет права ронять прогон, что бы с ним ни сделали."""
        telemetry.Trace("t").__exit__(None, None, None)
        self.assertIsNone(telemetry.current())

    def test_double_exit_does_not_crash(self):
        tr = telemetry.Trace("t")
        tr.__enter__()
        tr.__exit__(None, None, None)
        tr.__exit__(None, None, None)
        self.assertIsNone(telemetry.current())

    def test_nested_traces_restore_outer(self):
        with telemetry.Trace("внешний") as outer:
            with telemetry.Trace("внутренний"):
                self.assertEqual(telemetry.current().test_id, "внутренний")
            self.assertIs(telemetry.current(), outer)
        self.assertIsNone(telemetry.current())

    def test_metadata_reads_keyword_arguments(self):
        """По позициям вызов по именам давал нули, и отсев в отчёте врал."""
        @telemetry.traced("шаг", lambda arg, o: {"in": len(arg["items"]),
                                                 "порог": arg["min_score"]})
        def step(items, min_score=0.5):
            return items[:1]

        with telemetry.Trace("t"):
            step(items=[1, 2, 3, 4], min_score=0.9)
        meta = self.rows[-1]["metadata"]
        self.assertEqual(meta["in"], 4)
        self.assertEqual(meta["порог"], 0.9)

    def test_metadata_fills_defaults(self):
        @telemetry.traced("шаг", lambda arg, o: {"порог": arg["min_score"]})
        def step(items, min_score=0.5):
            return items

        with telemetry.Trace("t"):
            step([1])
        self.assertEqual(self.rows[-1]["metadata"]["порог"], 0.5)

    def test_phase_separates_two_halves_of_comparison(self):
        """Без метки половины оба прогона сваливаются в один журнал."""
        rows = [{"function_name": "шаг", "duration_ms": 1.0, "test_id": "t",
                 "run_id": "r", "phase": "без памяти", "metadata": {}},
                {"function_name": "шаг", "duration_ms": 3.0, "test_id": "t",
                 "run_id": "r", "phase": "с памятью", "metadata": {}}]
        self.assertEqual(telemetry.phases(rows), ["без памяти", "с памятью"])
        only, _ = telemetry.summarize(rows, phase="с памятью")
        self.assertEqual(only["шаг"]["calls"], 1)
        self.assertEqual(only["шаг"]["ms"], 3.0)

    def test_trace_id_differs_between_cases(self):
        seen = set()
        for case in ("a", "b"):
            with telemetry.Trace(case) as tr:
                seen.add(tr.trace_id)
        self.assertEqual(len(seen), 2)

    def test_metadata_is_scrubbed(self):
        @telemetry.traced("шаг", lambda arg, o: {"путь": arg["path"]})
        def step(path):
            return path

        with telemetry.Trace("t"):
            step("%s/dev/секрет glpat-abcdefghijkl" % os.path.expanduser("~"))
        meta = self.rows[0]["metadata"]["путь"]
        self.assertNotIn(os.path.expanduser("~"), meta)
        self.assertNotIn("glpat-abcdefghijkl", meta)

    def test_failure_is_traced_and_reraised(self):
        @telemetry.traced("шаг")
        def boom():
            raise ValueError("нет")

        with telemetry.Trace("t"), self.assertRaises(ValueError):
            boom()
        self.assertEqual(self.rows[0]["metadata"]["error"], "ValueError")

    def test_disabled_costs_nothing(self):
        """Хук в разговоре не должен платить за замер."""
        with mock.patch.object(telemetry, "ENABLED", False):
            @telemetry.traced("шаг", lambda arg, o: 1 / 0)
            def step():
                return "цело"

            self.assertEqual(step(), "цело")
        self.assertEqual(self.rows, [])

    def test_drop_rate_counted_from_metadata(self):
        rows = [{"function_name": "порог", "duration_ms": 1.0, "test_id": "t",
                 "run_id": "r", "metadata": {"in": 10, "out": 2}}]
        by_fn, tests = telemetry.summarize(rows)
        self.assertEqual(by_fn["порог"]["in"], 10)
        self.assertEqual(by_fn["порог"]["out"], 2)
        self.assertIn("80%", telemetry.report(rows))

    def test_scrub_uses_every_pattern_of_the_write_path(self):
        """Не три примера, а весь список записи: копия уже разъезжалась молча."""
        import encoder
        self.assertIs(telemetry._secrets(), encoder.SECRETS)
        probes = [
            "glpat-abcdefghijkl0000", "ghp_" + "a" * 20, "xmem_" + "b" * 30,
            "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdef",
            "api_key = sk-supersecretvalue",
            "machine git.example login person password hunter2",
            "ssh://person:hunter2@git.example/x",
            "hvs.CAESIJexampleexample",
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        ]
        for probe in probes:
            cleaned = telemetry.scrub(probe)
            self.assertNotEqual(cleaned, probe, "не вычищено: %s" % probe[:40])

    def test_scrub_survives_missing_write_path(self):
        """Журнал не должен падать, если путь записи недоступен."""
        with mock.patch.object(telemetry, "_SECRETS", None), \
             mock.patch.dict("sys.modules", {"encoder": None}):
            self.assertIsInstance(telemetry.scrub("текст"), str)


class TestJudge(unittest.TestCase):
    """Оценка одного случая. Здесь ломались обе главные цифры отчёта."""

    ABSENT = {"id": "absent-1", "kind": "absence", "query": "q",
              "expect": [], "forbid": ["нетакого"]}
    FACT = {"id": "fact-1", "kind": "fact", "query": "q",
            "expect": ["db.py"], "forbid": []}

    def test_crashed_case_is_not_a_pass(self):
        """Сломанный конвейер молчит не хуже исправного — это не успех."""
        self.assertFalse(evaluate.judge(self.ABSENT, "", "", "RuntimeError: нет связи")["ok"])

    def test_silence_passes_only_when_pipeline_ran(self):
        self.assertTrue(evaluate.judge(self.ABSENT, "", "", None)["ok"])

    def test_forbidden_word_fails_absence(self):
        self.assertFalse(evaluate.judge(self.ABSENT, "есть нетакого", "", None)["ok"])

    def test_expected_word_passes(self):
        self.assertTrue(evaluate.judge(self.FACT, "правился db.py", "", None)["ok"])

    def test_missing_word_fails_and_is_named(self):
        got = evaluate.judge(self.FACT, "ничего", "", None)
        self.assertFalse(got["ok"])
        self.assertEqual(got["missed"], ["db.py"])

    def test_answered_but_cut_is_distinguished_from_unknown(self):
        """Память ответила, но порог срезал — это не то же, что «не знала»."""
        got = evaluate.judge(self.FACT, "", "правился db.py", None)
        self.assertFalse(got["ok"])
        self.assertTrue(got["found_in_answer"])

    def test_absence_never_counts_as_answered(self):
        """Иначе колонка потерь уходит в минус."""
        self.assertFalse(evaluate.judge(self.ABSENT, "", "", None)["found_in_answer"])


class TestSummary(unittest.TestCase):
    def rows(self, specs):
        return [{"id": "c%d" % i, "kind": k, "ok": o, "found_in_answer": f,
                 "missed": [], "false_hits": [], "error": e}
                for i, (k, o, f, e) in enumerate(specs)]

    def test_loss_column_never_negative(self):
        out = evaluate.summary(self.rows([("absence", True, False, None),
                                          ("fact", False, True, None)]))
        self.assertNotIn("-", out.split("итог")[0])

    def test_errors_are_reported_separately(self):
        out = evaluate.summary(self.rows([("absence", False, False, "boom")]))
        self.assertIn("упало с ошибкой: 1", out)


class TestMatrix(unittest.TestCase):
    """Сравнение двух половин: без него одно число не значит ничего."""

    def rows(self, phase, oks, kinds=None, knew=None):
        kinds = kinds or ["fact"] * len(oks)
        knew = knew or [False] * len(oks)
        return [{"phase": phase, "id": "c%d" % i, "kind": k, "ok": o,
                 "found_in_answer": m, "missed": [], "false_hits": [],
                 "kept": 0, "chars": 0, "raw_chars": 0,
                 "seconds": 0.0, "error": None}
                for i, (o, k, m) in enumerate(zip(oks, kinds, knew))]

    def test_delta_is_difference_of_two_halves(self):
        base = self.rows(matrix.BASELINE, [False, False, True])
        active = self.rows(matrix.ACTIVE, [True, True, True])
        out = matrix.compare(base, active, 1.0, 2.0)
        self.assertIn("1 из 3 без памяти, 3 из 3 с памятью", out)
        self.assertIn("+2 случая", out)

    def test_loss_counted_only_where_memory_knew(self):
        """Прошедшие на молчании в знаменатель потерь не входят."""
        # Четвёртый случай провален, но память его и не знала. По общему
        # знаменателю потерь вышло бы 3, а конвейер потерял только 2.
        active = self.rows(matrix.ACTIVE, [True, False, False, False],
                           kinds=["absence", "fact", "fact", "fact"],
                           knew=[False, True, True, False])
        out = matrix.compare(self.rows(matrix.BASELINE, [True, False, False, False]),
                             active, 1.0, 2.0)
        self.assertIn("память ответила нужным в 2 случаях, из них срезал порог 2 (100%)", out)
        self.assertNotIn("срезал порог 3", out)

    def test_no_knowledge_says_so_instead_of_dividing(self):
        active = self.rows(matrix.ACTIVE, [False, False])
        out = matrix.compare(self.rows(matrix.BASELINE, [False, False]), active, 0.0, 0.0)
        self.assertIn("терять было нечего", out)

    def test_memory_off_returns_nothing_and_touches_nothing(self):
        """Половина без памяти обязана не ходить в хранилище вовсе."""
        with mock.patch.object(xmem, "DISABLED", True), \
             mock.patch.object(xmem, "_adapter") as adapter:
            self.assertEqual(xmem.read("вопрос"), "")
            self.assertEqual(xmem.write("текст"), "")
            adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
