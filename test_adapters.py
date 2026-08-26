#!/usr/bin/env python3
"""Проверки моделей и адаптеров. Запуск: python3 -m unittest test_adapters -v

Тесты писались после ревью, которое нашло две поломки исполнением, а не чтением:
чтение через адаптер возвращало не строку, и ссылка по ключу требовала полей,
которых у ссылки нет. Обе закрыты здесь.
"""
import json, os, unittest
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

import goldenset
import models
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

    def test_absence_cases_have_forbid(self):
        case = goldenset.case_absent(1, *goldenset.ABSENT[0])
        self.assertEqual(case["expect"], [])
        self.assertTrue(case["forbid"])


if __name__ == "__main__":
    unittest.main()
