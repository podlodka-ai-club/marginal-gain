#!/usr/bin/env python3
"""Проверки живого контура. Запуск: python3 -m unittest tests.test_live_wiring -v

Хуки написаны и рабочие, но точки в настройках агента пустовали: звать их было
некому. Всё, что лежит в базе, положено ручными запусками, и любая проверка
меряла старый снимок, а не работу. Здесь проверяется ровно стык: заняты ли
точки, доходит ли до них дело и что происходит, когда вызываемое сломано.
"""
import json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from pipeline import save

HERE = Path(__file__).resolve().parent.parent
HOOKS = HERE / "hooks"

# Настройки агента лежат в четырёх местах, и любое из них занимает точку.
# Проверяем объединение, а не один файл: иначе регистрация в соседнем файле
# читалась бы как её отсутствие.
SETTINGS = [
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
    HERE / ".claude" / "settings.json",
    HERE / ".claude" / "settings.local.json",
]


def registered(event):
    """Команды, занявшие точку. Пустой список значит, что точка свободна."""
    out = []
    for path in SETTINGS:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for group in (body.get("hooks") or {}).get(event) or []:
            for hook in group.get("hooks") or []:
                command = hook.get("command")
                if command:
                    out.append(command)
    return out


# Наши хуки по именам. Опознавать их по пути этого клона нельзя: соседние
# хуки в тех же настройках записаны через `~`, и стоит записать так же наш —
# проверка скажет «точка свободна» о занятой точке.
MINE = ("on_prompt_queue.sh", "on_prompt_read.sh", "on_stop.sh", "on_prompt.py")


def ours(commands):
    """Только наши команды: чужие напоминатели в тех же точках не в счёт."""
    return [c for c in commands if any(name in c for name in MINE)]


def where(command):
    """Файл, который зовёт команда. Путь раскрываем так же, как это сделает bash."""
    match = re.search(r"([^\s\"\']+/hooks/[^\s\"\']+?\.(?:sh|py))", command)
    if not match:
        return None
    target = os.path.expandvars(match.group(1))
    if target.startswith("~"):
        target = str(Path.home()) + target[1:]
    return Path(target)


def fake_python(tmp, message="boom: сломанный модуль"):
    """Каталог с python3, который всегда падает. Так ломается вызываемое."""
    binary = Path(tmp) / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    stub = binary / "python3"
    stub.write_text("#!/bin/sh\necho '%s' >&2\nexit 1\n" % message, encoding="utf-8")
    stub.chmod(0o755)
    return binary


class TestPointsAreTaken(unittest.TestCase):
    """Точка без команды это ненаписанный хук: код есть, звать его некому."""

    def test_the_human_message_calls_reading(self):
        commands = ours(registered("UserPromptSubmit"))
        self.assertTrue(any("on_prompt_read.sh" in c for c in commands),
                        "точка UserPromptSubmit не зовёт чтение: %r" % commands)

    def test_the_human_message_fills_the_queue(self):
        commands = ours(registered("UserPromptSubmit"))
        self.assertTrue(any("on_prompt_queue.sh" in c for c in commands),
                        "точка UserPromptSubmit не наполняет очередь: %r" % commands)

    def test_the_end_of_turn_calls_writing(self):
        commands = ours(registered("Stop"))
        self.assertTrue(any("on_stop.sh" in c for c in commands),
                        "точка Stop не зовёт запись: %r" % commands)

    def test_registered_commands_point_at_files_that_exist(self):
        """Запись в настройках, ведущая в пустоту, молчит так же, как её отсутствие."""
        missing = []
        for event in ("UserPromptSubmit", "Stop"):
            for command in ours(registered(event)):
                target = where(command)
                if target is None or not target.exists():
                    missing.append((event, command))
        self.assertEqual(missing, [], "точка занята командой, которой нет")


class TestCommonKnowsWhereItLives(unittest.TestCase):
    """Корень вычисляется от самого хука. Чужой путь уже ломал оба хука молча."""

    def test_root_holds_the_modules_the_hooks_call(self):
        done = subprocess.run(
            ["bash", "-c", 'source "%s"; printf "%%s" "$ROOT"' % (HOOKS / "common.sh")],
            capture_output=True, text=True)
        root = Path(done.stdout.strip())
        self.assertTrue((root / "pipeline" / "drain.py").exists(),
                        "корень из common.sh не содержит вызываемых модулей: %s" % root)

    def test_state_dir_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                ["bash", "-c", 'source "%s"; printf "%%s" "$STATE_DIR"' % (HOOKS / "common.sh")],
                capture_output=True, text=True, env=dict(os.environ, HOME=tmp))
            state = Path(done.stdout.strip())
            self.assertTrue(state.is_dir(), "каталог состояния не создан: %s" % state)


class TestGateKeepsHooksOutOfEveryTurn(unittest.TestCase):
    """Точки заняты у всего пользователя: без ворот хуки живут в каждом проекте.

    Ворота стоят в bash, до запуска питона. Разница не косметическая: молчание
    ценой миллисекунд отличается от молчания ценой запуска интерпретатора на
    каждое сообщение человека.
    """

    def home(self, tmp, allow=(), off=False):
        """Подставной дом: список разрешённого и выключатель лежат в нём."""
        state = Path(tmp) / ".local" / "state" / "memory-encoder"
        state.mkdir(parents=True, exist_ok=True)
        (state / "live-projects").write_text(
            "".join("%s\n" % line for line in allow), encoding="utf-8")
        if off:
            (state / "off").write_text("", encoding="utf-8")
        return state

    def run_hook(self, script, tmp, payload, project):
        env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR=project)
        env.pop("XMEM_LIVE", None)
        return subprocess.run(["bash", str(HOOKS / script)], input=payload,
                              env=env, capture_output=True, text=True, timeout=30)

    def test_queue_stays_empty_outside_the_allowed_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.home(tmp, allow=[str(HERE)])
            got = self.run_hook("on_prompt_queue.sh", tmp,
                                json.dumps({"prompt": "чужой проект"}), "/tmp/чужое")
            self.assertEqual(got.returncode, 0)
            self.assertFalse((state / "queue.jsonl").exists(),
                             "хук записал ход из проекта, которого нет в списке")

    def test_queue_fills_inside_the_allowed_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.home(tmp, allow=["/tmp/своё"])
            got = self.run_hook("on_prompt_queue.sh", tmp,
                                json.dumps({"prompt": "свой проект", "session_id": "gate-1"}),
                                "/tmp/своё")
            self.assertEqual(got.returncode, 0, got.stderr[-300:])
            # Питон ушёл в фон, чтобы не держать ход: ждём его, а не смотрим сразу.
            deadline = time.time() + 5
            while time.time() < deadline and not (state / "queue.jsonl").exists():
                time.sleep(0.1)
            self.assertTrue((state / "queue.jsonl").exists(),
                            "разрешённый проект тоже молчит: ворота заперты наглухо")

    def test_missing_list_means_silence_everywhere(self):
        """Нет списка — нет работы. Иначе установка включает себя сама."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".local" / "state" / "memory-encoder"
            got = self.run_hook("on_prompt_queue.sh", tmp,
                                json.dumps({"prompt": "без списка"}), str(HERE))
            self.assertEqual(got.returncode, 0)
            self.assertFalse((state / "queue.jsonl").exists(),
                             "без списка разрешённого хук всё равно пишет")

    def test_the_off_switch_silences_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.home(tmp, allow=["/tmp/своё"], off=True)
            got = self.run_hook("on_prompt_queue.sh", tmp,
                                json.dumps({"prompt": "выключено"}), "/tmp/своё")
            self.assertEqual(got.returncode, 0)
            self.assertFalse((state / "queue.jsonl").exists(),
                             "выключатель не глушит хук")

    def test_reading_does_not_start_python_outside_the_allowed_projects(self):
        """Чужой проект не должен стоить даже запуска интерпретатора."""
        with tempfile.TemporaryDirectory() as tmp:
            state = self.home(tmp, allow=[str(HERE)])
            env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR="/tmp/чужое",
                       PATH="%s:%s" % (fake_python(tmp), os.environ["PATH"]))
            env.pop("XMEM_LIVE", None)
            got = subprocess.run(["bash", str(HOOKS / "on_prompt_read.sh")],
                                 input=json.dumps({"prompt": "чужой"}), env=env,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(got.returncode, 0)
            self.assertEqual(got.stdout.strip(), "")
            self.assertFalse((state / "suggest.log").exists(),
                             "питон всё-таки запускался: ворота стоят не там")

    def test_writing_hook_obeys_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.home(tmp, allow=[str(HERE)])
            env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR="/tmp/чужое",
                       PATH="%s:%s" % (fake_python(tmp), os.environ["PATH"]))
            env.pop("XMEM_LIVE", None)
            got = subprocess.run(["bash", str(HOOKS / "on_stop.sh")],
                                 input=json.dumps({"session_id": "gate-2"}), env=env,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(got.returncode, 0)
            time.sleep(0.5)
            self.assertFalse((state / "save.log").exists(),
                             "конец хода разбирает очередь в чужом проекте")


class TestEnvironmentReachesTheHooks(unittest.TestCase):
    """Хранилище задаётся в .env репозитория, а не в окружении пользователя.

    Без этого запись падала каждый ход: XMEM_INSTANCE_ID не задан, ошибка в
    журнале, счётчики базы стоят. Ошибка видна, но видна только в журнале.
    """

    def env_of(self, name, extra=None):
        """Значение переменной так, как его увидит хук, без своего окружения."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("XMEM_")}
        clean.update(extra or {})
        done = subprocess.run(
            ["bash", "-c", 'source "%s"; printf "%%s" "${%s:-}"' % (HOOKS / "common.sh", name)],
            capture_output=True, text=True, env=clean)
        return done.stdout.strip()

    @unittest.skipUnless((HERE / ".env").exists(), ".env не выложен в репозиторий")
    def test_repository_env_reaches_the_hook(self):
        want = {}
        for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                want[key.strip()] = value.strip()
        self.assertTrue(want.get("XMEM_INSTANCE_ID"), "в .env не названо хранилище")
        # Сравниваем молча: значение это ключ, ему не место в выводе проверки.
        self.assertTrue(self.env_of("XMEM_INSTANCE_ID") == want["XMEM_INSTANCE_ID"],
                        "хук не видит хранилища из .env")

    def test_live_contour_writes_locally(self):
        """Живой контур пишет в SQLite, а не по сети.

        Сеть в горячем пути стоит задержки, ключа и квоты, а квота кончается
        ровно на замере. Ручные прогоны при этом остаются на пути из .env:
        решение касается хода, а не всего проекта.
        """
        self.assertEqual(self.env_of("XMEM_BACKEND"), "local",
                         "конец хода ходит в сеть")

    def switch(self, tmp, value):
        """Переключатель режима: файл рядом с очередью, одна строка."""
        state = Path(tmp) / ".local" / "state" / "memory-encoder"
        state.mkdir(parents=True, exist_ok=True)
        (state / "backend").write_text("%s\n" % value, encoding="utf-8")
        return state

    def test_the_switch_sends_the_turn_back_to_the_network(self):
        """Вернуться на хранилище за сетью — одна строка, без правки кода.

        Иначе режим переключается только окружением самого агента, а до него
        из терминала не дотянуться: получается умолчание без выключателя.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.switch(tmp, "sdk")
            self.assertEqual(self.env_of("XMEM_BACKEND", {"HOME": tmp}), "sdk",
                             "переключатель режима не действует")

    def test_the_switch_holds_the_turn_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.switch(tmp, "local")
            self.assertEqual(self.env_of("XMEM_BACKEND", {"HOME": tmp}), "local")

    def test_a_named_backend_wins_over_the_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.switch(tmp, "sdk")
            self.assertEqual(self.env_of("XMEM_BACKEND", {"HOME": tmp,
                                                          "XMEM_BACKEND": "local"}), "local",
                             "названное в окружении слабее файла")

    def test_a_named_backend_wins_over_the_default(self):
        """Заданное снаружи сильнее умолчания, иначе замер не переключить."""
        self.assertEqual(self.env_of("XMEM_BACKEND", {"XMEM_BACKEND": "sdk"}), "sdk",
                         "хук навязывает свой путь наружу")


class TestBrokenModuleDoesNotBreakTheTurn(unittest.TestCase):
    """Хук обязан молчать в разговоре, но не обязан молчать вообще."""

    def run_hook(self, script, tmp, payload):
        env = dict(os.environ, HOME=tmp, XMEM_LIVE="1",
                   PATH="%s:%s" % (fake_python(tmp), os.environ["PATH"]))
        return subprocess.run(["bash", str(HOOKS / script)], input=payload,
                              env=env, capture_output=True, text=True, timeout=30)

    def log_of(self, tmp, name, seconds=5):
        target = Path(tmp) / ".local" / "state" / "memory-encoder" / name
        deadline = time.time() + seconds
        while time.time() < deadline:
            if target.exists() and target.read_text(encoding="utf-8", errors="replace").strip():
                break
            time.sleep(0.1)
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def test_reading_hook_survives_a_broken_module(self):
        payload = json.dumps({"prompt": "что там по мере", "session_id": "broken-1"})
        with tempfile.TemporaryDirectory() as tmp:
            got = self.run_hook("on_prompt_read.sh", tmp, payload)
            self.assertEqual(got.returncode, 0, "хук уронил ход: %s" % got.stderr[-200:])
            self.assertEqual(got.stdout.strip(), "", "хук заговорил на сломанном модуле")
            self.assertIn("boom", self.log_of(tmp, "suggest.log"),
                          "ошибка нигде не записана, поломка видна только глазами")

    def test_writing_hook_survives_a_broken_module(self):
        payload = json.dumps({"session_id": "broken-2", "transcript_path": ""})
        with tempfile.TemporaryDirectory() as tmp:
            got = self.run_hook("on_stop.sh", tmp, payload)
            self.assertEqual(got.returncode, 0, "хук уронил ход: %s" % got.stderr[-200:])
            self.assertIn("boom", self.log_of(tmp, "save.log"),
                          "ошибка нигде не записана, поломка видна только глазами")


class TestReadingKeepsItsDeadline(unittest.TestCase):
    """Срок держит модуль. Опоздавшая подсказка не нужна, а задержка хода вредна."""

    SLOW = ("import time\n"
            "try:\n"
            "    from storage import port\n"
            "except Exception:\n"
            "    port = None\n"
            "if port is not None:\n"
            "    class Slow:\n"
            "        def read(self, *a, **k):\n"
            "            time.sleep(60)\n"
            "        def write(self, *a, **k):\n"
            "            return None\n"
            "    port.door = lambda *a, **k: Slow()\n")

    def test_hook_returns_before_the_conversation_notices(self):
        payload = json.dumps({"prompt": "долгий вопрос", "session_id": "slow-1"})
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "shim"
            shim.mkdir()
            (shim / "sitecustomize.py").write_text(self.SLOW, encoding="utf-8")
            env = dict(os.environ, HOME=tmp, PYTHONPATH=str(shim),
                       XMEM_LIVE="1", XMEM_HOOK_SECONDS="2")
            started = time.time()
            got = subprocess.run(["bash", str(HOOKS / "on_prompt_read.sh")],
                                 input=payload, env=env, capture_output=True,
                                 text=True, timeout=60)
            spent = time.time() - started
            self.assertEqual(got.returncode, 0)
            self.assertEqual(got.stdout.strip(), "", "опоздавшая подсказка всё же заговорила")
            self.assertLess(spent, 15, "хук держал ход %.1f с вместо срока" % spent)


if __name__ == "__main__":
    unittest.main()


class Collector:
    """Дверь, которая ничего не умеет, кроме как запомнить принятое."""

    def __init__(self):
        self.records = []

    def write_objects(self, records, relations=None):
        self.records.extend(records)
        return len(records)


class TestCursorIsPerStore(unittest.TestCase):
    """Отметка о прочитанном принадлежит хранилищу, а не архиву.

    Ход теперь пишет в локальную базу, а ручной прогон — в хранилище за сетью.
    Пока книжка учёта одна на всех, ход двигает её каждые несколько минут, и
    сетевой прогон приходит к дочитанному архиву: отправлять нечего. Хуки
    срабатывают чаще человека и выигрывают эту гонку всегда.
    """

    ROW = {"type": "user", "sessionId": "cursor-1",
           "timestamp": "2026-08-27T10:00:00Z", "cwd": "/home/person/dev/demo",
           "gitBranch": "memory-encoder",
           "message": {"content": "Проверка отметки о прочитанном"}}

    def archive(self, tmp):
        path = Path(tmp) / "разговор.jsonl"
        path.write_text(json.dumps(self.ROW, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_a_local_turn_leaves_the_archive_unread_for_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.archive(tmp)
            here, there = Collector(), Collector()
            with mock.patch.object(save, "STATE", Path(tmp) / "state.json"):
                with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                    save.ingest([path], dry=False, door=here)
                with mock.patch.dict(os.environ, {"XMEM_BACKEND": "sdk"}):
                    save.ingest([path], dry=False, door=there)
            self.assertTrue(here.records, "локальный проход не записал ничего")
            self.assertTrue(there.records,
                            "сетевой проход пришёл к дочитанному архиву: "
                            "ход в локальную базу закрыл его для сети")

    def test_the_same_store_still_reads_each_line_once(self):
        """Разделение отметок не должно превращаться в повторную запись."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.archive(tmp)
            first, second = Collector(), Collector()
            with mock.patch.object(save, "STATE", Path(tmp) / "state.json"), \
                 mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                save.ingest([path], dry=False, door=first)
                save.ingest([path], dry=False, door=second)
            self.assertTrue(first.records)
            self.assertEqual(second.records, [], "то же хранилище перечитало архив")


class TestEnvIsParsedProperly(unittest.TestCase):
    """Разбор .env. Кривая строка стоит одной ошибки в журнале за ход.

    Проверяем на подставном корне, а не на своём: свой .env один, а форм
    записи в нём много, и ловить надо форму, а не сегодняшнее содержимое.
    """

    def source(self, body, name="XMEM_INSTANCE_ID"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "hooks").mkdir(parents=True)
            (root / "hooks" / "common.sh").write_text(
                (HOOKS / "common.sh").read_text(encoding="utf-8"), encoding="utf-8")
            (root / ".env").write_text(body, encoding="utf-8")
            clean = {k: v for k, v in os.environ.items() if not k.startswith("XMEM_")}
            clean["HOME"] = tmp
            done = subprocess.run(
                ["bash", "-c", 'source "%s"; printf "%%s" "${%s:-}"'
                 % (root / "hooks" / "common.sh", name)],
                capture_output=True, text=True, env=clean)
            return done.stdout.strip(), done.stderr.strip()

    def test_the_export_form_is_understood(self):
        got, _ = self.source("export XMEM_INSTANCE_ID=fe1e2af9\n")
        self.assertEqual(got, "fe1e2af9", "форма export KEY=value потеряна")

    def test_quotes_do_not_reach_the_value(self):
        got, _ = self.source('XMEM_INSTANCE_ID="fe1e2af9"\n')
        self.assertEqual(got, "fe1e2af9", "кавычки уехали в значение")

    def test_spaces_around_the_key_do_not_break_it(self):
        got, _ = self.source("  XMEM_INSTANCE_ID = fe1e2af9\n")
        self.assertEqual(got, "fe1e2af9", "пробелы вокруг знака равенства ломают ключ")

    def test_a_bad_line_stays_quiet(self):
        """Хук молчит в разговоре: ругань bash уходит в вывод самого хука."""
        _, err = self.source("не-ключ=1\nXMEM_INSTANCE_ID=fe1e2af9\n")
        self.assertEqual(err, "", "разбор .env ругается в поток хука: %r" % err)


class TestQueueHookDoesNotHoldTheTurn(unittest.TestCase):
    """Точка на сообщении человека синхронная: ход ждёт, пока хук не выйдет."""

    def test_the_queue_hook_returns_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "bin"
            binary.mkdir()
            slow = binary / "python3"
            slow.write_text("#!/bin/sh\nsleep 20\n", encoding="utf-8")
            slow.chmod(0o755)
            env = dict(os.environ, HOME=tmp, XMEM_LIVE="1",
                       PATH="%s:%s" % (binary, os.environ["PATH"]))
            started = time.time()
            got = subprocess.run(["bash", str(HOOKS / "on_prompt_queue.sh")],
                                 input=json.dumps({"prompt": "быстро"}),
                                 env=env, capture_output=True, text=True, timeout=30)
            spent = time.time() - started
            self.assertEqual(got.returncode, 0)
            self.assertLess(spent, 3, "хук держал ход %.1f с" % spent)


class TestAllowedPathsAreNormalised(unittest.TestCase):
    """Незамеченная кривизна в списке выглядит как исправно молчащий хук."""

    def gate(self, allow, here, home):
        state = Path(home) / ".local" / "state" / "memory-encoder"
        state.mkdir(parents=True, exist_ok=True)
        (state / "live-projects").write_text(allow, encoding="utf-8")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("XMEM_")}
        clean.update(HOME=home, CLAUDE_PROJECT_DIR=here)
        done = subprocess.run(
            ["bash", "-c", 'source "%s"; live && printf open || printf shut'
             % (HOOKS / "common.sh")], capture_output=True, text=True, env=clean)
        return done.stdout.strip()

    def test_trailing_space_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "проект"
            work.mkdir()
            self.assertEqual(self.gate("%s   \n" % work, str(work), tmp), "open",
                             "пробел в конце строки запирает ворота")

    def test_windows_line_ending_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "проект"
            work.mkdir()
            self.assertEqual(self.gate("%s\r\n" % work, str(work), tmp), "open",
                             "перевод строки из Windows запирает ворота")

    def test_a_symlinked_prefix_still_matches(self):
        """На macOS /tmp это ссылка на /private/tmp: та же папка, разные строки."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "проект"
            work.mkdir()
            link = Path(tmp) / "ссылка"
            link.symlink_to(work, target_is_directory=True)
            self.assertEqual(self.gate("%s\n" % link, str(work), tmp), "open",
                             "ссылка и настоящий путь считаются разными местами")


class TestRegistrationHelpersSurviveOtherSpellings(unittest.TestCase):
    """Проверка занятости точек не должна зависеть от того, как записан путь.

    Прочие хуки в тех же настройках записаны через `~`. Стоит записать так же
    наш — и проверка скажет «точка свободна» о занятой точке.
    """

    def test_a_tilde_command_is_recognised_as_ours(self):
        command = "~/dev/marginal-gain/hooks/on_stop.sh"
        self.assertEqual(ours([command]), [command],
                         "команда через ~ не опознана как наша")

    def test_a_tilde_command_resolves_to_a_file(self):
        target = where("~/dev/marginal-gain/hooks/on_stop.sh")
        self.assertIsNotNone(target)
        self.assertTrue(str(target).startswith(str(Path.home())),
                        "путь через ~ не раскрыт: %s" % target)

    def test_a_quoted_command_resolves_to_a_file(self):
        target = where('bash "%s/on_stop.sh"' % HOOKS)
        self.assertEqual(target, HOOKS / "on_stop.sh",
                         "кавычки уехали внутрь пути: %s" % target)


class TestSwitchesAreWrittenDown(unittest.TestCase):
    """Рубильник, о котором не сказано, не существует.

    Ворота молчат одинаково и когда всё исправно, и когда выключено. Человек
    не восстановит это по коду хука: он не знает, что смотреть.
    """

    DOCS = ("README.md", "AGENTS.md")

    def text(self):
        return "\n".join((HERE / name).read_text(encoding="utf-8")
                         for name in self.DOCS)

    def test_every_switch_the_hooks_read_is_documented(self):
        # Смотрим весь горячий путь, а не один common.sh: срок чтения задаётся
        # в модуле подсказки, и рубильник от этого не перестаёт быть рубильником.
        body = "\n".join(f.read_text(encoding="utf-8") for f in sorted(HOOKS.glob("*"))
                         if f.is_file())
        body += (HERE / "pipeline" / "suggest.py").read_text(encoding="utf-8")
        switches = set(re.findall(r"XMEM_[A-Z_]+", body))
        switches |= {name for name in ("live-projects", "off", "backend")
                     if name in body}
        self.assertTrue(switches, "в common.sh не нашлось ни одного рубильника")
        docs = self.text()
        missing = sorted(s for s in switches if s not in docs)
        self.assertEqual(missing, [], "рубильник есть в коде, но не назван в документации")
