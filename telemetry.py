#!/usr/bin/env python3
"""Трассировка конвейера памяти: кто вызвался, сколько раз, сколько отбросил.

Зачем. Прогон набора говорит «прошло 7 из 100» и молчит о том, где потерялось
остальное. Без разбивки по шагам починка идёт наугад: непонятно, память ничего
не нашла, или нашла и порог всё срезал, или срезал разбор ответа.

Что пишется. Строка JSON на каждый вызов помеченной функции: время, случай
набора, имя функции, порядковый номер вызова, длительность, и обстановка —
только размеры и счётчики, без содержимого.

Личное в журнал не попадает по двум причинам сразу: в обстановку кладутся
числа, а не тексты, и всё, что всё-таки строка, проходит вычистку. Шаблоны
вычистки берутся у пути записи, а не копируются: копия уже разъезжалась, и
разъехалась она молча.

Модуль не тянет проект при импорте — его подключает самый нижний слой, и
зависимость свернулась бы в кольцо. Шаблоны подгружаются при первом вызове,
там кольца уже нет.

Разбор журнала: python3 telemetry.py --log <файл>
"""
import argparse, contextvars, functools, inspect, json, os, re, threading, time, uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.environ.get("MEM_TRACE_LOG") or
           Path.home() / ".local" / "state" / "memory-encoder" / "trace.jsonl")

# Пишем только когда просят. Хук в разговоре не должен платить за замер.
ENABLED = bool(os.environ.get("MEM_TRACE"))

# Один прогон — одна метка. Без неё отчёт складывает сегодняшний прогон
# со вчерашним: журнал дописывается, а разбор читает файл целиком.
RUN_ID = uuid.uuid4().hex[:8]

_TRACE = contextvars.ContextVar("trace", default=None)
_COUNTS = defaultdict(int)
_HANDLE = None
_LOCK = threading.Lock()

# Личное поверх учётных данных. Сами учётные данные берём у пути записи.
PERSONAL = [
    (re.compile(re.escape(str(Path.home()))), "/home/person"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "person@example.org"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "10.0.0.1"),
]
if Path.home().name:
    PERSONAL.append((re.compile(r"\b%s\b" % re.escape(Path.home().name)), "person"))

_SECRETS = None


def _secrets():
    """Шаблоны учётных данных берём у пути записи, чтобы копии не разъезжались.

    Импорт отложен до первого вызова: при импорте модуля он свернулся бы в
    кольцо через xmem, при вызове кольца уже нет.
    """
    global _SECRETS
    if _SECRETS is None:
        try:
            from encoder import SECRETS
            _SECRETS = SECRETS
        except ImportError:
            _SECRETS = []
    return _SECRETS


def scrub(text):
    """Вычистка учётных данных и личного. Порядок шаблонов важен."""
    if not isinstance(text, str):
        return text
    for pat, repl in _secrets():
        text = pat.sub(repl, text)
    for pat, repl in PERSONAL:
        text = pat.sub(repl, text)
    return text


class Trace:
    """Один случай набора. Метка живёт, пока идёт его обработка.

    `phase` разделяет прогон без памяти и прогон с памятью: без него обе
    половины сравнения сваливаются в один журнал и делаются неразличимы.
    """

    def __init__(self, test_id, trace_id=None, phase=""):
        self.test_id = test_id
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.phase = phase
        self.token = None

    def __enter__(self):
        self.token = _TRACE.set(self)
        return self

    def __exit__(self, *exc):
        # Метку гасим один раз. Повторный вход в тот же объект — не повод
        # уронить прогон из середины замера.
        token, self.token = self.token, None
        if token is not None:
            _TRACE.reset(token)
        return False


def current():
    return _TRACE.get()


def _handle():
    global _HANDLE
    if _HANDLE is None:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        _HANDLE = LOG.open("a", encoding="utf-8")
    return _HANDLE


def close():
    """Закрыть журнал. Нужно тем, кто держит процесс долго."""
    global _HANDLE
    handle, _HANDLE = _HANDLE, None
    if handle is not None:
        handle.close()


def emit(name, duration_ms, meta=None, count=None):
    """Одна строка журнала. Обстановка — только числа и короткие метки.

    Строка без метки случая помечается сиротой. Такое бывает, когда шаг ушёл
    в поток: contextvars по потокам не наследуются. Тихо потерять привязку
    хуже, чем громко её назвать.
    """
    if not ENABLED:
        return
    tr = current()
    info = {k: scrub(v) for k, v in (meta or {}).items()}
    if tr is None:
        info["orphan"] = True
    with _LOCK:
        if count is None:
            _COUNTS[name] += 1
            count = _COUNTS[name]
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": RUN_ID,
            "trace_id": tr.trace_id if tr else "",
            "test_id": tr.test_id if tr else "",
            "phase": tr.phase if tr else "",
            "function_name": name,
            "call_count": count,
            "duration_ms": round(duration_ms, 3),
            "metadata": info,
        }
        fh = _handle()
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


def traced(name, meta=None):
    """Пометка шага конвейера.

    `meta` — функция от (аргументы по именам, результат), возвращающая словарь
    чисел. Вызывается только при включённом замере, поэтому в обычной работе
    накладных расходов нет вовсе.
    """
    def wrap(fn):
        signature = None

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            nonlocal signature
            if not ENABLED:
                return fn(*args, **kwargs)
            started = time.perf_counter()
            try:
                out = fn(*args, **kwargs)
            except Exception as exc:
                emit(name, (time.perf_counter() - started) * 1000,
                     {"error": type(exc).__name__})
                raise
            info = {}
            if meta is not None:
                try:
                    # Аргументы отдаём именами, а не позициями: при вызове через
                    # ключевые слова разбор по индексу молча даёт нули, и отсев
                    # в отчёте становится враньём.
                    if signature is None:
                        signature = inspect.signature(fn)
                    bound = signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    info = meta(bound.arguments, out) or {}
                except Exception as exc:
                    info = {"meta_error": type(exc).__name__}
            emit(name, (time.perf_counter() - started) * 1000, info)
            return out
        return inner
    return wrap


# ---------- разбор журнала ----------

def summarize(rows, phase=None, run_id=None):
    """Сводка: вызовы, время, отсев. Отсев — во что превратился вход шага."""
    by_fn = defaultdict(lambda: {"calls": 0, "ms": 0.0, "errors": 0,
                                 "in": 0, "out": 0, "orphans": 0})
    tests = set()
    for row in rows:
        if phase is not None and row.get("phase", "") != phase:
            continue
        if run_id is not None and row.get("run_id", "") != run_id:
            continue
        rec = by_fn[row["function_name"]]
        rec["calls"] += 1
        rec["ms"] += row.get("duration_ms") or 0.0
        meta = row.get("metadata") or {}
        if "error" in meta:
            rec["errors"] += 1
        if meta.get("orphan"):
            rec["orphans"] += 1
        if isinstance(meta.get("in"), int):
            rec["in"] += meta["in"]
        if isinstance(meta.get("out"), int):
            rec["out"] += meta["out"]
        if row.get("test_id"):
            tests.add(row["test_id"])
    return by_fn, tests


def phases(rows):
    """Какие половины сравнения есть в журнале, в порядке появления."""
    out = []
    for row in rows:
        name = row.get("phase", "")
        if name not in out:
            out.append(name)
    return out


def report(rows, phase=None, run_id=None):
    """Отчёт по одному прогону. Без run_id складываются все прогоны в файле."""
    by_fn, tests = summarize(rows, phase, run_id)
    total_ms = sum(r["ms"] for r in by_fn.values())
    shown = [r for r in rows
             if (phase is None or r.get("phase", "") == phase)
             and (run_id is None or r.get("run_id", "") == run_id)]
    lines = ["случаев в журнале: %d, строк: %d" % (len(tests), len(shown)), ""]
    lines.append("%-22s %7s %10s %9s %8s %8s" %
                 ("шаг", "вызовов", "всего мс", "на вызов", "ошибок", "отсев"))
    for name in sorted(by_fn, key=lambda n: -by_fn[n]["ms"]):
        rec = by_fn[name]
        per = rec["ms"] / rec["calls"] if rec["calls"] else 0.0
        if rec["in"]:
            drop = "%.0f%%" % (100.0 * (rec["in"] - rec["out"]) / rec["in"])
        else:
            drop = "—"
        lines.append("%-22s %7d %10.1f %9.3f %8d %8s"
                     % (name, rec["calls"], rec["ms"], per, rec["errors"], drop))
    orphans = sum(r["orphans"] for r in by_fn.values())
    lines.append("")
    lines.append("суммарно в помеченных шагах: %.1f мс" % total_ms)
    if orphans:
        lines.append("строк без метки случая: %d — шаг ушёл в поток, привязка потеряна"
                     % orphans)
    return "\n".join(lines)


def read_log(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Разбор журнала трассировки")
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--run", help="только этот прогон; без него — все, что в файле")
    args = ap.parse_args()
    path = Path(args.log)
    if not path.exists():
        print("нет журнала %s — прогон шёл без MEM_TRACE=1" % path)
        return
    rows = read_log(path)
    runs = []
    for row in rows:
        if row.get("run_id") and row["run_id"] not in runs:
            runs.append(row["run_id"])
    if args.run is None and len(runs) > 1:
        print("в журнале %d прогонов: %s" % (len(runs), ", ".join(runs)))
        print("считаю все вместе; отдельный прогон — ключ --run\n")
    print(report(rows, run_id=args.run))


if __name__ == "__main__":
    main()
