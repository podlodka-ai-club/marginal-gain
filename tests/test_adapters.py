#!/usr/bin/env python3
"""Проверки моделей и адаптеров. Запуск: python3 -m unittest tests.test_adapters -v

Тесты писались после ревью, которое нашло две поломки исполнением, а не чтением:
чтение через адаптер возвращало не строку, и ссылка по ключу требовала полей,
которых у ссылки нет. Обе закрыты здесь.
"""
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import evaluate
from eval import goldenset
from domain import models
from infra import telemetry
from pipeline import suggest
from storage import db
from storage import port
from storage import api
from storage import local

HERE = Path(__file__).resolve().parent.parent


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
    def test_console_door_cannot_be_asked_for_structured_write(self):
        """Не «падает при вызове», а метода нет: вызвать нечего."""
        self.assertFalse(hasattr(port.door("cli"), "write_objects"))

    def test_unknown_backend_refused(self):
        with self.assertRaises(port.BackendError):
            port.door("лишний")

    def test_read_mode_translated_to_service_name(self):
        stub = mock.Mock(read=mock.Mock(return_value=""))
        port.StructuredDoor(stub, "api").read("вопрос", mode="raw")
        self.assertEqual(stub.read.call_args.kwargs["mode"], "raw-tables")


class TestHttpAdapter(unittest.TestCase):
    def test_read_returns_text_not_dict(self):
        """Вызывающий разбирает ответ строкой — адаптер обязан её отдать."""
        with mock.patch.object(api, "_call",
                               return_value={"reader_result": {"rows": [[4]]}}):
            self.assertIsInstance(api.read("сколько"), str)

    def test_read_sends_service_field_name(self):
        """Сервис называет поле mode; read_mode он молча игнорирует."""
        with mock.patch.object(api, "_call", return_value={}) as call:
            api.read("вопрос", mode="raw-tables")
        self.assertEqual(call.call_args.args[1], {"query": "вопрос", "mode": "raw-tables"})

    def test_missing_answer_is_empty_text(self):
        with mock.patch.object(api, "_call", return_value={}):
            self.assertEqual(api.read("вопрос"), "")

    def test_empty_batch_refused(self):
        with self.assertRaises(api.ApiError):
            api.write_objects([])

    def test_empty_env_falls_back_to_default_address(self):
        """Пустая переменная в окружении не должна давать относительный адрес."""
        self.assertTrue(api.BASE.startswith("http"))


class TestSuggestSurvivesAdapterAnswer(unittest.TestCase):
    """Ревью нашло это исполнением: dict вместо строки убивал подсказку молча."""

    def test_pieces_parses_adapter_answer(self):
        answer = json.dumps(["Оценка уверенности: 0.90. Факт про модуль оценки."],
                            ensure_ascii=False)
        got = suggest.pieces(answer)
        self.assertTrue(got)
        self.assertGreaterEqual(got[0][0], 0.9)


class TestThreshold(unittest.TestCase):
    """Порог на настоящих ответах хранилища, снятых прогоном 26.08.

    Красный до правки: порог требовал маркер «Оценка уверенности», который
    дописывает только `understand.render_fact`. В хранилище его ноль вхождений,
    поэтому порог отдавал пустоту при любом содержимом базы.
    """

    FACTS = json.dumps({"answer": json.dumps([
        {"content": "Список компаний, которые нанимают без whiteboard-собеседований: "
                    "https://github.com/poteto/hiring-without-whiteboards .",
         "fact_type": "external_resource", "project": "job-hunt", "scope": "global"},
        {"content": "Вакансии тянутся через api.hh.ru .",
         "fact_type": "external_resource", "project": "job-hunt", "scope": "global"},
    ], ensure_ascii=False)}, ensure_ascii=False)

    RECORD = json.dumps({"answer": str(
        {"content": "Отвечать коротко.", "fact_type": "preference",
         "scope": "global", "subject": "стиль ответа"})}, ensure_ascii=False)

    SILENT = json.dumps({"answer": "no matching files"}, ensure_ascii=False)

    def test_list_of_facts_splits_into_pieces(self):
        """Вложенная строка разбирается: два факта, а не один слипшийся кусок."""
        got = suggest.pieces(self.FACTS)
        self.assertEqual(len(got), 2)

    def test_record_in_python_repr_is_parsed(self):
        """Одиночная запись приходит с одинарными кавычками, json её не берёт."""
        got = suggest.pieces(self.RECORD)
        self.assertEqual(len(got), 1)
        self.assertIn("Отвечать коротко", got[0][1])

    def test_unscored_record_passes_threshold(self):
        """Запись без маркера доходит до агента: оценки нет, а факт есть."""
        kept = suggest.gate(suggest.pieces(self.FACTS))
        self.assertEqual(len(kept), 2)
        self.assertIn("github.com", suggest.render(kept))

    def test_reader_prose_stays_silent(self):
        """«no matching files» это слова читателя, а не факт. Молчим."""
        self.assertEqual(suggest.gate(suggest.pieces(self.SILENT)), [])

    def test_prose_inside_list_stays_silent(self):
        """Та же проза, обёрнутая в список, остаётся прозой.

        Ревью нашло исполнением: признак «структурная запись» стоял на
        контейнере, и список голых строк пропускал «no matching files» в
        контекст агента как запомненный факт.
        """
        answer = json.dumps({"answer": json.dumps(["no matching files"])})
        self.assertEqual(suggest.gate(suggest.pieces(answer)), [])

    def test_fields_beyond_content_survive(self):
        """Запись без `content` не должна схлопываться: ветка лежит в поле."""
        answer = json.dumps({"answer": json.dumps([{"git_branch": "HEAD",
                                                    "project": "job-hunt"}])})
        self.assertIn("HEAD", suggest.render(suggest.gate(suggest.pieces(answer))))

    def test_scored_outrank_unscored(self):
        """Оценённое идёт первым: известная уверенность сильнее её отсутствия."""
        answer = json.dumps({"answer": json.dumps([
            {"content": "без оценки"},
            {"content": "с оценкой. Оценка уверенности: 0.90."},
        ], ensure_ascii=False)}, ensure_ascii=False)
        kept = suggest.gate(suggest.pieces(answer))
        self.assertEqual([s for s, _, _ in kept], [0.9, None])

    def test_one_long_piece_does_not_starve_the_rest(self):
        """Длинный кусок пропускаем, а не обрываем на нём всю выдачу.

        На настоящей базе первым приходило событие на тысячи символов, и порог
        отдавал пустоту, имея за спиной пятьдесят пять тысяч символов найденного.
        """
        answer = json.dumps({"answer": json.dumps([
            {"content": "х" * 5000},
            {"content": "короткий нужный факт"},
        ], ensure_ascii=False)}, ensure_ascii=False)
        self.assertIn("короткий нужный факт",
                      suggest.render(suggest.gate(suggest.pieces(answer))))

    def test_unscored_line_has_no_confidence_tail(self):
        """Не приписываем уверенность там, где её не измеряли."""
        self.assertNotIn("уверенность", suggest.render([(None, "факт", None)]))


class TestLocalStore(unittest.TestCase):
    """Локальная база. Замена сети должна быть незаметна вызывающему."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.repo = db.Repository(Path(self.dir.name) / "memory.db")
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.repo.close)

    def test_tables_follow_models_without_drift(self):
        """Колонки выводятся из схемы. Ручной список разошёлся бы молча."""
        for name, cls in models.OBJECTS.items():
            got = {row[1] for row in
                   self.repo.conn.execute('PRAGMA table_info("%s")' % name.lower())}
            self.assertEqual(got, {f.name for f in dataclasses.fields(cls)}, name)

    def test_migration_is_idempotent(self):
        """Повторный запуск не должен ни падать, ни накатывать заново."""
        self.assertEqual(db.migrate(self.repo.conn), 0)
        self.assertEqual(
            self.repo.conn.execute("PRAGMA user_version").fetchone()[0],
            len(db.MIGRATIONS))

    def test_second_write_updates_row_by_key(self):
        """Первичный ключ схемы — тот же, что в XMD: строка одна, не две."""
        first = models.Fact(fact_type="preference", subject="стиль", scope="global",
                            content="Отвечать коротко")
        self.repo.apply([first.mutation()])
        self.repo.apply([models.Fact(fact_type="preference", subject="стиль",
                                     scope="global", content="Отвечать очень коротко"
                                     ).mutation()])
        rows = self.repo.conn.execute("SELECT content FROM fact").fetchall()
        self.assertEqual([r[0] for r in rows], ["Отвечать очень коротко"])

    def test_partial_write_keeps_what_is_already_there(self):
        """Пустое поле не шлётся и потому не затирает лежащее значение."""
        self.repo.apply([models.Episode(session_id="s1", episode_number=1,
                                        title="Правка", outcome="done",
                                        project="marginal-gain").mutation()])
        self.repo.apply([models.Episode(session_id="s1", episode_number=1,
                                        title="Правка", outcome="done").mutation()])
        row = self.repo.conn.execute("SELECT project FROM episode").fetchone()
        self.assertEqual(row[0], "marginal-gain")

    def test_empty_string_does_not_blank_a_stored_value(self):
        """Пустая строка это тоже пустое поле. Ревью нашло исполнением.

        Вычистка секретов умеет свести короткое содержимое к пустой строке.
        Раньше такая запись доезжала до базы и стирала лежащий там факт.
        """
        self.repo.apply([models.Episode(session_id="s1", episode_number=1,
                                        title="Правка", outcome="done",
                                        project="marginal-gain").mutation()])
        self.repo.apply([models.Episode(session_id="s1", episode_number=1,
                                        title="Правка", outcome="done",
                                        project="").mutation()])
        row = self.repo.conn.execute("SELECT project FROM episode").fetchone()
        self.assertEqual(row[0], "marginal-gain")

    def test_repository_answers_from_another_thread(self):
        """Адаптер держит один репозиторий на процесс и ходит в него из потоков."""
        import threading
        out = []
        worker = threading.Thread(target=lambda: out.append(self.repo.counts()))
        worker.start()
        worker.join()
        self.assertEqual(len(out), 1)

    def test_relation_endpoints_are_stored(self):
        ep = models.Episode(session_id="s1", episode_number=1, title="t", outcome="done")
        fact = models.Fact(fact_type="project_state", subject="p", scope="project",
                           content="c")
        self.repo.apply([ep.mutation(), fact.mutation(),
                         models.link("episode_facts", episode=ep, fact=fact)])
        roles = {r[0] for r in
                 self.repo.conn.execute("SELECT role FROM links WHERE relation = ?",
                                        ("episode_facts",))}
        self.assertEqual(roles, {"episode", "fact"})

    def test_search_finds_written_fact(self):
        self.repo.apply([models.Fact(
            fact_type="project_state", subject="marginal-gain", scope="project",
            content="Правился файл suggest.py ради порога.").mutation()])
        got = self.repo.search("Какие файлы правились в проекте marginal-gain?")
        self.assertTrue(got)
        self.assertEqual(got[0]["object_type"], "Fact")

    def test_fact_outranks_conversation_metadata(self):
        """Факт это знание, строка разговора — метаданные. Порядок не случаен."""
        self.repo.apply([models.Fact(
            fact_type="project_state", subject="job-hunt", scope="project",
            content="В проекте job-hunt правился файл db.py").mutation()])
        self.repo.apply([models.Session(session_id="s1", project="job-hunt",
                                        working_directory="/dev/job-hunt",
                                        git_branch="job-hunt").mutation()])
        got = self.repo.search("файлы job-hunt")
        self.assertEqual(got[0]["object_type"], "Fact")

    def test_search_stays_silent_without_a_hit(self):
        """Случайная строка хуже пустоты: она поедет в контекст как факт."""
        self.assertEqual(self.repo.search("несуществующая ерунда zzz"), [])


class TestLocalAdapter(unittest.TestCase):
    """Порт локальной базы должен совпадать с сетевым до имён."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.dict(
            os.environ, {"XMEM_LOCAL_PATH": str(Path(self.dir.name) / "memory.db")})
        patch.start()
        self.addCleanup(patch.stop)
        local.close()
        self.addCleanup(local.close)

    def test_port_matches_the_network_adapter(self):
        """Меняться местами можно только при одинаковых именах."""
        for name in ("write_text", "write_objects", "read", "schema"):
            self.assertTrue(callable(getattr(local, name)), name)

    def test_backend_switch_picks_local(self):
        self.assertIs(port.door("local").adapter, local)

    def test_our_own_text_becomes_a_record(self):
        """Текстовый путь записи не теряется: формат наш, разбор детерминирован."""
        got = local.write_text("Fact.\ncontent: Отвечать коротко\n"
                                    "fact_type: preference\nsubject: стиль\n"
                                    "scope: global\nОценка уверенности: 0.90.")
        self.assertEqual(got["stored"], "Fact")
        self.assertEqual(local.repository().counts()["Fact"], 1)

    def test_multiline_field_survives(self):
        """Значение в несколько строк собирается целиком, а не по первой строке."""
        got = local.parse_text("Fact.\ncontent: первая строка\n"
                                    "вторая строка того же поля\n"
                                    "fact_type: preference\nsubject: стиль\n"
                                    "scope: global")
        self.assertIn("вторая строка", got[1]["content"])

    def test_human_note_is_not_glued_to_a_field(self):
        """«Оценка уверенности» — пояснение человеку, а не продолжение поля."""
        got = local.parse_text("Fact.\ncontent: текст\nfact_type: preference\n"
                                    "subject: стиль\nscope: global\n"
                                    "Оценка уверенности: 0.90.")
        self.assertEqual(got[1]["scope"], "global")

    def test_foreign_text_is_kept_not_dropped(self):
        """Экстрактора нет, но терять вход нельзя: потерю надо видеть."""
        got = local.write_text("Разговор abc, проект x, ветка y. user:\nпривет")
        self.assertEqual(got["stored"], "raw")
        self.assertEqual(local.repository().counts()["raw_text"], 1)

    def test_read_returns_text_not_dict(self):
        """Тот же контракт, что у сетевого адаптера: вызывающий ждёт строку."""
        self.assertIsInstance(local.read("что угодно"), str)

    def test_empty_result_is_silence(self):
        """Не «ничего не найдено» словами: фраза уехала бы в контекст как факт."""
        self.assertEqual(local.read("несуществующая ерунда zzz"), "")

    def test_suggestion_survives_the_local_answer(self):
        """Сквозь: запись, чтение, порог, текст для агента."""
        fact = models.Fact(fact_type="project_state", subject="marginal-gain",
                           scope="project",
                           content="Правился файл suggest.py ради порога.")
        local.write_objects([fact.mutation()])
        text, kept, raw = suggest.suggest("файлы marginal-gain",
                                          door=port.door("local"))
        self.assertIn("suggest.py", text)


class TestSearchTreatsUnderscoreAsALetter(unittest.TestCase):
    """Подчёркивание в запросе — буква, а не подстановочный знак.

    В LIKE `_` совпадает с любым одиночным символом, а WORD пропускает его в
    слова: запрос про on_prompt.py тянул из базы заодно onXprompt.py.

    На выдачу это не влияло — счёт очков в Python сверяет подстроку честно и
    лишнее отбрасывает. Вредило это отбору кандидатов: их берётся не больше
    CANDIDATES, и мусор вытеснял из этого числа настоящие совпадения.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = db.Repository(Path(self.dir.name) / "m.db")
        self.addCleanup(self.repo.close)

    def put(self, subject, content):
        self.repo.apply([models.Fact(fact_type="project_state", subject=subject,
                                     scope="project", content=content).mutation()])

    def test_like_escapes_the_signs_of_the_pattern(self):
        """Самый прямой уровень: во что превращается слово вопроса."""
        self.assertEqual(db.like("on_prompt.py"), "%on\\_prompt.py%")
        self.assertEqual(db.like("100%"), "%100\\%%")
        self.assertEqual(db.like("a\\b"), "%a\\\\b%")

    def test_decoys_do_not_crowd_out_the_real_match(self):
        """Потолок кандидатов занижаем: иначе для проверки нужны сотни строк."""
        # Каждая ловушка обязана подходить под образец on_prompt.py именно
        # через подстановку: одна любая буква на месте подчёркивания.
        for letter in "ABCDEFGH":
            self.put("шум%s" % letter,
                     "В проекте demo правился файл on%sprompt.py" % letter)
        self.put("своё", "В проекте demo правился файл on_prompt.py")
        with mock.patch.object(db, "CANDIDATES", 4):
            got = [r["content"] for r in self.repo.search("on_prompt.py", limit=10)]
        self.assertTrue(any("on_prompt.py" in c for c in got),
                        "настоящее совпадение вытеснено подставными: %s" % got)

    def test_results_stay_clean_of_wildcard_matches(self):
        """Сторожевой: выдача и раньше была чистой, пусть такой и остаётся."""
        self.put("своё", "В проекте demo правился файл on_prompt.py")
        self.put("чужое", "В проекте demo правился файл onXprompt.py")
        got = [r["content"] for r in self.repo.search("on_prompt.py", limit=10)]
        self.assertFalse(any("onXprompt.py" in c for c in got))


class TestFixtureCanAnswerEveryCase(unittest.TestCase):
    """Набор, наполненный своей же фикстурой, обязан быть проходимым.

    fixture() отдавала только Fact, а десять случаев из ста спрашивают ветку —
    она живёт на Episode. Прогон с пустым хранилищем упирался в 89 из 100 не
    из-за памяти, а по построению, и разница половин мерялась об этот потолок.
    """

    EPISODES = {
        ("s-1", 1): {"session_id": "s-1", "number": 1, "cwd": "/home/p/dev/demo",
                     "branch": "feature-x", "started_at": "2026-08-01T10:00:00Z",
                     "ended_at": "2026-08-01T10:30:00Z", "request": "правь alpha.py",
                     "files": ["/home/p/dev/demo/alpha.py"], "commands": [],
                     "replies": [], "errors": []},
    }

    def test_context_case_finds_its_branch_in_the_fixture(self):
        case = goldenset.case_context(("s-1", 1), self.EPISODES[("s-1", 1)])
        self.assertIsNotNone(case, "случай про ветку вообще не собрался")
        rows = goldenset.fixture({}, self.EPISODES, [case])
        blob = json.dumps(rows, ensure_ascii=False)
        for want in case["expect"]:
            self.assertIn(want, blob, "ожидаемое из случая в фикстуру не попало")

    def test_shipped_set_is_answerable_from_its_shipped_fixture(self):
        """Проверяем то, что реально уехало в репозиторий, а не выдумку."""
        _, cases = goldenset.load(HERE / "eval-cases.json", "cases")
        _, rows = goldenset.load(HERE / "eval-fixture.json", "fixture")
        blob = json.dumps(rows, ensure_ascii=False)
        blind = [c["id"] for c in cases
                 if c["kind"] != "absence" and c.get("expect")
                 and not any(want in blob for want in c["expect"])]
        self.assertEqual(blind, [], "случаи, которые нечем удовлетворить")


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
        _, turns = goldenset.load("eval-script.json", "script")
        self.assertTrue(turns)
        self.assertTrue(all(t["feeds"] for t in turns))

    def test_script_is_ordered_by_time(self):
        """Иначе разговор про файл случается раньше, чем файл в нём появился."""
        _, turns = goldenset.load("eval-script.json", "script")
        stamps = [t["started_at"] for t in turns]
        self.assertEqual(stamps, sorted(stamps))

    def test_every_case_is_reachable_from_script(self):
        _, cases = goldenset.load("eval-cases.json", "cases")
        _, turns = goldenset.load("eval-script.json", "script")
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
        """Не три примера, а весь список scrub: копия уже разъезжалась молча."""
        from infra import scrub
        self.assertIs(telemetry._secrets(), scrub.SECRETS)
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

    def test_scrub_covers_personal_on_top_of_secrets(self):
        """Свой список личного журнал чистит сверх общих шаблонов scrub."""
        from infra import scrub
        probe = "person@example.com 192.168.1.7"
        self.assertEqual(scrub.redact(probe), probe, "это забота журнала, не scrub")
        cleaned = telemetry.scrub(probe)
        self.assertNotIn("person@example.com", cleaned)
        self.assertNotIn("192.168.1.7", cleaned)


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


class TestMemoryOffIsReallyOff(unittest.TestCase):
    """Выключенная память не ходит в хранилище вовсе.

    Не «не позвала адаптер», а звать нечем: у выключенной двери адаптера нет.
    """

    def test_memory_off_returns_nothing_and_touches_nothing(self):
        off = port.door(disabled=True)
        self.assertEqual(off.read("вопрос"), "")
        self.assertEqual(off.write("текст"), "")
        self.assertIsNone(off.write_objects([models.Session(session_id="s")]))
        # Не «не позвал адаптер», а держать нечего: адаптера у неё нет вовсе.
        self.assertFalse(hasattr(off, "adapter"))


if __name__ == "__main__":
    unittest.main()
