#!/usr/bin/env python3
"""Рубильники: реестр, а не набор команд.

Переключателей у контура уже пять, и будет больше: каждая новая механика
приносит свой. Писать под каждый разбор аргументов и свою ветку в теле — тот
самый путь, которым здесь уже ходили дважды (лесенка из четырёх `if` в
извлечении фактов, ветка на вендора в записи). Поэтому рубильник — запись в
реестре, а команда одна на всех: показать и переставить.

Добавить рубильник значит добавить одну запись в `SWITCHES` и одно имя в
`NAMES`. Ни разбор аргументов, ни вывод состояния при этом не правятся.

Каждый рубильник обязан уметь сказать **откуда взято текущее значение**. Это
не украшение: окружение сильнее файла, и переменная, выставленная в профиле,
отменяет любую правку файла молча. Молчаливо неверное состояние уже стоило
нам дня — точки в настройках пустовали, а выглядело это как исправный контур,
которому нечего сказать.

Запуск:

    python3 -m pipeline.switch                 всё состояние разом
    python3 -m pipeline.switch marks           один рубильник
    python3 -m pipeline.switch marks show      переставить
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from domain import lifespan, marks
from infra import config
from pipeline import voice

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "hooks" / "gate.sh"


class Switch:
    """Рубильник, чьё значение лежит строкой в файле состояния.

    Значение читается по общему порядку силы: окружение, файл, умолчание.
    Записать можно только файл — переменную окружения дочерний процесс
    родителю не поставит, и делать вид, что поставил, нельзя.
    """

    def __init__(self, name, about, env, file_name, default, values=None,
                 explain=None):
        self.name = name
        self.about = about
        self.env = env
        self.file_name = file_name
        self.default = default
        self.values = tuple(values or ())
        self.explain = explain or {}

    # --- чтение ---

    def read(self):
        """Пара «значение, откуда взято». Второе важнее первого."""
        got = (os.environ.get(self.env) or "").strip() if self.env else ""
        if got:
            return got, "окружение (%s)" % self.env
        got = self._file_value()
        if got:
            return got, "файл %s" % self.file_name
        return self.default, "умолчание"

    def _file_value(self):
        try:
            return (config.state_dir() / self.file_name).read_text(
                encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def show(self):
        value, where = self.read()
        line = "%-24s %-10s ← %s" % (self.name, value, where)
        note = self.explain.get(value)
        return line + ("\n%-24s %s" % ("", note) if note else "")

    def shadowed(self):
        """Правка файла ничего не изменит, пока окружение говорит своё."""
        got = (os.environ.get(self.env) or "").strip() if self.env else ""
        return ("окружение сильнее: %s=%s, значение файла не действует"
                % (self.env, got)) if got else ""

    # --- запись ---

    def choices(self):
        return self.values + ("default",) if self.values else ("default",)

    def set(self, value):
        """Переставить рубильник. `default` означает «убрать файл»."""
        if value == "default":
            self._drop()
            return "%s: вернулся к умолчанию (%s)" % (self.name, self.default)
        if self.values and value not in self.values:
            raise ValueError("%s: %r не из %s"
                             % (self.name, value, ", ".join(self.values)))
        self._write(value)
        return "%s: %s" % (self.name, value)

    def _write(self, value):
        config.state_dir().mkdir(parents=True, exist_ok=True)
        (config.state_dir() / self.file_name).write_text("%s\n" % value,
                                                       encoding="utf-8")

    def _drop(self):
        try:
            (config.state_dir() / self.file_name).unlink()
        except OSError:
            pass


class Presence(Switch):
    """Рубильник, у которого значение — само наличие файла.

    Так устроен общий выключатель: `off` есть — молчат все хуки. Значение в
    таком файле никто не читает, и заводить его смысла нет.
    """

    def read(self):
        got = (os.environ.get(self.env) or "").strip() if self.env else ""
        if got:
            return ("off" if got in ("0", "no", "off") else "on",
                    "окружение (%s)" % self.env)
        if (config.state_dir() / self.file_name).exists():
            return "off", "файл %s" % self.file_name
        return self.default, "умолчание"

    def set(self, value):
        if value == "default":
            value = self.default
        if value not in self.values:
            raise ValueError("%s: %r не из %s"
                             % (self.name, value, ", ".join(self.values)))
        if value == "off":
            config.state_dir().mkdir(parents=True, exist_ok=True)
            (config.state_dir() / self.file_name).touch()
            return "%s: выключено, молчат все хуки во всех проектах" % self.name
        self._drop()
        return "%s: включено, дальше решают ворота" % self.name


class Listing(Switch):
    """Рубильник-список: строка на запись. Так задаются живые каталоги.

    Значение здесь — не одно слово, поэтому переставляется не заменой, а
    добавлением и удалением: `add` кладёт текущий каталог, `remove` убирает.
    """

    def items(self):
        try:
            lines = (config.state_dir() / self.file_name).read_text(
                encoding="utf-8").splitlines()
        except OSError:
            return []
        return [line.strip() for line in lines
                if line.strip() and not line.strip().startswith("#")]

    def read(self):
        known = self.items()
        if not known:
            return "пусто", "файл %s" % self.file_name
        return "%d каталог(ов)" % len(known), "файл %s" % self.file_name

    def show(self):
        known = self.items()
        head = Switch.show(self)
        if not known:
            return head + "\n%-24s %s" % ("", "список пуст — память молчит везде")
        return head + "".join("\n%-24s %s" % ("", p) for p in known)

    def set(self, value):
        here = str(Path.cwd())
        known = self.items()
        if value == "add":
            if here in known:
                return "%s: уже в списке %s" % (self.name, here)
            self._write("\n".join(known + [here]))
            return "%s: добавлено %s" % (self.name, here)
        if value == "remove":
            left = [p for p in known if p.rstrip("/") != here.rstrip("/")]
            if left:
                self._write("\n".join(left))
            else:
                self._drop()
            return "%s: убрано %s" % (self.name, here)
        raise ValueError("%s: %r не из %s"
                         % (self.name, value, ", ".join(self.values)))


# Имена в порядке добавления, как в реестрах правил и схем. Проверка сверяется
# с этим списком: запись без имени работать не должна.
NAMES = ["live", "here", "backend", "scheme", "marks", "memory", "voice"]

SWITCHES = {
    "live": Presence(
        "живая ли память", "общий выключатель всех хуков",
        "XMEM_LIVE", "off", "on", values=("on", "off")),
    "here": Listing(
        "живые каталоги", "где память работает",
        None, "live-projects", "пусто", values=("add", "remove")),
    "backend": Switch(
        "путь наружу", "куда пишет ход",
        "XMEM_BACKEND", "backend", "local", values=("local", "sdk", "cli"),
        explain={"local": "локальная база, сеть в горячем пути не трогается"}),
    "scheme": Switch(
        "схема разметки", "просьба к модели и маппер, парой",
        "XMEM_MARKS", "marks", config.DEFAULT_MARKS, values=tuple(marks.NAMES)),
    "marks": Switch(
        "блок на экране", "срезать служебный блок или показывать",
        "XMEM_HIDE_MARKS", "hide-marks", "hide", values=("hide", "show"),
        explain={"show": "блок виден человеку, разбор при этом работает как работал"}),
    "memory": Switch(
        "режим памяти", "сколько факт верен без обращений",
        "XMEM_MEMORY", "memory", config.DEFAULT_MEMORY,
        values=tuple(sorted(lifespan.MODES, key=lambda n: lifespan.MODES[n])),
        explain={name: "%d дней, дальше факт уходит в отложенное" % span
                 for name, span in lifespan.MODES.items()}),
    "voice": Switch(
        "форма вброса", "чем найденное подаётся агенту",
        "XMEM_VOICE", "voice", config.DEFAULT_VOICE,
        values=tuple(voice.SHIPPED),
        explain={"plain": "справка блоком: факт, уверенность, обстановка",
                 "directive": "указание вместо справки, без наших чисел",
                 "inline": "одной строкой рядом с задачей"}),
}


def live_here(where=None):
    """Ворота спрашиваем у самих ворот, а не переписываем их проверку.

    Копия разошлась бы с оригиналом в первый же день: пути там сравниваются
    настоящие, а не строки, и на macOS это уже ловило нас с `/tmp`.
    """
    try:
        got = subprocess.run(["bash", str(GATE), str(where or Path.cwd())],
                             capture_output=True, text=True, timeout=30)
        return got.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def status(where=None):
    """Всё состояние разом: что включено, где и откуда взято."""
    here = Path(where or Path.cwd())
    alive = live_here(here)
    lines = ["каталог: %s" % here,
             "память здесь: %s" % {True: "живая", False: "молчит",
                                   None: "не удалось спросить ворота"}[alive]]
    lines += [SWITCHES[name].show() for name in NAMES]
    lines.append("состояние: %s" % config.state_dir())
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xmem", description=__doc__.splitlines()[0])
    ap.add_argument("name", nargs="?", choices=NAMES,
                    help="рубильник; без имени — всё состояние")
    ap.add_argument("value", nargs="?", help="новое значение; без него — текущее")
    args = ap.parse_args(argv)

    if not args.name:
        print(status())
        return 0
    switch = SWITCHES[args.name]
    if not args.value:
        print(switch.show())
        print("значения: %s" % ", ".join(switch.choices()))
        return 0
    try:
        print(switch.set(args.value))
    except ValueError as bad:
        print(bad, file=sys.stderr)
        return 1
    warning = switch.shadowed()
    if warning:
        print(warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
