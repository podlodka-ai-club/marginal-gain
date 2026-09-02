#!/usr/bin/env python3
"""Слои проекта: кто кому имеет право звонить. Запуск: python3 -m unittest tests.test_layers -v

Проверки здесь не про поведение функций, а про направление зависимостей.
Обычный тест такое не ловит: кольцо из отложенных импортов работает ровно до
того дня, когда импорт переносят наверх, и падает сразу весь проект.

Импорт проверяем в отдельном процессе. В общем `sys.modules` половина проекта
уже загружена соседними тестами, и любая проверка «что подтянулось» врёт.
"""
import ast, os, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import save
from storage import port


def imported_by(module):
    """Что оказалось в sys.modules после импорта module. Свежий процесс."""
    code = ("import sys; sys.path.insert(0, %r); import %s; "
            "print('\\n'.join(sorted(sys.modules)))" % (str(ROOT), module))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(ROOT))
    if out.returncode != 0:
        raise AssertionError("импорт %s упал: %s" % (module, out.stderr[-600:]))
    return set(out.stdout.split())


def ours_reached_by(module):
    """Только наши модули, до которых дотянулся импорт. Пакеты-обёртки отбрасываем."""
    return imported_by(module) & ours()


# Слои проекта снизу вверх. Зависеть можно только вниз по этому списку.
LAYERS = ["infra", "domain", "archive", "storage", "pipeline", "eval"]

# Модули проекта, а не всё подряд из стандартной библиотеки.
def ours():
    return {"%s.%s" % (p.parent.name, p.stem)
            for layer in LAYERS for p in (ROOT / layer).glob("*.py")
            if p.stem != "__init__"}


def sources():
    """Все боевые файлы проекта. Тесты и стенд сюда не входят."""
    return sorted(p for layer in LAYERS for p in (ROOT / layer).glob("*.py"))


def layer_of(module):
    return module.split(".", 1)[0]


class TestLayersPointOneWay(unittest.TestCase):
    """Слой зависит только от того, что ниже. Это и есть вся структура.

    Порядок снизу вверх: infra, domain, archive, storage, pipeline, eval.
    Плоский корень на девятнадцать модулей эту стрелку не выражал никак —
    любой мог позвать любого, и цикл через отложенный импорт держался годами
    незамеченным.
    """

    def test_no_layer_reaches_upward(self):
        offenders = []
        for path in sources():
            mine = LAYERS.index(path.parent.name)
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    head = node.module.split(".")[0]
                elif isinstance(node, ast.Import):
                    head = node.names[0].name.split(".")[0]
                else:
                    continue
                if head not in LAYERS:
                    continue
                if LAYERS.index(head) > mine:
                    offenders.append("%s/%s → %s" % (path.parent.name, path.name, head))
        self.assertEqual(sorted(set(offenders)), [])

    def test_nothing_of_ours_left_lying_in_the_root(self):
        """Корень держит только точки входа и настройку, не модули."""
        stray = [p.name for p in ROOT.glob("*.py")]
        self.assertEqual(stray, [])

    def test_no_import_cycle_anywhere(self):
        """Ни одного кольца во всём графе, считая отложенные импорты.

        Проверка не по парам, а по всему графу: кольцо
        models → encoder → xmem → telemetry → encoder жило годами именно
        потому, что каждое ребро по отдельности выглядело безобидно.

        Отложенные импорты считаются рёбрами наравне с верхними: кольцо на
        них не исчезает, а только перестаёт падать при запуске.
        """
        mods = {"%s.%s" % (p.parent.name, p.stem): p for p in sources()}

        def deps(name, path):
            found = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module in mods:
                        found.add(node.module)
                    elif node.module in LAYERS:
                        found |= {"%s.%s" % (node.module, a.name) for a in node.names
                                  if "%s.%s" % (node.module, a.name) in mods}
                elif isinstance(node, ast.Import):
                    found |= {a.name for a in node.names if a.name in mods}
            return found - {name}

        edges = {n: deps(n, p) for n, p in mods.items()}
        rings, stack, seen = set(), [], set()

        def walk(node):
            if node in stack:
                rings.add(" → ".join(stack[stack.index(node):] + [node]))
                return
            if node in seen:
                return
            seen.add(node)
            stack.append(node)
            for nxt in sorted(edges[node]):
                walk(nxt)
            stack.pop()

        for node in sorted(edges):
            seen.clear()
            walk(node)
        self.assertEqual(sorted(rings), [])

    def test_every_layer_says_what_it_is_for(self):
        for layer in LAYERS:
            doc = (ROOT / layer / "__init__.py").read_text().strip()
            self.assertTrue(doc.startswith('"""'), layer)


class TestScrubIsTheBottomLayer(unittest.TestCase):
    """Вычистка секретов — нижний слой: её тянут все, она не тянет никого."""

    def test_scrub_pulls_nothing_of_ours(self):
        self.assertEqual(ours_reached_by("infra.scrub"), {"infra.scrub"})

    def test_scrub_redacts(self):
        from infra import scrub
        self.assertNotIn("hunter2", scrub.redact("api_key = hunter2"))
        self.assertTrue(scrub.SECRETS)


class TestNoSilentDoubles(unittest.TestCase):
    """Двух определений одного имени в модуле быть не должно.

    Второе молча выигрывает, и правка, попавшая в первое, не действует ни на
    что. Ровно этот класс тихого расхождения scrub и заведён предотвращать —
    и ровно на нём он сам и попался при переносе из encoder.py.
    """

    def test_no_top_level_name_defined_twice(self):
        offenders = []
        for path in sources():
            names = []
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    names.append(node.name)
            for name in sorted(set(names)):
                if names.count(name) > 1:
                    offenders.append("%s: %s ×%d" % (path.name, name, names.count(name)))
        self.assertEqual(offenders, [])


class TestNoImportRing(unittest.TestCase):
    """Кольца нет ни одного. Держалось на двух отложенных импортах."""

    def test_models_does_not_reach_the_storage_door(self):
        """models → encoder → xmem: схема записи не должна знать про хранилище."""
        self.assertNotIn("storage.port", ours_reached_by("domain.models"))

    def test_telemetry_does_not_reach_the_storage_door(self):
        """Журнал берёт шаблоны у scrub, а не у пути записи через кольцо."""
        reached = ours_reached_by("infra.telemetry")
        self.assertNotIn("storage.port", reached)
        # infra.config назван здесь намеренно: каталог состояния у журнала
        # тот же, что у всего прочего, и считать его второй раз означало бы
        # журнал, уехавший мимо песочницы прогона.
        self.assertEqual(reached, {"infra.telemetry", "infra.scrub", "infra.config"})

    def test_telemetry_patterns_come_from_scrub(self):
        from infra import scrub
        from infra import telemetry
        self.assertIs(telemetry._secrets(), scrub.SECRETS)


class TestEncoderIsGone(unittest.TestCase):
    """Старый конвейер удалён: его дубль это save.py, а хуки зовут drain.py."""

    def test_no_module_imports_encoder(self):
        offenders = []
        for path in sources() + list((ROOT / "hooks").glob("*.py")):
            for num, line in enumerate(path.read_text().splitlines(), 1):
                if "encoder" in line and "import" in line:
                    offenders.append("%s:%d %s" % (path.name, num, line.strip()))
        self.assertEqual(offenders, [])

    def test_encoder_file_removed(self):
        self.assertEqual(list(ROOT.rglob("encoder.py")), [])


class TestReadingIsNotWriting(unittest.TestCase):
    """Чтение архива отдельно от записи в хранилище.

    Модуль понимания брал у модуля записи разбор транскрипта: `from save
    import TRANSCRIPTS, blocks, result_text`. Понимать архив и писать в
    хранилище — разные поводы меняться, и первый тянул за собой второй вместе
    с дверью хранилища и схемой записей.
    """

    def test_understanding_no_longer_reaches_the_writing_module(self):
        self.assertNotIn("pipeline.save", ours_reached_by("pipeline.understand"))

    def test_archive_knows_neither_the_door_nor_the_schema(self):
        reached = ours_reached_by("archive.transcripts")
        self.assertNotIn("storage.port", reached)
        self.assertNotIn("domain.models", reached)
        self.assertNotIn("storage.db", reached)

    def test_archive_owns_transcript_reading(self):
        from archive import transcripts
        for name in ("TRANSCRIPTS", "blocks", "result_text", "records_from_line",
                     "read_new", "episodes_from_file", "parse_time"):
            self.assertTrue(hasattr(transcripts, name), name)

    def test_only_one_module_parses_timestamps(self):
        """Их было два разбора: save.when и understand.parse_time — расходились молча.

        Живёт в infra: свежесть нужна и мере факта, а domain лежит ниже архива
        и тянуть его наверх нельзя.
        """
        offenders = ["%s/%s" % (p.parent.name, p.name) for p in sources()
                     if p.name != "timeline.py" and "fromisoformat" in p.read_text()]
        self.assertEqual(offenders, [])


EPISODE = {
    "session_id": "s", "number": 1, "cwd": "/home/p/dev/demo", "branch": "main",
    "request": "отвечай покороче и правь файл", "started_at": "2026-08-20T10:00:00Z",
    "ended_at": "2026-08-20T10:05:00Z",
    "files": ["/home/p/dev/demo/db.py"], "commands": ["pytest -q"],
    "replies": ["готово, см. https://example.org/doc"],
    "errors": ["ImportError: нет модуля"],
}


class TestExtractorIsARegistry(unittest.TestCase):
    """Извлекатель открыт к расширению так же, как реестр признаков.

    Была лесенка из четырёх if в одной функции. Эпик обещает тридцать пять
    эвристик — это тридцать пять правок одного тела, и каждая рискует задеть
    соседние четыре.
    """

    def test_registry_and_names_agree(self):
        from archive import extract
        self.assertEqual(sorted(extract.RULES), sorted(extract.NAMES))

    def test_every_rule_is_a_function_of_one_episode(self):
        from archive import extract
        for name in extract.NAMES:
            self.assertTrue(callable(extract.RULES[name]), name)
            self.assertIsInstance(extract.RULES[name](EPISODE), list, name)

    def test_new_heuristic_needs_no_edit_of_the_assembler(self):
        """Это и есть открытость: правило добавляется, сборщик не меняется."""
        from archive import extract
        made = ("preference", "выдуманная тема", "global", "из нового правила")
        with mock.patch.object(extract, "NAMES", extract.NAMES + ["новое"]), \
             mock.patch.dict(extract.RULES, {"новое": lambda ep: [made]}):
            self.assertIn(made, extract.facts_of(EPISODE))

    def test_the_assembler_names_no_heuristic(self):
        """Виды фактов называют правила, а не сборщик."""
        fn = next(n for n in ast.walk(ast.parse((ROOT / "archive" / "extract.py").read_text()))
                  if isinstance(n, ast.FunctionDef) and n.name == "facts_of")
        said = {n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for kind in ("preference", "project_state", "external_resource"):
            self.assertNotIn(kind, said)

    def test_every_rule_still_fires_on_a_full_episode(self):
        """Реестр не должен молча растерять эвристики при переносе."""
        from archive import extract
        kinds = {f[0] for f in extract.facts_of(EPISODE)}
        self.assertEqual(kinds, {"preference", "project_state", "external_resource"})

    def test_link_rule_strips_what_the_address_was_wrapped_in(self):
        """Адрес в ответе почти всегда во что-то завёрнут: кавычки, скобки, точка.

        Обёртка прилипала к адресу и уезжала в набор эталонов как часть
        ожидаемого: случай url-570781f1 ждал `10.0.0.1:3000"` и не мог пройти
        ничем.
        """
        from archive import extract
        wrapped = {
            'см. "https://example.org/a" тут': "https://example.org/a",
            "ставим (https://example.org/b), потом": "https://example.org/b",
            "адрес https://example.org/c.": "https://example.org/c",
            "в кавычках \u0027https://example.org/d\u0027": "https://example.org/d",
            "в скобках <https://example.org/e>": "https://example.org/e",
        }
        for reply, want in wrapped.items():
            ep = dict(EPISODE, replies=[reply])
            got = [f[3].split()[-1] for f in extract.links(ep)]
            self.assertEqual(got, [want], "из %r вышло %r" % (reply, got))

    def test_the_stand_keeps_its_names(self):
        """research/lab зовёт u.facts_of и u.NOT_CODE — стенд ломать нельзя."""
        from archive import extract
        from pipeline import understand
        self.assertIs(understand.facts_of, extract.facts_of)
        self.assertIs(understand.fact_key, extract.fact_key)
        self.assertIs(understand.NOT_CODE, extract.NOT_CODE)


class TestDoorRoles(unittest.TestCase):
    """У двери не один широкий интерфейс, а роли.

    Консоль структурной записи не умеет. Раньше это лечилось примечанием в
    докстринге и явной ошибкой при вызове — то есть договорённостью. Теперь
    метода на консольной двери просто нет: вызвать нечего.
    """

    def test_console_door_has_no_structured_write(self):
        self.assertFalse(hasattr(port.door("cli"), "write_objects"))

    def test_console_door_still_writes_text_and_reads(self):
        console = port.door("cli")
        self.assertTrue(callable(console.write))
        self.assertTrue(callable(console.read))

    def test_structured_doors_carry_all_three_roles(self):
        for name in ("api", "sdk", "local"):
            for role in ("write", "write_objects", "read"):
                self.assertTrue(callable(getattr(port.door(name), role, None)),
                                "%s.%s" % (name, role))

    def test_unknown_door_refused(self):
        with self.assertRaises(port.BackendError):
            port.door("лишний")


class TestDoorIsChosenAtCallTime(unittest.TestCase):
    """Путь наружу выбирается при вызове, а не при импорте модуля.

    Из импортного выбора росли обе беды сразу: моки в тестах вместо
    подстановки и importlib.reload в matrix. В matrix при этом перезагружали
    xmem_api и xmem_sdk, а xmem_local забыли — и его кэш соединения жил
    насквозь через обе половины сравнения.
    """

    def setUp(self):
        self.was = {k: os.environ.get(k) for k in ("XMEM_BACKEND", "XMEM_DISABLED")}

    def tearDown(self):
        for k, v in self.was.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_environment_switch_needs_no_module_reload(self):
        os.environ["XMEM_BACKEND"] = "cli"
        self.assertFalse(hasattr(port.door(), "write_objects"))
        os.environ["XMEM_BACKEND"] = "local"
        self.assertTrue(hasattr(port.door(), "write_objects"))

    def test_no_import_time_constants_left(self):
        for gone in ("BACKEND", "DISABLED", "INSTANCE"):
            self.assertFalse(hasattr(port, gone), gone)

    def test_every_adapter_with_state_is_closed(self):
        """close_all закрывает всё, у чего есть что закрывать. xmem_local забывали."""
        from storage import local
        from storage import sdk
        with mock.patch.object(local, "close") as shut_local, \
             mock.patch.object(sdk, "close") as shut_sdk:
            port.close_all()
        shut_local.assert_called_once()
        shut_sdk.assert_called_once()


class TestMatrixNeedsNoReload(unittest.TestCase):
    """A/B-сравнение переключает половины окружением, а не перезагрузкой модулей."""

    def test_reset_closes_the_local_path_too(self):
        """Перечисление руками уже забыло xmem_local — его кэш жил через обе половины."""
        from eval import matrix
        from storage import local
        from storage import sdk
        with mock.patch.object(local, "close") as shut_local, \
             mock.patch.object(sdk, "close") as shut_sdk:
            matrix.reset_session()
        shut_local.assert_called_once()
        shut_sdk.assert_called_once()

    def test_no_module_reload_left(self):
        """По разбору, а не по тексту: докстринг про reload — не вызов reload."""
        tree = ast.parse((ROOT / "eval" / "matrix.py").read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and n.attr == "reload"]
        self.assertEqual(calls, [], "importlib.reload вернулся в matrix")


class TestFallbackByCapability(unittest.TestCase):
    """Запасной текстовый путь выбирается по тому, что дверь умеет.

    Не по её имени: сравнение с "cli" в save.py означало, что добавление
    пятого пути без структурной записи молча роняет разговор.
    """

    ITEMS = [{"session": "s", "seq": 1, "event_type": "user_message", "tool": None,
              "cwd": "/tmp/p", "branch": "b", "role": "человек",
              "at": "2026-01-01T00:00:00+00:00", "text": "привет"}]

    def test_text_only_door_gets_prose(self):
        door = mock.Mock(spec=["write", "read"])
        save.deliver(list(self.ITEMS), door)
        self.assertEqual(door.write.call_count, 1)

    def test_one_door_for_the_whole_run_not_one_per_file(self):
        """Дверь открывается один раз на проход.

        По одной на файл значит, что путь наружу может смениться посреди
        прохода вместе с окружением, а половина разговоров уедет не туда.
        """
        import tempfile, json as js
        from pipeline import save
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for n in (1, 2, 3):
                f = root / ("t%d.jsonl" % n)
                f.write_text(js.dumps({
                    "type": "user", "sessionId": "s%d" % n,
                    "timestamp": "2026-08-20T10:00:00Z", "cwd": "/tmp/p",
                    "gitBranch": "b", "message": {"content": "привет %d" % n}},
                    ensure_ascii=False) + "\n", encoding="utf-8")
                paths.append(f)
            door = mock.Mock(spec=["write", "write_objects", "read"])
            with mock.patch.object(save, "STATE", root / "state.json"), \
                 mock.patch.object(port, "door") as opened:
                save.ingest(paths, dry=False, door=door)
            opened.assert_not_called()
            self.assertEqual(door.write_objects.call_count, len(paths))

    def test_ingest_opens_the_door_once_when_none_is_given(self):
        import tempfile, json as js
        from pipeline import save
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for n in (1, 2, 3):
                f = root / ("t%d.jsonl" % n)
                f.write_text(js.dumps({
                    "type": "user", "sessionId": "s%d" % n,
                    "timestamp": "2026-08-20T10:00:00Z", "cwd": "/tmp/p",
                    "gitBranch": "b", "message": {"content": "привет %d" % n}},
                    ensure_ascii=False) + "\n", encoding="utf-8")
                paths.append(f)
            with mock.patch.object(save, "STATE", root / "state.json"), \
                 mock.patch.object(port, "door",
                                   return_value=mock.Mock(spec=["write", "write_objects", "read"])) as opened:
                save.ingest(paths, dry=False)
            self.assertEqual(opened.call_count, 1)

    def test_structured_door_gets_records(self):
        door = mock.Mock(spec=["write", "write_objects", "read"])
        save.deliver(list(self.ITEMS), door)
        door.write_objects.assert_called_once()
        door.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
