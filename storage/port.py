#!/usr/bin/env python3
"""Единственная дверь наружу: всё общение с xmemory идёт только отсюда.

Дверь не одна на всех, а по ролям. Роль это то, что дверь умеет:

  write(text)              текстовая запись — умеют все
  read(query, mode)        чтение — умеют все
  write_objects(records)   структурная запись, где ключ задан полем
  neighbours(keys)         шаг по связям: факты, связанные с названными
  fold(now)                свёртка дублей и разворот обратно
  contexts(keys)           обстановка фактов: где и когда их видели
  slice(axes)              срез фактов по осям обстановки

За дверью четыре пути, они выбираются именем:

  cli    консольная утилита xmemcli. Без ключа доступа, но только текстом:
         ключ выводит экстрактор, и при промахе записи схлопываются на одну
         строку. Значение по умолчанию — так было до сих пор.
  api    прямой HTTP. Структурная запись есть.
  sdk    официальный клиент для Python. То же, что api, но соединение и
         разбор ответа держит он.
  local  SQLite рядом с проектом. Ни ключа, ни сети, ни квоты, но и читателя
         нет: на запрос приходят найденные записи, а не пересказ.

У консольной двери метода write_objects **нет**. Раньше он был и падал явной
ошибкой, а вызывающий ловил её и сверял имя пути с "cli". Так подстановка не
работала: контракт двери был шире, чем может выполнить один из путей, и
добавление пятого пути без структурной записи молча роняло разговор. Теперь
вызывать нечего, и проверять надо не имя, а умение: hasattr(door, ...).

Имя пути читается **при вызове** door(), а не при импорте модуля. Из
импортного выбора росли обе беды сразу: моки в тестах вместо подстановки и
importlib.reload в matrix — где два сетевых пути перезагружали, а локальный
забыли, и его кэш соединения жил насквозь через обе половины сравнения.
"""
import os
import subprocess

from infra import telemetry

# Консоль называет режимы чтения короче, чем сервис.
READ_MODES = {"single": "single-answer", "raw": "raw-tables", "xresponse": "xresponse"}

# Linux не пропускает один аргумент длиннее 128 КиБ (MAX_ARG_STRLEN).
# Берём с запасом, потому что кириллица это два байта на символ.
MAX_ARG_BYTES = 100_000


class BackendError(RuntimeError):
    """Путь наружу не может выполнить то, о чём его просят."""


def _read_meta(arg, out):
    return {"backend": arg["self"].name, "mode": arg["mode"],
            "query_chars": len(arg["query"] or ""), "answer_chars": len(out or "")}


def _split(text):
    """Длинную запись режем на части по границе байтов. Ничего не теряем."""
    data = text.encode("utf-8")
    if len(data) <= MAX_ARG_BYTES:
        return [text]
    parts, start = [], 0
    while start < len(data):
        chunk = data[start:start + MAX_ARG_BYTES]
        # не рвём символ пополам
        while chunk:
            try:
                parts.append(chunk.decode("utf-8"))
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        start += len(chunk)
    total = len(parts)
    return ["[часть %d из %d] %s" % (i + 1, total, p) for i, p in enumerate(parts)]


class TextDoor:
    """Консоль. Текстовая запись и чтение — и ничего больше.

    Метода write_objects здесь нет намеренно, см. заголовок модуля.
    """

    name = "cli"

    def __init__(self, instance=None):
        # Идентификатор хранилища берём из окружения, в коде его быть не должно.
        # Задать можно так: export XMEM_INSTANCE_ID=<id>
        self.instance = instance if instance is not None else os.environ.get("XMEM_INSTANCE_ID", "")

    def _run(self, args, timeout=180):
        if not self.instance:
            raise RuntimeError("не задан XMEM_INSTANCE_ID: укажи хранилище в окружении")
        env = dict(os.environ, XMEM_INSTANCE_ID=self.instance)
        proc = subprocess.run(["xmemcli"] + args, capture_output=True, text=True,
                              env=env, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError("xmemcli %s: %s" % (args[0], (proc.stderr or proc.stdout)[:400]))
        return proc.stdout.strip()

    def write(self, text, wait=False):
        """Ключ выводит экстрактор, см. предупреждение вверху."""
        out = []
        # Ограничение на длину аргумента — свойство консоли, а не сервиса.
        for part in _split(text):
            args = ["write", part]
            if not wait:
                args.append("--no-wait")
            out.append(self._run(args))
        return out[0] if len(out) == 1 else "частей %d" % len(out)

    @telemetry.traced("retrieve", _read_meta)
    def read(self, query, mode="single"):
        """Всегда строка: вызывающие разбирают ответ текстом, см. suggest.pieces."""
        return self._run(["read", query, "--read-mode", mode])


class StructuredDoor:
    """Путь, умеющий все три роли: api, sdk, local."""

    def __init__(self, adapter, name):
        self.adapter, self.name = adapter, name

    def write(self, text, wait=False):
        return self.adapter.write_text(text, wait=wait)

    def write_objects(self, records, relations=(), op="create"):
        """Записи схемы и связи между ними.

        Принимает объекты из models. Порядок сохраняется: связь применяется
        после объектов, которые она соединяет, поэтому их можно слать одним
        вызовом.

        `op` выбирает вид мутации. Обновление адресует лежащую строку и несёт
        только изменившееся — им продлевается срок факта, и требовать от такой
        записи полного набора полей значило бы читать факт целиком ради одного
        значения.
        """
        mutations = [r.mutation(op) for r in records] + list(relations)
        return self.adapter.write_objects(mutations)

    @telemetry.traced("retrieve", _read_meta)
    def read(self, query, mode="single"):
        return self.adapter.read(query, mode=READ_MODES.get(mode, mode))

    def neighbours(self, keys, limit=10):
        """Шаг по графу связей. Умеют все наши пути: и база, и сеть.

        Спрашиваем адаптер, а не имя двери: чужой путь, обхода не умеющий,
        должен работать молча и как раньше. Своих таких больше нет — сетевые
        ходят по графу через `storage.graph`, — но правило остаётся правилом:
        имя пути ничего не говорит о том, что путь умеет.
        """
        step = getattr(self.adapter, "neighbours", None)
        if step is None:
            raise AttributeError("путь %s обхода по графу не умеет" % self.name)
        return step(keys, limit=limit)

    def lapse(self, now, dry=False):
        """Переклад просроченного в отложенное. Умеет не всякий путь наружу.

        Спрашиваем адаптер, а не имя двери: у сетевого читателя выборки по
        сроку нет, и забывание не имеет права ронять на нём ход.
        """
        step = getattr(self.adapter, "lapse", None)
        if step is None:
            raise AttributeError("путь %s забывать не умеет" % self.name)
        return step(now, dry=dry)

    def fold(self, now, dry=False):
        """Свёртка дублей. Умеет не всякий путь наружу.

        Спрашиваем адаптер, а не имя двери: у сетевого читателя выборки по
        содержанию нет, и свёртка не имеет права ронять на нём ход.
        """
        step = getattr(self.adapter, "fold", None)
        if step is None:
            raise AttributeError("путь %s сворачивать не умеет" % self.name)
        return step(now, dry=dry)

    def folded(self, identity):
        """Из чего собрана замена. Обратная сторона свёртки."""
        step = getattr(self.adapter, "folded", None)
        if step is None:
            raise AttributeError("путь %s свёртки не помнит" % self.name)
        return step(identity)

    def unfold(self, identity):
        """Разворот свёртки: исходные возвращаются в живую таблицу."""
        step = getattr(self.adapter, "unfold", None)
        if step is None:
            raise AttributeError("путь %s разворачивать не умеет" % self.name)
        return step(identity)

    def contexts(self, keys):
        """Обстановки фактов. Умеет не всякий путь наружу.

        Спрашиваем адаптер, а не имя двери: у сетевого читателя такой выборки
        нет, и уместность обязана считаться без него — по тому, что лежит в
        самой записи. Отсутствие обхода не должно ронять подсказку.
        """
        step = getattr(self.adapter, "contexts", None)
        if step is None:
            raise AttributeError("путь %s обстановки факта не читает" % self.name)
        return step(keys)

    def slice(self, axes, limit=200):
        """Срез фактов по осям обстановки. Умеет не всякий путь наружу."""
        step = getattr(self.adapter, "slice_by", None)
        if step is None:
            raise AttributeError("путь %s срезов по обстановке не умеет" % self.name)
        return step(axes, limit=limit)

    def state(self, as_of=None):
        """На каком состоянии хранилища идёт прогон. Умеет не всякий путь наружу.

        Спрашиваем адаптер, а не имя двери: у сетевого читателя такой выборки
        нет, а замер обязан работать и на нём — просто скажет меньше.
        """
        step = getattr(self.adapter, "state", None)
        if step is None:
            raise AttributeError("путь %s о своём состоянии не рассказывает" % self.name)
        return step(as_of)

    def deep(self, query, limit=10):
        """Глубокое чтение: к найденному добавляется отложенное."""
        step = getattr(self.adapter, "deep", None)
        if step is None:
            raise AttributeError("путь %s глубокого чтения не умеет" % self.name)
        return step(query, limit=limit)


class SilentDoor:
    """Память выключена целиком: чтение отдаёт пустоту, запись молча гаснет.

    Умеет все роли: половина сравнения «без памяти» не должна отличаться от
    рабочей ничем, кроме результата, иначе выбор запасного пути в save начнёт
    зависеть от того, включена ли память.
    """

    name = "off"

    def write(self, text, wait=False):
        return ""

    def write_objects(self, records, relations=(), op="create"):
        return None

    @telemetry.traced("retrieve", _read_meta)
    def read(self, query, mode="single"):
        return ""


def _api():
    from storage import api
    return api


def _sdk():
    from storage import sdk
    return sdk


def _local():
    from storage import local
    return local


ADAPTERS = {"api": _api, "sdk": _sdk, "local": _local}


def store_name(backend=None, disabled=None):
    """Как зовут путь наружу. Тем же правилом, каким его выбирает door().

    Нужно книжкам учёта обоих проходов: отметка «докуда разобрано» принадлежит
    хранилищу, и выключенная память — такое же хранилище, как остальные. Считай
    её локальной — и половина сравнения «без памяти» закроет архив, не записав
    ни строчки, а настоящий локальный проход придёт к дочитанному.
    """
    if disabled is None:
        disabled = bool(os.environ.get("XMEM_DISABLED"))
    if disabled:
        return SilentDoor.name
    return (backend or os.environ.get("XMEM_BACKEND") or "cli").strip().lower() or "cli"


def door(backend=None, disabled=None):
    """Путь наружу. Имя из аргумента, иначе из окружения — при вызове, не при импорте.

    Пустая переменная в окружении значит «не задано», см. .env.example.
    """
    if disabled is None:
        disabled = bool(os.environ.get("XMEM_DISABLED"))
    if disabled:
        return SilentDoor()
    name = (backend or os.environ.get("XMEM_BACKEND") or "cli").strip().lower()
    if name == "cli":
        return TextDoor()
    if name in ADAPTERS:
        return StructuredDoor(ADAPTERS[name](), name)
    raise BackendError("неизвестный XMEM_BACKEND: %r, допустимо cli, %s"
                       % (name, ", ".join(sorted(ADAPTERS))))


def close_all():
    """Закрыть всё, у чего есть что закрывать.

    Спрашиваем каждый путь, а не перечисляем руками: перечисление уже забыло
    storage.local, и его кэш соединения протекал между половинами сравнения.
    Загружаем адаптер, только если он уже был загружен: поднимать sqlite ради
    того, чтобы его закрыть, незачем.
    """
    import sys
    for name in sorted(ADAPTERS):
        module = sys.modules.get("storage." + name)
        shut = getattr(module, "close", None) if module is not None else None
        if shut is not None:
            shut()
