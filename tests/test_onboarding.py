#!/usr/bin/env python3
"""Памятка о подключении проходится чужим человеком до конца.

Запуск: python3 -m unittest tests.test_onboarding -v

Проверка не читает памятку глазами, а выполняет её: берёт из неё блок для
настроек агента, кладёт его в пустой дом, открывает ворота названной там
командой, сеет названный там пробный факт и зовёт ровно ту команду, которую
памятка велела вписать в настройки. Ожидаемый сигнал — блок «Из памяти» в
выводе хука.

Отсюда и мутация: убрать из памятки шаг про регистрацию точек, и звать будет
нечего — проверка краснеет. Дом у прогона свой, временный, поэтому настройки
машины, где идёт прогон, ни на что не влияют: памятка проверяется на чистых
настройках, а не на уже подключённом контуре.

Свойствами, а не одним сценарием: важно не «на моей машине сработало», а
«сработает, как бы ни были записаны чужие хуки в тех же точках и какие бы
рубильники ни стояли по умолчанию».
"""
import json, os, re, subprocess, tempfile, unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from pipeline import switch

HERE = Path(__file__).resolve().parent.parent
MEMO = HERE / "SETUP.md"

# Точки, без которых памятка бесполезна: чтение на сообщении человека и запись
# в конце хода. Остальные (срезание служебного блока) — не обязательны для
# первой подсказки и потому здесь не требуются.
NEEDED = {"UserPromptSubmit": "on_prompt_read.sh", "Stop": "on_stop.sh"}

ANSWER = "Из памяти прошлых разговоров:"

SLOW = settings(deadline=None, max_examples=5,
                suppress_health_check=[HealthCheck.too_slow])


def memo():
    """Текст памятки. Нет файла — нет и подключения: пусть проверка скажет это."""
    try:
        return MEMO.read_text(encoding="utf-8")
    except OSError:
        return ""


def fences(language=None):
    """Блоки кода из памятки. Человек копирует именно их, а не пересказ."""
    found = re.findall(r"```([a-z]*)\n(.*?)```", memo(), re.DOTALL)
    return [body for mark, body in found if language is None or mark == language]


def registration():
    """Блок для настроек агента: единственный json, где названы точки.

    Пусто значит, что шага про регистрацию в памятке нет. Всё, что ниже,
    после этого валится — и должно валиться: без занятых точек хуки не зовёт
    никто, а выглядит это как исправная тишина.
    """
    for body in fences("json"):
        try:
            got = json.loads(body)
        except ValueError:
            continue
        if isinstance(got, dict) and got.get("hooks"):
            return got
    return None


def commands(config, event):
    """Команды, занявшие точку в этих настройках."""
    out = []
    for group in ((config or {}).get("hooks") or {}).get(event) or []:
        for hook in group.get("hooks") or []:
            if hook.get("command"):
                out.append(hook["command"])
    return out


def here(command):
    """Команда из памятки, переписанная на этот клон.

    В памятке путь показан от домашнего каталога — у читателя клон лежит своим
    местом. Проверке важно не совпадение строки, а то, что команда ведёт в
    файл хука и этот файл работает.
    """
    return re.sub(r"[^\s\"']*/hooks/", "%s/hooks/" % HERE, command)


def target(command):
    """Файл, который зовёт команда."""
    match = re.search(r"([^\s\"']+/hooks/[^\s\"']+?\.(?:sh|py))", here(command))
    return Path(match.group(1)) if match else None


def mine(named):
    """Только наши команды: чужие хуки в тех же точках зовёт агент, не мы."""
    return [c for c in named if target(c) is not None]


def clean_env(home, base):
    """Окружение чужой машины: свои рубильники прогона не подсказывают."""
    out = {k: v for k, v in os.environ.items() if not k.startswith("XMEM_")}
    out.update(HOME=str(home), CLAUDE_PROJECT_DIR=str(HERE),
               XMEM_BACKEND="local", XMEM_LOCAL_PATH=str(base),
               XMEM_INSTANCE_ID="test-instance")
    return out


def run(command, home, base, stdin=None, cwd=HERE):
    return subprocess.run(["bash", "-c", command], input=stdin, cwd=str(cwd),
                          env=clean_env(home, base), capture_output=True,
                          text=True, timeout=120)


def line_with(needle, language="bash"):
    """Строка команды из памятки. Проверяем названное там, а не своё."""
    for body in fences(language):
        for line in body.splitlines():
            if needle in line and not line.strip().startswith("#"):
                return line.split("#")[0].strip()
    return None


def named(needle):
    """Та же строка, но её отсутствие — сразу поломка с внятным словом.

    Иначе выкинутый из памятки шаг вылезает падением подстановки где-то ниже,
    и по обломкам не видно, чего именно не хватает.
    """
    line = line_with(needle)
    if not line:
        raise AssertionError("памятка не называет команды со словами %r" % needle)
    return line


def probe_block():
    """Блок, который кладёт в базу пробный факт: без него смотреть не на что."""
    for body in fences("bash"):
        if "Fact(" in body:
            return body
    return None


def wire(home, existing=None):
    """Настройки агента ровно те, что велит памятка.

    Чужие хуки в тех же точках дописываются рядом, а не затираются: так это и
    описано в памятке, и так это делает человек с уже настроенным агентом.
    """
    config = json.loads(json.dumps(existing or {}))
    ours = registration() or {}
    hooks = config.setdefault("hooks", {})
    for event, groups in (ours.get("hooks") or {}).items():
        rewritten = json.loads(here(json.dumps(groups, ensure_ascii=False)))
        hooks.setdefault(event, []).extend(rewritten)
    folder = Path(home) / ".claude"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def machine(tmp, existing=None):
    """Чистая машина по памятке: пустой дом, пустая база, занятые точки."""
    home = Path(tmp) / "дом"
    home.mkdir()
    base = Path(tmp) / "memory.db"
    config = wire(home, existing)
    run(named("here add"), home, base)
    seed = probe_block()
    if seed is None:
        raise AssertionError("памятка не кладёт в базу ничего: вспоминать нечего")
    run(seed, home, base)
    return home, base, config


FOREIGN = st.lists(st.sampled_from(["echo напоминалка", "~/bin/чужой-хук.sh", "true"]),
                   max_size=2, unique=True)
POINTS = st.sampled_from(["UserPromptSubmit", "Stop", "SessionStart"])


@st.composite
def settings_before(draw):
    """Настройки, которые у человека уже есть: пусто, пусто с хуками, чужие хуки."""
    shape = draw(st.sampled_from(["нет", "пусто", "хуки пусты", "чужие"]))
    if shape == "нет":
        return None
    if shape == "пусто":
        return {"model": "opus"}
    if shape == "хуки пусты":
        return {"hooks": {}}
    event = draw(POINTS)
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": c}]}
                              for c in draw(FOREIGN)]}}


class TestTheMemoIsThereAtAll(unittest.TestCase):
    """Памятки нет — проект показать некому: читатель застрянет на подключении."""

    def test_the_memo_exists(self):
        self.assertTrue(MEMO.exists(), "нет памятки о подключении: %s" % MEMO)

    def test_it_starts_from_a_clone(self):
        self.assertIn("git clone", memo(),
                      "памятка начинается не с нуля: читатель уже должен что-то знать")

    def test_the_readme_points_at_it(self):
        """Читатель приходит в README и обязан увидеть дверь оттуда."""
        self.assertIn(MEMO.name, (HERE / "README.md").read_text(encoding="utf-8"),
                      "на памятку не ведёт ничего: её никто не найдёт")


class TestTheOffSwitchIsSaidFirst(unittest.TestCase):
    """Точки заняты у всего пользователя. Первое, что нужно знать, — как выключить."""

    def test_one_command_silences_everything(self):
        self.assertIsNotNone(line_with("live off"),
                             "не сказано, как выключить всё одной командой")

    def test_the_off_switch_comes_before_the_registration(self):
        body = memo()
        off = body.find("live off")
        point = body.find("UserPromptSubmit")
        self.assertNotEqual(off, -1)
        self.assertNotEqual(point, -1)
        self.assertLess(off, point,
                        "выключатель назван после того, как всё уже включено")


class TestTheRegistrationTakesBothPoints(unittest.TestCase):
    """Блок для настроек — сердце памятки: без него звать хуки некому."""

    def test_the_memo_carries_a_settings_block(self):
        self.assertIsNotNone(registration(),
                             "в памятке нет блока для настроек агента")

    def test_every_needed_point_is_taken(self):
        config = registration()
        for event, hook in NEEDED.items():
            named = commands(config, event)
            self.assertTrue(any(hook in c for c in named),
                            "точка %s не зовёт %s: %r" % (event, hook, named))

    def test_the_named_commands_lead_to_files_that_exist(self):
        """Запись, ведущая в пустоту, молчит так же, как её отсутствие."""
        config = registration()
        for event in NEEDED:
            for command in commands(config, event):
                path = target(command)
                self.assertIsNotNone(path, "команда не зовёт файл хука: %r" % command)
                self.assertTrue(path.exists(), "команда зовёт пустоту: %s" % path)

    def test_the_hooks_go_through_the_gate(self):
        """Хук мимо common.sh минует ворота и работает во всех проектах сразу."""
        config = registration()
        for event in NEEDED:
            for command in commands(config, event):
                body = target(command).read_text(encoding="utf-8")
                self.assertIn("common.sh", body,
                              "памятка велит звать хук мимо ворот: %r" % command)

    @given(existing=settings_before())
    def test_foreign_hooks_survive_the_registration(self, existing):
        """У читателя уже что-то настроено. Памятка не должна это стирать."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "дом"
            home.mkdir()
            after = wire(home, existing)
            for event in ((existing or {}).get("hooks") or {}):
                for command in commands(existing, event):
                    self.assertIn(command, commands(after, event),
                                  "чужой хук в точке %s затёрт" % event)

    @given(existing=settings_before())
    def test_both_points_are_taken_whatever_was_there_before(self, existing):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "дом"
            home.mkdir()
            after = wire(home, existing)
            for event, hook in NEEDED.items():
                self.assertTrue(any(hook in c for c in commands(after, event)),
                                "точка %s осталась свободной" % event)


class TestAFreshMachineGetsAMemoryBack(unittest.TestCase):
    """Главный сигнал: человек прошёл памятку и увидел воспоминание.

    Всё берётся из памятки: команда открытия ворот, пробный факт, вопрос и
    команда, вписанная в настройки. Свой код проверка не подставляет — иначе
    она зеленела бы на памятке, из которой выкинули половину шагов.
    """

    def question(self):
        """Вопрос, который памятка велит задать. Он же поедет в хук."""
        found = re.search(r'"prompt"\s*:\s*"([^"]+)"', memo())
        return found.group(1) if found else None

    def ask(self, home, base, config, question):
        said = []
        for command in mine(commands(config, "UserPromptSubmit")):
            payload = json.dumps({"prompt": question, "session_id": "подключение-1"},
                                 ensure_ascii=False)
            got = run(here(command), home, base, stdin=payload)
            self.assertEqual(got.returncode, 0,
                             "хук уронил ход: %s" % got.stderr[-300:])
            said.append(got.stdout)
        return "\n".join(said)

    def test_the_memo_names_the_question_to_ask(self):
        self.assertIsNotNone(self.question(),
                             "памятка не называет вопроса, на который придёт память")

    def test_the_memo_seeds_something_to_recall(self):
        self.assertIsNotNone(probe_block(),
                             "на пустой базе вспоминать нечего, а положить нечем")

    @SLOW
    @given(existing=settings_before())
    def test_the_memory_reaches_the_conversation(self, existing):
        with tempfile.TemporaryDirectory() as tmp:
            home, base, config = machine(tmp, existing)
            said = self.ask(home, base, config, self.question())
            self.assertIn(ANSWER, said,
                          "человек прошёл памятку и памяти не увидел: %r" % said[-300:])

    @SLOW
    @given(existing=settings_before())
    def test_the_off_switch_from_the_memo_really_silences_it(self, existing):
        """Обещание «выключить всё одной командой» проверяется, а не берётся на слово."""
        with tempfile.TemporaryDirectory() as tmp:
            home, base, config = machine(tmp, existing)
            run(named("live off"), home, base)
            said = self.ask(home, base, config, self.question())
            self.assertNotIn(ANSWER, said, "выключатель не выключает")
            self.assertEqual(said.strip(), "",
                             "выключенный хук всё равно говорит: %r" % said[:200])


class TestTheMemoSaysWhereToLookWhenItIsQuiet(unittest.TestCase):
    """Хук молчит и в разговоре, и при поломке: без журналов человек гадает."""

    def logs(self):
        body = "\n".join(f.read_text(encoding="utf-8")
                         for f in sorted((HERE / "hooks").glob("*.sh")))
        return sorted(set(re.findall(r"[a-z]+\.log", body)))

    @given(name=st.sampled_from(["suggest.log", "save.log", "queue.log"]))
    def test_every_log_the_hooks_write_is_named(self, name):
        self.assertIn(name, self.logs(), "журнал %s хуки больше не пишут" % name)
        self.assertIn(name, memo(), "журнал %s в памятке не назван" % name)

    def test_the_state_command_is_named(self):
        """Закрытые ворота и исправный хук снаружи неотличимы — кроме этой команды."""
        self.assertIsNotNone(line_with("./bin/xmem"),
                             "не сказано, чем отличить закрытые ворота от тишины по делу")

    def test_the_state_command_tells_a_shut_gate_from_a_working_hook(self):
        """Проверяем саму разницу, а не упоминание о ней."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "дом"
            home.mkdir()
            base = Path(tmp) / "memory.db"
            shut = run("./bin/xmem", home, base).stdout
            self.assertIn("молчит", shut, "закрытые ворота выглядят как рабочие")
            run(named("here add"), home, base)
            opened = run("./bin/xmem", home, base).stdout
            self.assertIn("живая", opened, "открытые ворота выглядят как закрытые")


class TestTheMemoOnlyNamesWhatExists(unittest.TestCase):
    """Памятка расходится с кодом молча: команда с опечаткой просто ничего не делает."""

    def calls(self):
        out = []
        for body in fences("bash"):
            for line in body.splitlines():
                line = line.split("#")[0].strip()
                if line.startswith("./bin/xmem"):
                    out.append(line.split()[1:])
        return out

    def test_the_memo_uses_the_command_at_all(self):
        self.assertTrue(self.calls(), "рубильники в памятке не названы командой")

    def test_every_switch_named_exists(self):
        for call in self.calls():
            if not call:
                continue
            self.assertIn(call[0], switch.NAMES,
                          "рубильника %s нет: памятка отстала от кода" % call[0])

    def test_every_value_named_is_allowed(self):
        for call in self.calls():
            if len(call) < 2:
                continue
            allowed = switch.SWITCHES[call[0]].choices()
            self.assertIn(call[1], allowed,
                          "значение %s у рубильника %s не принимается: %s"
                          % (call[1], call[0], allowed))


if __name__ == "__main__":
    unittest.main()
