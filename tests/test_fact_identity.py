#!/usr/bin/env python3
"""Факт о файле подписан своим файлом, а не проектом.

Запуск: python3 -m unittest tests.test_fact_identity -v

Разборщик подписывал факт о правке именем проекта: `project_state|проект|project`.
Подпись — это первичный ключ строки в хранилище, поэтому «правился файл А» и
«правился файл Б» одного проекта оказывались одной строкой, и вторая правка
затирала первую. На локальном архиве 2026-08-31 мера видит 468 узлов о правке
файла, а подписей у них 45 — по одной на проект: теряется 423 факта из 468.

Проверки заданы свойствами, а не примерами: свойство «сколько разных файлов
встретилось, столько и строк» ловит и схлопывание, и обратную беду —
раздвоение одного файла на две строки. Перечисляя примеры руками, поймаешь
только то, что придумал.

Мутация, на которую проверки обязаны краснеть: вернуть в подпись имя проекта
(`archive/extract.edited_files`, `subject=project`).
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from archive import extract
from domain import models
from pipeline import understand
from storage import local, port

HOME = "/home/person"
BRANCH = "fact-file-key"

# Перебор ходит по диску: срок примера меряет скорость диска, а не наш код.
SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

PROJECTS = ["demo", "other"]

# Имена файлов. Одно и то же имя встречается в разных проектах нарочно: путь
# различает их, а имя — нет.
NAMES = ["db.py", "port.py", "run.py", "README.md"]

# Пути с учётными данными внутри имени. Путь стал подписью, поэтому секрет из
# пути теперь попадает в первичный ключ, а не только в текст.
SECRETS = ["xmem_LiveKeyDoNotStore1234.json",
           "ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123.py",
           "hvs.LiveVaultTokenDoNotStore.txt"]

# Служебные пути: наши же записи разговоров и состояние инструментов. Фактом
# не становятся ни при какой подписи, см. extract.NOT_CODE.
SERVICE = ["%s/.claude/settings.json" % HOME, "%s/.local/state/queue.jsonl" % HOME]

REQUESTS = st.sampled_from([
    "Отвечай кратко, длинные ответы не читаю",
    "Посмотри, что там с базой",
    "Почини порт",
])

REPLIES = st.sampled_from(["Готово.", "Готово. Смотри https://example.org/db"])

ERRORS = st.sampled_from(["", "FileNotFoundError: db.py"])

SESSIONS = st.sampled_from(["разговор-1", "разговор-2", "разговор-3"])

EPISODES = st.builds(
    lambda request, names, service, reply, error: {
        "request": request, "names": names, "service": service,
        "reply": reply, "error": error},
    request=REQUESTS,
    names=st.lists(st.sampled_from(NAMES), max_size=4, unique=True),
    service=st.lists(st.sampled_from(SERVICE), max_size=2, unique=True),
    reply=REPLIES, error=ERRORS)

# Архив: файл на разговор, у разговора свой проект и от одного до трёх эпизодов.
ARCHIVES = st.lists(
    st.tuples(SESSIONS, st.sampled_from(PROJECTS),
              st.lists(EPISODES, min_size=1, max_size=3)),
    min_size=1, max_size=3, unique_by=lambda item: item[0])


def cwd_of(project):
    return "%s/dev/%s" % (HOME, project)


def paths_of(project, spec):
    """Пути эпизода в том порядке, в каком их видит разбор."""
    return ["%s/%s" % (cwd_of(project), name) for name in spec["names"]] + spec["service"]


def wanted(shape):
    """Разные рабочие файлы архива — столько и должно стать фактов о правке."""
    return {path for _, project, specs in shape for spec in specs
            for path in paths_of(project, spec) if path.startswith(cwd_of(project))}


def rows(session, project, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": cwd_of(project),
                "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": spec["request"]}))
        blocks = [{"type": "tool_use", "name": "Edit", "input": {"file_path": target}}
                  for target in paths_of(project, spec)]
        if spec["error"]:
            blocks.append({"type": "tool_result", "is_error": True,
                           "content": spec["error"]})
        blocks.append({"type": "text", "text": spec["reply"]})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape):
    out = []
    for number, (session, project, specs) in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(session, project, specs):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.append(path)
    return out


@contextlib.contextmanager
def store(tmp):
    """Своя база на прогон. Адаптер держит репозиторий на процесс — закрываем."""
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()


def facts_in(base):
    """Строки фактов из базы: подпись -> поля. Читаем то, что легло, а не то,
    что отправили: затирание видно только в хранилище."""
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return {"%s|%s|%s" % (r["fact_type"], r["subject"], r["scope"]): dict(r)
                for r in conn.execute("SELECT * FROM fact")}
    finally:
        conn.close()


def file_facts(base):
    """Только факты о правке файла."""
    return {key: row for key, row in facts_in(base).items()
            if "правился файл" in (row["content"] or "")}


def digest(files, **kwargs):
    kwargs.setdefault("dry", False)
    return understand.digest(files, door=port.door(), **kwargs)


def episode(project, files, request="Посмотри, что там с базой"):
    """Эпизод в том виде, в каком его отдаёт разбор транскрипта."""
    return {"session_id": "разговор-1", "number": 1, "request": request,
            "cwd": cwd_of(project), "branch": BRANCH, "files": list(files),
            "commands": [], "replies": ["Готово."], "errors": [],
            "started_at": "2026-08-28T10:00:00Z", "ended_at": "2026-08-28T11:00:00Z"}


def edits(ep):
    """Факты о правке файла, которые дало правило."""
    return [f for f in extract.facts_of(ep) if "правился файл" in f[3]]


class TestTheSubjectIsTheFile(unittest.TestCase):
    """Подпись факта о правке — путь файла. Проект живёт в своём поле."""

    @SLOW
    @given(project=st.sampled_from(PROJECTS),
           names=st.lists(st.sampled_from(NAMES), min_size=1, max_size=4, unique=True))
    def test_the_subject_is_the_edited_path(self, project, names):
        files = ["%s/%s" % (cwd_of(project), name) for name in names]
        got = [fact[1] for fact in edits(episode(project, files))]
        self.assertEqual(got, files)

    @SLOW
    @given(project=st.sampled_from(PROJECTS),
           names=st.lists(st.sampled_from(NAMES), min_size=2, max_size=4, unique=True))
    def test_different_files_never_share_a_signature(self, project, names):
        """Столько разных файлов — столько разных подписей. Это и есть ключ."""
        files = ["%s/%s" % (cwd_of(project), name) for name in names]
        found = edits(episode(project, files))
        signatures = {models.Fact(*fact).identity() for fact in found}
        self.assertEqual(len(signatures), len(files))

    @SLOW
    @given(project=st.sampled_from(PROJECTS), name=st.sampled_from(NAMES),
           first=REQUESTS, second=REQUESTS)
    def test_one_file_keeps_one_signature_across_episodes(self, project, name,
                                                          first, second):
        """Тот же файл в другом эпизоде — тот же факт, а не второй.

        Обратная сторона того же свойства: подпись обязана различать файлы и
        обязана не различать задачи, ради которых файл правили. Иначе каждая
        правка заводила бы новую строку и мера подтверждений обнулялась бы.
        """
        path = "%s/%s" % (cwd_of(project), name)
        one = edits(episode(project, [path], request=first))[0]
        two = edits(episode(project, [path], request=second))[0]
        self.assertEqual(models.Fact(*one).identity(), models.Fact(*two).identity())

    @SLOW
    @given(project=st.sampled_from(PROJECTS), service=st.sampled_from(SERVICE),
           name=st.sampled_from(NAMES))
    def test_a_service_path_still_makes_no_fact(self, project, service, name):
        """Наши записи разговоров и состояние — не знание о проекте."""
        files = [service, "%s/%s" % (cwd_of(project), name)]
        self.assertEqual([fact[1] for fact in edits(episode(project, files))],
                         [files[1]])

    @SLOW
    @given(project=st.sampled_from(PROJECTS), name=st.sampled_from(NAMES))
    def test_the_project_is_kept_in_its_own_field(self, project, name):
        """Проект из подписи ушёл, но не пропал: он поле записи и слово текста.

        Поиск взвешивает `project` наравне с темой, и вопрос «какие файлы
        правились в проекте X» обязан находить строку по-прежнему.
        """
        path = "%s/%s" % (cwd_of(project), name)
        ep = episode(project, [path])
        fact = edits(ep)[0]
        record = understand.fact_of(ep, fact)
        self.assertEqual(record.project, project)
        self.assertEqual(record.subject, path)
        self.assertIn("В проекте %s" % project, record.content)


class TestNothingIsOverwrittenInTheStore(unittest.TestCase):
    """То же свойство на живой базе: строк столько, сколько разных файлов."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_one_row_per_distinct_file(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            got = {row["subject"] for row in file_facts(base).values()}
            self.assertEqual(got, wanted(shape))

    @SLOW
    @given(shape=ARCHIVES)
    def test_every_row_is_read_back_by_its_own_key(self, shape):
        """Каждый файл читается по своей подписи, и в строке — он сам."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            rows_by_key = file_facts(base)
            for path in wanted(shape):
                key = "project_state|%s|project" % path
                self.assertIn(key, rows_by_key, "факт про %s не найден" % path)
                self.assertIn("правился файл %s ради" % path,
                              rows_by_key[key]["content"])

    def test_a_second_file_does_not_evict_the_first(self):
        """Ожидаемый сигнал задачи: два файла одного проекта лежат разом."""
        shape = [("разговор-1", "demo", [{"request": "Посмотри, что там с базой",
                                          "names": ["db.py", "port.py"],
                                          "service": [], "reply": "Готово.",
                                          "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = digest(archive(tmp, shape))
            rows_by_key = file_facts(base)
            self.assertEqual(len(rows_by_key), 2,
                             "два файла одного проекта дали %d строк"
                             % len(rows_by_key))
            self.assertEqual(got["facts"], 2)
            for name in ("db.py", "port.py"):
                key = "project_state|%s/%s|project" % (cwd_of("demo"), name)
                self.assertIn(key, rows_by_key)
                self.assertIn(name, rows_by_key[key]["content"])

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_file_of_one_project_is_not_touched_by_another(self, shape):
        """Одно имя файла в двух проектах — две строки: путь у них разный."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            for key, row in file_facts(base).items():
                self.assertTrue(row["subject"].startswith("%s/dev/" % HOME))
                self.assertEqual(Path(row["subject"]).parent.name, row["project"])

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_second_pass_adds_no_row(self, shape):
        """Повтор по тому же архиву не плодит строк: подпись устойчива."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            was = file_facts(base)
            digest(files, reset=True)
            self.assertEqual(set(file_facts(base)), set(was))


class TestTheMeasureAndTheStoreCountTheSame(unittest.TestCase):
    """Мера считает узлы, хранилище хранит строки. Расходиться им нельзя.

    Расхождение и было болезнью: мера видела 468 узлов, в хранилище ложилось
    45 строк. Цифра прохода при этом сходилась, потому что считала отправленное,
    а не легшее.
    """

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_node_of_the_measure_is_a_row_of_the_store(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            nodes = {key for key, rec in understand.weigh(files).items()
                     if key[0] == "file"}
            self.assertEqual(len(nodes), len(file_facts(base)))

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_measure_and_the_store_name_the_same_files(self, shape):
        """Мало совпасть числом: узел и строка должны звать файл одним именем."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            nodes = {key[1] for key in understand.weigh(files) if key[0] == "file"}
            self.assertEqual(nodes,
                             {row["subject"] for row in file_facts(base).values()})

    @SLOW
    @given(project=st.sampled_from(PROJECTS), name=st.sampled_from(SECRETS))
    def test_a_secret_in_the_path_is_cleaned_on_both_sides(self, project, name):
        """Секрет в пути чистится и в мере, и в записи — одинаково.

        Запись вычищает ключ на выходе сама. Не вычисти мера тот же путь,
        узел и строка разошлись бы именами: мера считала бы подтверждения
        файлу, которого в хранилище нет.
        """
        shape = [("разговор-1", project, [{"request": "Почини порт",
                                           "names": [name], "service": [],
                                           "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            digest(files)
            rows_by_key = file_facts(base)
            self.assertEqual(len(rows_by_key), 1)
            nodes = {key[1] for key in understand.weigh(files) if key[0] == "file"}
            self.assertEqual(nodes,
                             {row["subject"] for row in rows_by_key.values()})
            secret = name.split(".")[0]
            for row in rows_by_key.values():
                self.assertNotIn(secret, row["subject"])
                self.assertNotIn(secret, row["content"])

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_pass_counts_what_the_store_keeps(self, shape):
        """Счётчик прохода не должен обещать больше, чем осталось в базе."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = digest(archive(tmp, shape))
            self.assertGreaterEqual(got["facts"], len(file_facts(base)))
            self.assertEqual(len(file_facts(base)), len(wanted(shape)))


class TestOtherFactsKeepTheirSubject(unittest.TestCase):
    """Правку подписывает файл. Остальные виды фактов задача не трогает."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_preference_is_still_signed_by_its_topic(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            topics = {topic for topic, _ in extract.PREF_TOPICS}
            for key, row in facts_in(base).items():
                if row["fact_type"] != "preference":
                    continue
                self.assertIn(row["subject"], topics)
                self.assertEqual(row["scope"], "global")
                self.assertIn(row["project"], (None, ""))

    @SLOW
    @given(shape=ARCHIVES)
    def test_an_address_is_still_signed_by_its_project(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            for key, row in facts_in(base).items():
                if row["fact_type"] == "external_resource":
                    self.assertIn(row["subject"], PROJECTS)


class TestTheGoldenSetStillAsksAboutTheProject(unittest.TestCase):
    """Набор эталонов спрашивал про проект, беря его из подписи факта.

    Подпись стала путём файла, и вопрос превратился бы в «какие файлы
    правились в проекте /home/person/dev/demo/db.py». Случай остался бы
    формально валидным и молча непроходимым — такую поломку видно только
    свойством, потому что она не падает, а искажает смысл.
    """

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_question_names_a_project_and_not_a_path(self, shape):
        from eval import goldenset
        with tempfile.TemporaryDirectory() as tmp:
            occ, _ = goldenset.collect(archive(tmp, shape))
            marked = goldenset.label(occ, "2099-01-01")
            asked = 0
            for key, rec in marked.items():
                if rec["items"][-1]["fact"][0] != "project_state":
                    continue
                case = goldenset.case_fact(key, rec)
                if case is None:
                    continue
                asked += 1
                self.assertIn("в проекте %s?" % rec["items"][-1]["project"],
                              case["query"])
                self.assertNotIn("/", case["query"].split("в проекте ")[-1])
            if wanted(shape):
                self.assertGreater(asked, 0, "ни одного случая про файлы")


class TestEveryFileKeepsItsLink(unittest.TestCase):
    """Связь эпизод — факт ставится на подпись факта. Схлопывание рвало её."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_link_per_file_of_the_episode(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            digest(archive(tmp, shape))
            conn = sqlite3.connect(str(base))
            try:
                ends = {}
                for link_id, role, key in conn.execute(
                        "SELECT link_id, role, object_key FROM links "
                        "WHERE relation = 'episode_facts'"):
                    ends.setdefault(link_id, {})[role] = json.loads(key)
            finally:
                conn.close()
            linked = {json.dumps(e["fact"], ensure_ascii=False, sort_keys=True)
                      for e in ends.values() if "fact" in e}
            files = {json.dumps({"fact_type": "project_state", "subject": path,
                                 "scope": "project"},
                                ensure_ascii=False, sort_keys=True)
                     for path in wanted(shape)}
            self.assertTrue(files <= linked,
                            "у файлов нет связи с эпизодом: %s"
                            % sorted(files - linked)[:2])


if __name__ == "__main__":
    unittest.main()
