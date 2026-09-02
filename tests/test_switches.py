#!/usr/bin/env python3
"""Реестр рубильников. Запуск: python3 -m unittest tests.test_switches -v

Проверки отвечают на три вопроса:

1. Реестр — реестр, а не набор веток: новый рубильник добавляется одной
   записью, и ни разбор аргументов, ни вывод состояния этого не замечают.
2. Переставленный рубильник действительно долетает до того, кто его читает —
   до `infra/config`, до ворот на bash и до хука печати. Команда, которая
   пишет файл, но ничего не меняет в поведении, выглядит точно так же.
3. Состояние говорит, **откуда** взято значение. Переменная окружения сильнее
   файла, и молчание об этом уже стоило нам дня разбирательств.
"""
import contextlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from infra import config
from pipeline import switch

HERE = Path(__file__).resolve().parent.parent
HOOKS = HERE / "hooks"


@contextlib.contextmanager
def aside(tmp):
    """Увести состояние в сторону — и убедиться, что увелось.

    Одной подмены мало. Эти проверки пишут файлы рубильников: сломайся сам
    рубильник каталога — и они напишут их в живое состояние человека, молча и
    все разом. Ровно так и вышло на прогоне мутации: в живом каталоге завелись
    `probe`, `backend` и `memory`, а путь наружу уехал в сеть. Поэтому уводим
    и сразу проверяем, что увели.
    """
    with mock.patch.dict(os.environ, {"XMEM_STATE_DIR": str(tmp)}):
        if config.state_dir() != Path(tmp):
            raise AssertionError(
                "рубильник каталога не сработал: %s вместо %s — проверка писала "
                "бы в живое состояние" % (config.state_dir(), tmp))
        yield


class TestRegistryIsARegistry(unittest.TestCase):
    """Устройство то же, что у правил извлечения и схем разметки."""

    def test_every_name_has_an_entry_and_back(self):
        self.assertEqual(sorted(switch.SWITCHES), sorted(switch.NAMES))

    def test_a_new_switch_is_one_entry(self):
        """Мутация наоборот: добавляем запись и водим её общей командой.

        Ни `main`, ни `status` при этом не правятся — если бы правились, этот
        тест не прошёл бы, не тронув их.
        """
        with tempfile.TemporaryDirectory() as tmp:
            new = switch.Switch("пробный", "проба", "XMEM_PROBE", "probe", "нет",
                                values=("да", "нет"))
            with aside(tmp), \
                 mock.patch.dict(switch.SWITCHES, {"probe": new}), \
                 mock.patch.object(switch, "NAMES", switch.NAMES + ["probe"]):
                self.assertEqual(switch.main(["probe", "да"]), 0)
                self.assertEqual(new.read()[0], "да")
                self.assertIn("пробный", switch.status())
                self.assertEqual(switch.main(["probe", "мимо"]), 1)

    def test_unknown_value_is_refused_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with aside(tmp):
                with self.assertRaises(ValueError):
                    switch.SWITCHES["backend"].set("почтой")
                self.assertFalse((Path(tmp) / "backend").exists())


class TestSwitchReachesTheOneWhoReadsIt(unittest.TestCase):
    """Файл написан — этого мало. Читатель должен увидеть новое значение."""

    def test_marks_switch_changes_what_config_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            with aside(tmp):
                switch.SWITCHES["marks"].set("show")
                self.assertFalse(config.hide_marks())
                switch.SWITCHES["marks"].set("hide")
                self.assertTrue(config.hide_marks())
                switch.SWITCHES["marks"].set("default")
                self.assertTrue(config.hide_marks(), "умолчание должно срезать")

    def test_marks_switch_reaches_the_display_hook(self):
        """Сквозная: команда пишет файл, хук печати его слушается.

        Слово команды (`show`) и слово руки (`0`) должны пониматься оба: файл
        пишет команда, окружение ставит человек, и рубильник, понимающий
        только одну привычку, срабатывает через раз.
        """
        payload = json.dumps({"turn_id": "t", "message_id": "sw1", "index": 1,
                              "final": False, "delta": "Готово.\n<<<MEMORY-FACTS\n"})
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".local/state/memory-encoder"
            state.mkdir(parents=True)
            env = dict(os.environ, HOME=tmp, XMEM_LIVE="1", PYTHONPATH=str(HERE))
            env.pop("XMEM_HIDE_MARKS", None)

            def run():
                return subprocess.run(["bash", str(HOOKS / "on_message_display.sh")],
                                      input=payload, env=env, capture_output=True,
                                      text=True, timeout=60, cwd=str(HERE)).stdout.strip()

            self.assertIn("displayContent", run(), "по умолчанию блок не срезается")
            (state / "hide-marks").write_text("show\n")
            self.assertEqual(run(), "", "команда сказала показывать, хук всё режет")
            (state / "hide-marks").write_text("hide\n")
            self.assertIn("displayContent", run(), "хук перестал срезать вовсе")

    def test_here_switch_opens_and_closes_the_gate(self):
        """Ворота спрашиваем у самих ворот: своей копии проверки тут нет."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "проект"
            work.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            env = dict(os.environ, HOME=tmp)
            env.pop("XMEM_LIVE", None)

            def gate():
                return subprocess.run(["bash", str(HOOKS / "gate.sh"), str(work)],
                                      env=env, capture_output=True,
                                      text=True, timeout=30).returncode

            self.assertNotEqual(gate(), 0, "без списка ворота обязаны молчать")
            live = Path(tmp) / ".local/state/memory-encoder"
            live.mkdir(parents=True, exist_ok=True)
            (live / "live-projects").write_text("%s\n" % work)
            self.assertEqual(gate(), 0, "каталог в списке, а ворота закрыты")


class TestStatusNamesItsSource(unittest.TestCase):
    """Значение без источника — половина ответа."""

    def test_environment_is_named_and_marked_as_stronger(self):
        with tempfile.TemporaryDirectory() as tmp:
            with aside(tmp):
                switch.SWITCHES["backend"].set("sdk")
                value, where = switch.SWITCHES["backend"].read()
                self.assertEqual((value, where), ("sdk", "файл backend"))
                with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                    value, where = switch.SWITCHES["backend"].read()
                    self.assertEqual(value, "local")
                    self.assertIn("XMEM_BACKEND", where)
                    self.assertIn("окружение сильнее",
                                  switch.SWITCHES["backend"].shadowed())

    def test_status_says_whether_memory_is_live_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            with aside(tmp), \
                 mock.patch.object(switch, "live_here", lambda where=None: False):
                text = switch.status(tmp)
        self.assertIn("память здесь: молчит", text)
        for name in switch.NAMES:
            self.assertIn(switch.SWITCHES[name].name, text)


if __name__ == "__main__":
    unittest.main()
