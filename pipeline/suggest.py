#!/usr/bin/env python3
"""Модуль 3, Подсказка. Спрашивает xmemory и отдаёт агенту то, что прошло порог.

Своей головы у модуля нет: ценность оценивает понимание, здесь только порог.
Адресат подсказки это агент, а не человек, поэтому лишний текст не просто
бесполезен, он засоряет контекст агента и уводит его в сторону. Молчание это
нормальный и частый исход.
"""
import argparse, ast, json, os, re, signal, sys
from datetime import datetime, timezone
from pathlib import Path

from domain import context, ledger, lifespan, marks, models
from domain.query import words
from infra import telemetry
from pipeline import prompt
from storage import port

LOG = Path.home() / ".local" / "state" / "memory-encoder" / "suggest-log.jsonl"

# Срок в горячем пути. Держим его здесь, а не внешней командой: timeout(1)
# есть не везде, и его отсутствие выглядело как молчаливый успех.
HOOK_SECONDS = int(os.environ.get("XMEM_HOOK_SECONDS") or 10)


class Overdue(Exception):
    """Срок вышел. Подсказка опоздала и потому больше не нужна."""


def deadline(seconds=None):
    """Прервать себя по истечении срока. Возвращает функцию отмены.

    SIGALRM есть не на всякой платформе; там, где его нет, работаем без
    срока — это хуже, чем со сроком, но лучше, чем не работать вовсе.
    """
    if not hasattr(signal, "SIGALRM"):
        return lambda: None

    def ring(signum, frame):
        raise Overdue()

    was = signal.signal(signal.SIGALRM, ring)
    signal.alarm(seconds if seconds is not None else HOOK_SECONDS)

    def cancel():
        signal.alarm(0)
        signal.signal(signal.SIGALRM, was)
    return cancel

MIN_SCORE = 0.5      # ниже этого факт не подтверждён повторением, см. ADR 0002
MIN_FIT = 0.5        # ниже этого запись не к месту, см. ADR 0009
MAX_ITEMS = 5        # больше пяти внимание модели размазывается
MAX_CHARS = 1200     # потолок на весь кусок, который уходит в контекст агента

SCORE = re.compile(r"Оценка уверенности:\s*([0-9]+(?:\.[0-9]+)?)")

# Служебные поля записи: агенту не говорят ничего, а потолок съедают.
NOISE = ("first_seen_at", "observed_at", "created_at", "updated_at", "id",
         "object_type", "via_graph", "sequence_number", "session_id",
         "fact_type", "scope", "subject", "valid_until", "lapsed_at",
         # Обстановка и уместность печатаются отдельной строкой, разобранными
         # по осям. Свались они сюда сырым словарём — потолок выдачи съеден,
         # а прочесть это модели тяжелее, чем не иметь вовсе.
         "situation", "fit")


def _parse(text):
    """Строка приходит и как JSON, и как питонье представление. Пробуем оба."""
    for reader in (json.loads, ast.literal_eval):
        try:
            return reader(text)
        except (ValueError, SyntaxError):
            continue
    return None


def _unwrap(answer):
    """Разворачиваем ответ до содержимого.

    Хранилище отдаёт `{"answer": "<строка>"}`, и в этой строке лежит ещё один
    список или запись. Пока не развернуть, шесть фактов остаются одним куском:
    порог видит один кусок без оценки и выбрасывает разом всё.
    """
    body = answer
    for _ in range(4):
        if isinstance(body, dict) and "answer" in body:
            body = body["answer"]
            continue
        if not isinstance(body, str):
            break
        text = body.strip()
        if not text or text[0] not in "[{":
            break
        parsed = _parse(text)
        if parsed is None:
            break
        body = parsed
    return body


def _text(chunk):
    """Запись превращаем в строку, не теряя полей.

    Раньше брали только `content`, и запись без него схлопывалась в `str(dict)`.
    Вместе с ней пропадали поля вроде `git_branch` — то самое, о чём спрашивал
    вопрос.
    """
    if not isinstance(chunk, dict):
        return str(chunk)
    parts = []
    if chunk.get("content"):
        parts.append(str(chunk["content"]))
    # У факта весь смысл в тексте: подпись, охват и вид дословно повторяют его
    # же содержание, а на потолок в 1200 символов это втрое дороже. У эпизода
    # наоборот — там ветка и исход, и терять их нельзя, на этом уже спотыкались.
    if chunk.get("object_type") == "Fact" and parts:
        return parts[0]
    for key, value in chunk.items():
        if key == "content" or key in NOISE:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        parts.append("%s: %s" % (key, value))
    return ". ".join(parts) if parts else str(chunk)


@telemetry.traced("parse_answer", lambda arg, out: {
    "answer_chars": len(arg["answer"] or ""), "out": len(out),
    "scored": sum(1 for row in out if row[0] is not None)})
def pieces(answer):
    """Ответ памяти разбираем на куски и достаём оценку каждого."""
    body = _unwrap(answer)
    # Признак «это запись, а не слова читателя» ставится каждому куску отдельно.
    # На контейнер его ставить нельзя: список голых строк тоже список, и тогда
    # «no matching files» внутри списка проходит порог как факт.
    if isinstance(body, list):
        chunks = [(_text(c), c if isinstance(c, dict) else None) for c in body]
    elif isinstance(body, dict):
        chunks = [(_text(body), body)]
    else:
        chunks = [(c, None) for c in re.split(r"\n\s*\n", str(body))]
    out = []
    for chunk, record in chunks:
        text = chunk.strip()
        if not text:
            continue
        found = SCORE.search(text)
        # Саму запись несём дальше, а не только её текст: по ней вставка потом
        # называет свои источники. Ключ, до которого нельзя дотянуться, в связь
        # не поставить — это уже проходили на фактах.
        out.append((float(found.group(1)) if found else None, text, record))
    return out


# Где событие случилось, а не что оно говорит. Слово вопроса, попавшее только
# сюда, находкой не является: событий одного проекта в архиве десятки тысяч, и
# любое из них подошло бы так же.
PLACE = ("project", "working_directory", "session_id")

# Чем событие является: инструмент, ветка, вид, время. Сюда вопрос обращается
# по делу — «что делали в ветке X», «что запускали в среду».
DEED = ("tool_name", "git_branch", "event_type", "occurred_at", "day_of_week")

# Сырьё. У факта и эпизода есть своё утверждение, у события — только дословный
# текст, который оно несёт: команда, её вывод, реплика.
RAW_MATERIAL = ("Event",)

# Вопросы, которые дословного как раз и просят. «Какой командой это чинили» —
# это ровно событие, и отсеивать его нельзя: отсев режет случайное совпадение,
# а не вид записи. Список собран руками, как и STOP.
VERBATIM = ("команд", "запуск", "запусти", "выполн", "терминал", "консол",
            "вывод", "показал", "ошибк", "traceback", "bash", "shell",
            "command", "output", "error", "stderr", "stdout")

# Разделители, которых в обычном слове не бывает: они выдают имя — путь, файл,
# проект, ветку. Просьбой о дословном имя не бывает никогда. Без этой оговорки
# вопрос про `errors.py`, проект `bash-tools` или ветку `error-handler` выключал
# бы отсев целиком и молча: сравнение шло подстрокой по всему вопросу.
NAMEISH = ".:/-"


def terms_of(query):
    """Слова вопроса. Тем же правилом, каким их берёт поиск, см. domain/query."""
    return words(query)


def asks_verbatim(query):
    """Вопрос просит дословного: команду, её вывод, ошибку.

    Смотрим на слова вопроса, а не на его текст. Подстрока по всему вопросу
    ошибалась в обе стороны сразу: имя файла `errors.py` включало исключение и
    выключало отсев целиком, а сказать об этом было некому.
    """
    return any(word.startswith(mark)
               for word in terms_of(query)
               if not any(sign in word for sign in NAMEISH)
               for mark in VERBATIM)


def incidental(record, terms, verbatim=False):
    """Ложная находка: событие, зацепившееся за вопрос ничем.

    Ничем — значит только именем места, где оно случилось, или словом внутри
    дословного тела, которое оно несёт. Слово из вопроса случайно встретилось
    внутри команды, которую агент когда-то выполнил; формально совпадение есть,
    знания нет. Такой кусок съедает потолок выдачи и уводит агента в сторону.

    Отсев режет совпадение, а не вид. Событие доходит до агента, когда вопрос
    обратился к нему по делу — назвал инструмент, ветку, время, — и когда сам
    вопрос просит дословного: «какой командой это чинили» это ровно событие.

    Факт и эпизод не трогаем никогда: у них есть собственное утверждение, и имя
    проекта в нём — законная тема, а не случайная зацепка.
    """
    if not isinstance(record, dict):
        return False
    if record.get("object_type") not in RAW_MATERIAL or verbatim:
        return False
    place = " ".join(str(record.get(name) or "") for name in PLACE).lower()
    deed = " ".join(str(record.get(name) or "") for name in DEED).lower()
    # Слово, объяснимое именем места, обращением по делу не считается: ветка
    # почти всегда носит имя проекта, и попадание в неё — то же совпадение,
    # что и попадание в сам проект.
    return not any(term in deed and term not in place for term in terms)


@telemetry.traced("sift_incidental", lambda arg, out: {
    "in": len(arg["items"]), "out": len(out)})
def sift(items, query):
    """Убрать ложные находки. Всё остальное — порядок, оценки — как было.

    Стоит между разбором ответа и порогом: порог считает уверенность, а тут
    решается, знание ли это вообще. Вопрос без единого своего слова отсеивать
    нечем — такой ответ проходит целиком.
    """
    terms = terms_of(query)
    if not terms:
        return list(items)
    verbatim = asks_verbatim(query)
    return [row for row in items
            if not incidental(row[2] if len(row) > 2 else None, terms, verbatim)]


def knowledge(answer, query):
    """Сырой ответ хранилища без ложных находок и число отсеянных кусков.

    Этим спрашивает замер. Найденным считается то, что вообще могло дойти до
    агента: иначе «нашли» считает подстроки, «отдали» — знание, и разрыв между
    ними меряет две разные вещи. Одно правило на обе стороны, разойтись нечему.

    Число отсеянного отдаётся вместе с текстом нарочно. Кусок, срезанный зря,
    записывается в мусор, а не в потерю: по одному итогу перетянутый отсев
    выглядит улучшением. Пусть объём среза будет виден отдельным числом.
    """
    chunks = pieces(answer)
    kept = sift(chunks, query)
    return ("\n".join(text for _score, text, _record in kept),
            len(chunks) - len(kept))


def situation_of(payload, at=None):
    """Обстановка хода из того, что уже приходит в хук. Ни сети, ни модели.

    До сих пор из payload брались только `prompt` и `session_id`, а `cwd` и
    `permission_mode` выбрасывались — при том, что каталог это и есть главный
    ответ на вопрос «то же ли это место». Ветку дочитываем с диска, из
    `.git/HEAD`: подпроцесс в горячем пути стоит дороже всего остального.

    Времени в payload нет вовсе, и «сейчас» подставляем здесь. Без этого день и
    часть суток у хода всегда пусты, а пустая ось не судится ни с той, ни с
    другой стороны: две временные оси из пяти молча не работали бы никогда.

    Пустая обстановка — это None, а не словарь из пустых осей: молчание об
    обстановке не должно читаться как «ничего не подходит».
    """
    payload = payload or {}
    cwd = payload.get("cwd")
    if not (cwd or payload.get("project")):
        # Одно время это не обстановка. Часы у хода есть всегда, и подставь мы
        # их без места — фильтр судил бы факты по одному дню недели, притом что
        # знать про ход мы не знаем ничего.
        return None
    found = context.of(dict(
        payload,
        git_branch=payload.get("git_branch")
        or (context.branch_of(cwd) if cwd else None),
        occurred_at=payload.get("occurred_at")
        or at or datetime.now(timezone.utc).isoformat()))
    return found if any(value is not None for value in found.values()) else None


def keys_of_facts(items):
    """Подписи фактов среди кусков выдачи. Ими спрашивается их обстановка."""
    out = []
    for item in items:
        record = item[2] if len(item) > 2 else None
        if isinstance(record, dict) and record.get("object_type") == "Fact":
            out.append(models.Fact(fact_type=record.get("fact_type") or "",
                                   subject=record.get("subject") or "",
                                   scope=record.get("scope") or "").identity())
    return out


@telemetry.traced("place_in_context", lambda arg, out: {
    "in": len(arg["items"]),
    "fitted": sum(1 for row in out
                  if isinstance(row[2], dict) and row[2].get("fit") is not None)})
def place(items, here, door=None):
    """Приписать каждому куску его обстановку и уместность к обстановке хода.

    Обстановка факта лежит не в нём: ветка, каталог и время принадлежат
    эпизоду и добираются связью. У эпизода, события и разговора она в самой
    записи — там же, где её ищет `context.of`. Одна функция на обе стороны,
    поэтому сравнивать есть что.

    Дверь, которая обстановки не читает, оставляет факту то, что есть в его
    строке: проект и отметку «когда видели». Этого хватает на главную ось, и
    сетевой путь не остаётся без уместности вовсе.

    Обстановки хода нет — не трогаем ничего: так ходит замер и все прежние
    вызовы, и выдача обязана остаться прежней.
    """
    if not here:
        return list(items)
    known = {}
    keys = keys_of_facts(items)
    if keys and door is not None:
        try:
            known = door.contexts(keys)
        except AttributeError:
            known = {}
        except Exception:
            known = {}      # обстановка это добавка: её отказ не роняет выдачу
    out = []
    for item in items:
        score, text = item[0], item[1]
        record = item[2] if len(item) > 2 else None
        if not isinstance(record, dict):
            out.append((score, text, record))
            continue
        if record.get("object_type") == "Fact":
            key = models.Fact(fact_type=record.get("fact_type") or "",
                              subject=record.get("subject") or "",
                              scope=record.get("scope") or "").identity()
            found = known.get(key) or [context.of(record)]
        else:
            found = [context.of(record)]
        closest = max(found, key=lambda one: context.fit(one, here))
        out.append((score, text, dict(record, situation=closest,
                                      fit=context.best(found, here))))
    return out


def fit_of(record):
    """Уместность куска, если её посчитали. Не посчитали — None, а не единица."""
    return record.get("fit") if isinstance(record, dict) else None


def passes(score, record, min_score=MIN_SCORE, min_fit=MIN_FIT):
    """Прошёл ли кусок порог. Порог судит произведение веса на уместность.

    Веса может не быть вовсе — его дописывает только текстовый путь, а в базе
    лежат записи. Множителя, на который умножать, тогда не существует, и порог
    на произведении вырождается в порог на одной уместности. Это и есть
    основной случай: неуместное режется без всякой оценки.

    Уместности нет — обстановки хода не дали, — судим весом, как судили всегда.
    """
    fit = fit_of(record)
    if score is None:
        return fit is None or fit >= min_fit
    return score * (fit if fit is not None else 1.0) >= min_score


def rank(score, record):
    """Чем кусок сильнее. Тем же произведением, каким его судит порог."""
    fit = fit_of(record)
    if score is None:
        return fit if fit is not None else 0.0
    return score * (fit if fit is not None else 1.0)


def winnow(items, min_score=MIN_SCORE, min_fit=MIN_FIT):
    """Куски, прошедшие порог. Потолки тут не считаются — это следующий шаг.

    Отдельно от `gate` потому, что молчание нужно уметь назвать: «не прошло
    порог» и «не влезло в потолок» — разные причины и разная починка. Держать
    правило порога в двух местах нельзя, оно разъедется молча, поэтому `gate`
    зовёт эту же функцию.
    """
    rows = [(i[0], i[1], i[2] if len(i) > 2 else None) for i in items]
    return [(s, t, r) for s, t, r in rows
            if passes(s, r, min_score=min_score, min_fit=min_fit)]


def eligible(items, min_score=MIN_SCORE, min_fit=MIN_FIT):
    """Прошедшие порог, из которых порог вообще может собрать выдачу.

    Проза читателя без оценки и без записи — его собственные слова, а не факт;
    так приходит «no matching files found». Кусок, пустеющий после снятия
    маркера, тоже не факт. Правило одно на двоих: по нему `gate` собирает
    выдачу, по нему же называется молчание. Держи их порознь — и проза,
    отсеянная порогом, назвалась бы «не влезло в потолок», то есть самая
    частая причина молчания уехала бы в чужую колонку.
    """
    return [(s, t, r) for s, t, r in winnow(items, min_score=min_score,
                                            min_fit=min_fit)
            if (s is not None or r) and SCORE.sub("", t).strip()]


@telemetry.traced("threshold_filter", lambda arg, out: {
    "in": len(arg["items"]), "out": len(out), "min_score": arg["min_score"]})
def gate(items, min_score=MIN_SCORE, max_items=MAX_ITEMS, max_chars=MAX_CHARS,
         min_fit=MIN_FIT):
    """Порог. Кусок без оценки пропускаем, но ставим после оценённых.

    Судит произведение веса на уместность, а не один вес: частое, но не к
    месту, до агента доходить не должно, см. ADR 0009. Порядок внутри
    оценённых — по тому же произведению.

    Маркер уверенности дописывает одна функция, `understand.render_fact`, и в
    хранилище ноль его вхождений: всё, что там лежит, записано мимо неё. Пока
    порог требовал маркер, он возвращал пустоту при любом содержимом базы —
    и выбрасывал в том числе единственный ответ, где нужное нашлось.
    Отсутствие оценки это не отказ, это отсутствие оценки.

    Кусок, не влезающий в потолок, пропускается, а не обрывает выдачу. На
    настоящей базе первым куском приходило событие на тысячи символов, и
    порог отдавал пустоту, имея за спиной пятьдесят пять тысяч символов
    найденного.
    """
    kept = eligible(items, min_score=min_score, min_fit=min_fit)
    scored = sorted((row for row in kept if row[0] is not None),
                    key=lambda row: rank(row[0], row[2]), reverse=True)
    # Проза читателя без оценки отсеяна раньше, здесь остаётся только порядок:
    # оценённые впереди, структурные записи без оценки следом.
    plain = sorted(((None, t, r) for s, t, r in kept if s is None),
                   key=lambda row: rank(row[0], row[2]), reverse=True)
    out, size = [], 0
    for score, text, record in (scored + plain)[:max_items]:
        clean = SCORE.sub("", text).strip()
        # Обстановка едет вместе с фактом и место в потолке занимает наравне
        # с ним: не считать её значило бы тихо раздуть кусок вдвое.
        room = len(clean) + len(context.describe(
            record.get("situation") if isinstance(record, dict) else None))
        if size + room > max_chars:
            continue     # длинный кусок пропускаем, а не обрываем на нём выдачу
        out.append((score, clean, record))
        size += room
    return out


def render(kept):
    """Формат под агента: сжатые утверждения, без обращений и предисловий.

    Факт уходит вместе со своей обстановкой, а не голой строкой: модель должна
    видеть, откуда факт и когда он верен, и решать сама. Строка обстановки
    разобрана по осям — сырой словарь читать тяжелее, чем не иметь вовсе.

    Ни обстановку, ни уместность не приписываем там, где их не считали:
    выдуманное число выглядит измеренным.
    """
    lines = ["Из памяти прошлых разговоров:"]
    for score, text, record in kept:
        one = " ".join(text.split())
        lines.append("- %s (уверенность %.2f)" % (one, score) if score is not None
                     else "- %s" % one)
        where = context.describe(record.get("situation")
                                 if isinstance(record, dict) else None)
        fit = fit_of(record)
        if where:
            lines.append("  обстановка: %s%s"
                         % (where, "" if fit is None else
                            " (уместность %.2f)" % fit))
    return "\n".join(lines)


# Затухание на шаг по графу. Сосед приходит не потому, что его спросили, а
# потому, что он связан с тем, что спросили: весить больше источника он не может.
DAMPING = 0.5

# Сколько соседей добираем. Потолок на всю выдачу всё равно режет, но обходить
# граф ради кусков, которые заведомо не влезут, незачем.
MAX_NEAR = 3


def keys_of(kept):
    """Подписи фактов, которые прошли порог. От них и делается шаг."""
    out = []
    for item in kept:
        record = item[2] if len(item) > 2 else None
        if isinstance(record, dict) and record.get("object_type") == "Fact":
            try:
                out.append(models.Fact(fact_type=record.get("fact_type") or "",
                                       subject=record.get("subject") or "",
                                       scope=record.get("scope") or "").identity())
            except (TypeError, ValueError):
                continue
    return out


@telemetry.traced("graph_step", lambda arg, out: {
    "from": len(arg["kept"]), "near": len(out)})
def near(kept, door, limit=MAX_NEAR):
    """Один шаг по графу: факты, связанные с тем, что уже нашлось.

    Дверь, которая обхода не умеет, оставляет выдачу как была: у сетевого
    читателя такого чтения нет, и подсказка не имеет права от него зависеть.

    Шаг ровно один. Сосед соседа приходил бы уже не по связи с вопросом, а по
    связи со связью — на архиве это заливает выдачу быстрее, чем помогает.
    """
    keys = keys_of(kept)
    if not keys:
        return []
    try:
        found = door.neighbours(keys, limit=limit)
    except AttributeError:
        return []
    except Exception:
        # Обход это добавка. Упади он — подсказка обязана отдать то, что нашла
        # поиском, а не промолчать целиком.
        return []
    # База затухания — лучшая из оценок прямых попаданий. Оценки может не быть
    # вовсе: её дописывает только текстовый путь, а в хранилище лежат записи.
    # Тогда соседу оценку не приписываем: выдуманное число выглядело бы как
    # измеренное, и слабейшая строка оказалась бы единственной с цифрой.
    scored = [s for s, _, _ in kept if s is not None]
    top = max(scored) if scored else None
    # Вес связи задаёт порядок соседей — их отдаёт хранилище тяжёлыми вперёд.
    # В оценку он не входит: она у всех соседей одна и ниже источника, иначе
    # тяжёлая связь вытаскивала бы соседа выше того, о чём спросили.
    out = []
    for record, _weight in found:
        record = dict(record, via_graph=True)
        out.append((round(top * DAMPING, 4) if top is not None else None,
                    _text(record), record))
    return out


@telemetry.traced("pipeline", lambda arg, out: {
    "kept": len(out[1]), "sent_chars": len(out[0]), "silent": not out[0],
    "reason": out[3]})
def consult(query, mode="single", min_score=MIN_SCORE, door=None, here=None):
    """То же, что `suggest`, но с именем причины, по которой память промолчала.

    Причина берётся не догадкой поверх пустого ответа, а с той ступени, где
    выдача опустела: не нашли, всё оказалось ложной находкой, не прошло порог,
    не влезло в потолок. Догадка снаружи их не различает, а разбирать разрыв
    между «нашли» и «отдали» можно только по именам.

    Отдаёт четвёркой: текст, куски, сырой ответ, причина. Причина у говорящего
    захода — None.
    """
    door = door or port.door()
    here = context.of(here) if here else None
    if here and not any(value is not None for value in here.values()):
        here = None
    answer = door.read(query, mode=mode)
    chunks = pieces(answer)
    if not chunks:
        # Выключенная память отвечает пустотой ровно так же, как ненашедшая, и
        # не назови мы это отдельно — рубильник читался бы как «в памяти пусто».
        # Спрашиваем после чтения, а не вместо: половина сравнения «без памяти»
        # обязана идти той же дорогой, что рабочая, и отличаться лишь исходом.
        off = getattr(door, "name", None) == port.SilentDoor.name
        return "", [], answer, "disabled" if off else "not_found"
    # Отсев до порога: ложная находка не должна ни занимать место в
    # пятёрке, ни съедать потолок в 1200 символов. Обстановка приписывается
    # между ними: порог судит произведение веса на уместность.
    honest = sift(chunks, query)
    if not honest:
        return "", [], answer, "incidental"
    placed = place(honest, here, door)
    kept = gate(placed, min_score=min_score)
    if not kept:
        # Порог зовём вторым, а не первым: иначе на пустой выдаче ступень
        # порога не отработала бы вовсе и пропала из замера. Здесь он только
        # разводит три случая, которые снаружи выглядят одинаково пусто.
        if not winnow(placed, min_score=min_score):
            return "", [], answer, "below_threshold"
        if not eligible(placed, min_score=min_score):
            # Порог прошли, а записей среди прошедшего нет: хранилище ответило
            # словами. Это «не нашли», а не «не влезло».
            return "", [], answer, "not_found"
        return "", [], answer, "over_budget"
    # Соседи добираются после порога и ставятся следом: прямое попадание
    # первым, добавка второй. Потолки те же — их считает тот же `gate`.
    added = place(near(kept, door), here, door)
    if added:
        room = MAX_ITEMS - len(kept)
        if room > 0:
            size = sum(len(t) + len(context.describe(
                r.get("situation") if isinstance(r, dict) else None))
                for _, t, r in kept)
            for score, text, record in added[:room]:
                clean = " ".join(text.split())
                where = len(context.describe(record.get("situation")
                                             if isinstance(record, dict) else None))
                if size + len(clean) + where > MAX_CHARS:
                    continue
                kept.append((score, clean, record))
                size += len(clean) + where
    return render(kept), kept, answer, None


def suggest(query, mode="single", min_score=MIN_SCORE, door=None, here=None):
    """Что всплывает на этот вопрос в этой обстановке.

    `here` — обстановка хода. Не задана (так ходит замер и все прежние вызовы)
    — уместность не считается, и выдача ровно та, что была до неё.

    Причину молчания не отдаёт: её спрашивают у `consult`. Так замер и прежние
    вызовы остаются с той же тройкой, что была до ленты.
    """
    return consult(query, mode, min_score, door=door, here=here)[:3]


def sources_of(kept):
    """Записи, из которых собрана подсказка, в виде концов связи.

    Читатель отдаёт найденное записями схемы, и у каждой есть свой ключ. Берём
    только те объекты, которые схема разрешает считать источником вставки:
    факт и эпизод.
    """
    out = []
    for item in kept:
        record = item[2] if len(item) > 2 else None
        if not isinstance(record, dict):
            continue
        kind = record.get("object_type")
        if kind == "Fact":
            found = models.Fact(fact_type=record.get("fact_type") or "",
                                subject=record.get("subject") or "",
                                scope=record.get("scope") or "")
        elif kind == "Episode":
            found = models.Episode(session_id=record.get("session_id") or "",
                                   episode_number=record.get("episode_number"))
        else:
            continue
        try:
            found._validate_key()
        except models.SchemaError:
            continue          # запись без половины ключа в связь не поставить
        out.append(found)
    return out


def shown_keys(kept):
    """Ключи записей, которые агент увидел в подсказке. Ими лента их и считает.

    Ключ ставится в момент вброса и уходит вместе с ним. Без него ответ про
    пользу повиснет между показанными записями и достанется не той: за ход
    агент получает несколько подсказок и делает много шагов.
    """
    out = []
    for source in sources_of(kept):
        if source.OBJECT == "Fact":
            out.append(source.identity())
        else:
            out.append("%s|%s|%s" % (source.OBJECT, source.session_id,
                                     source.episode_number))
    return out


def renew(kept, door=None, at=None, mode=None):
    """Продлить срок фактам, которые ушли в выдачу. Спрошенное живёт дальше.

    Обращение здесь — это показ: факт попал в кусок, который увидел агент.
    Второй счётчик обращения, польза, пишется отметкой исхода в поле `helped`;
    какой из двух лучше держит память живой, решается по данным, а продлевать
    достаточно по любому.

    Уходит обновлением, а не записью целиком: мы знаем ключ факта и новый срок,
    а содержимое лежит в хранилище — читать его ради продления незачем, да и
    нечем на горячем пути. Строки, которой нет, обновление не заводит, поэтому
    ключ из чужой выдачи не воскрешает уехавшее в отложенное.

    Дверь без структурной записи оставляет всё как было: продление это добавка,
    и её отсутствие не должно менять ни выдачу, ни ход.
    """
    door = door or port.door()
    if not hasattr(door, "write_objects"):
        return []
    until = lifespan.until(at, mode)
    renewals = [models.Fact(fact_type=source.fact_type, subject=source.subject,
                            scope=source.scope, valid_until=until)
                for source in sources_of(kept) if source.OBJECT == "Fact"]
    if renewals:
        door.write_objects(renewals, op="update")
    return renewals


def injection_of(session_id, text, at=None):
    """Запись MemoryInjection по схеме. Ключ — разговор и время вставки."""
    return models.MemoryInjection(
        session_id=session_id or "unknown",
        injected_at=at or datetime.now(timezone.utc).isoformat(),
        injected_content=" ".join((text or "").split())[:600],
        notes="подставлено модулем подсказки по порогу %.2f" % MIN_SCORE)


def note_injection(session_id, text, kept=(), door=None, at=None):
    """MemoryInjection: что подставили и из чего собрали. Исход — потом.

    Уходит структурой: прежде запись писалась прозой, и ключ ей выводил
    разборщик на той стороне. Ключ, которого мы не знаем, в связь не поставить,
    поэтому у вставки не было ни разговора, ни источников — она лежала сама по
    себе и ни с чем не сходилась.
    """
    door = door or port.door()
    record = injection_of(session_id, text, at)
    if not hasattr(door, "write_objects"):
        door.write(render_injection(record))
        remember(record)
        return record
    session = models.Session(session_id=record.session_id)
    relations = [models.link("injection_target_session",
                             memory_injection=record, session=session)]
    for source in sources_of(kept):
        role = "fact" if source.OBJECT == "Fact" else "episode"
        relations.append(models.link("injection_source_%s" % role,
                                     memory_injection=record, **{role: source}))
    door.write_objects([session, record], relations)
    # Обращение продлевает срок: то, что попало в выдачу, живёт дальше. Стоит
    # здесь, а не в самой подсказке, потому что подсказку зовёт ещё и замер, а
    # замер не должен двигать сроки в хранилище, которое меряет.
    renew(kept, door=door, at=record.injected_at)
    # Ключ уходит в журнал после записи, а не до неё. По нему проход `--settle`
    # потом находит, чем кончился ход, и ключ вставки, которой в хранилище нет,
    # заставил бы его завести пустую запись с одним ключом.
    remember(record)
    return record


def mute(reason, session_id, query, note=None):
    """Записать молчание и вернуть его причину. Одна дверь для всех отказов.

    Имя причины ставится там, где молчание случилось, а не догадкой снаружи:
    догадка видит только пустую строку и все шесть причин сливает в одну.
    """
    ledger.silence(reason, session_id=session_id, query=query, note=note)
    return reason


def attend(query, session_id=None, door=None, here=None, mode="single",
           min_score=MIN_SCORE, hot=False, record=True, at=None):
    """Заход подсказки целиком: спросить память, отдать найденное и оставить
    в ленте ровно один исход — вброс или молчание с названной причиной.

    Собрано в одну функцию, потому что исход у захода один, а решается он в
    разных местах: срок, отказ носителя, четыре ступени отсева. Раздай их по
    вызывающим — и каждый назовёт молчание по-своему, а половина не назовёт
    никак. Разрыв «нашли 32, отдали 25» разбирается только по именам.

    `hot` — горячий путь, тот самый, где действует срок. `record` — писать ли
    запись о вставке; замер и проверки ходят без неё.

    Отдаёт тройкой: текст, куски, причина молчания. У говорящего захода — None.
    """
    door = door or port.door()
    cancel = deadline() if hot else (lambda: None)
    try:
        text, kept, _raw, why = consult(query, mode, min_score, door=door,
                                        here=here)
    except Overdue:
        return "", [], mute("overdue", session_id, query)
    except port.BackendError as bad:
        return "", [], mute("backend_error", session_id, query, note=str(bad))
    except Exception as bad:
        # Своя поломка называется своей причиной. Свали её на носитель — и
        # колонка отказов начнёт расти от наших же ошибок, а разбивка по
        # причинам, ради которой всё и затевалось, начнёт врать.
        return "", [], mute("pipeline_error", session_id, query,
                            note="%s: %s" % (type(bad).__name__, bad))
    finally:
        cancel()
    if not text:
        return "", [], mute(why or "not_found", session_id, query)
    at = at or datetime.now(timezone.utc).isoformat()
    if record:
        try:
            note_injection(session_id, text, kept, door=door, at=at)
        except Exception:
            # Подсказка уже собрана и уйдёт агенту: неудачная запись о ней
            # права её отменить не имеет.
            pass
    # Лента пишется от показа, а не от записи: показ случился ровно тогда,
    # когда текст ушёл агенту. Привяжи её к записи — и заход, чью запись
    # носитель не принял, пропал бы из ленты целиком: ни вброса, ни молчания,
    # то есть невидимо ровно там, где имена причин и нужны.
    ledger.injected(session_id, at, shown_keys(kept), query=query)
    # Ключ вставки уходит агенту вместе с ней. Без него спросить про пользу
    # нечем: за ход подсказок бывает несколько, и ответ повис бы между ними.
    # Ключ дописывается после ленты и не входит в записанное содержимое: это
    # адрес вставки, а не то, что память вспомнила.
    try:
        ask = prompt.used([ledger.key_of(session_id, at)])
    except Exception:
        ask = ""            # просьба это добавка, отменять подсказку она не вправе
    return ("%s\n\n%s" % (text, ask) if ask else text), kept, None


def render_injection(record):
    """Та же запись прозой — для двери, которая структурной записи не умеет."""
    return "\n".join([
        "MemoryInjection.",
        "session_id: %s" % record.session_id,
        "injected_at: %s" % record.injected_at,
        "injected_content: %s" % (record.injected_content or ""),
        "notes: %s" % (record.notes or ""),
    ])


# Каким способом снят ответ про пользу в этом проходе. Способов три, и они
# считаются порознь; проход по архиву — догадка без участия агента.
SETTLE_SOURCE = "transcript"


def settle(files, door=None, log=None):
    """Отметить исход ходов, в которые подставляли память.

    Правило. `helped` это не «память помогла» — такого наблюдения у нас нет.
    Это «ход, в который её подставили, дошёл до конца»: берём первый эпизод
    того же разговора, начавшийся не раньше вставки, и смотрим его исход.
    `done` — да, `blocked` — нет, `abandoned` или эпизода нет вовсе — поле не
    пишем: неизвестное это не отрицательное.

    Список вставок берём из своего журнала, а не из хранилища: читать оттуда мы
    умеем только поиском словами, а перечислить вставки поиск не может.
    """
    from archive.transcripts import episodes_from_file
    from pipeline import understand

    known = notes_of(log)
    if not known:
        return {"seen": 0, "settled": 0, "logged": 0}
    ends = {}
    for path in files:
        try:
            episodes = episodes_from_file(path)
        except OSError:
            continue
        for ep in episodes:
            talk = ep["session_id"] or "unknown"
            ends.setdefault(talk, []).append(
                (ep["started_at"] or "", understand.outcome_of(ep)))
    for pairs in ends.values():
        pairs.sort()

    door = door or port.door()
    got = {"seen": len(known), "settled": 0, "logged": 0}
    # Что по этим вставкам уже отвечено. Проход не помечает журнал разобранным
    # и каждый заход идёт по нему целиком: дописывай мы вслепую, одна вставка
    # получала бы новую строку пользы на каждом проходе, и доля поехала бы
    # вверх на ровном месте.
    answered = ledger.verdicts(ledger.rows(), source=SETTLE_SOURCE)
    door_written, marks = [], []
    for talk, at in known:
        outcome = "unknown"
        for started, found in ends.get(talk, []):
            if started >= at:
                outcome = found
                break
        helped = {"done": True, "blocked": False}.get(outcome)
        door_written.append(models.MemoryInjection(
            session_id=talk, injected_at=at,
            session_outcome=outcome, helped=helped))
        verdict = ledger.verdict_of(helped)
        if answered.get((talk, at)) != verdict:
            marks.append((talk, at, verdict))
        got["settled"] += 1
    if door_written and hasattr(door, "write_objects"):
        door.write_objects(door_written)
    # Лента после хранилища, тем же порядком, что и всюду: ответ про пользу
    # вставки, которой в хранилище нет, считать нельзя.
    for talk, at, verdict in marks:
        ledger.helped(talk, at, verdict, source=SETTLE_SOURCE)
        got["logged"] += 1
    return got


# Способ съёма ответа для вопроса, заданного вместе с вбросом. Свой, а не
# общий с проходом по архиву: у одного показа вышло бы два ответа, и «помог M
# из N» перестало бы сходиться. Расхождение способов — отдельные данные.
INLINE_SOURCE = "inline"


def harvest(files, log=None):
    """Снять ответы агента про пользу и уложить их в ленту по ключу вставки.

    Ответ приходит служебным блоком в ответе самого агента — тем же, которым он
    размечает факты. Читаем его оттуда же, откуда читаются факты: из архива.

    Принимается только ответ по ключу, который мы сами выдали: список вставок
    берём из своего журнала. Иначе долю пользы правил бы текст ответа, а не
    работа памяти — и ошибка агента в ключе тихо росла бы чужой счёт.

    Известная слабость способа: агенту только что показали подсказку, и он
    склонен согласиться. Поэтому ответ пишется своим значением `inline` и с
    догадкой по архиву не складывается, см. ADR 0012.
    """
    from archive.transcripts import episodes_from_file

    known = {ledger.key_of(talk, at) for talk, at in notes_of(log)}
    got = {"seen": 0, "logged": 0, "unknown": 0}
    if not known:
        return got
    # Что по этим вставкам уже отвечено этим же способом. Проход не помечает
    # архив разобранным и идёт по нему целиком: дописывай мы вслепую, одна
    # вставка получала бы новую строку на каждом ходе.
    answered = ledger.verdicts(ledger.rows(), source=INLINE_SOURCE)
    said = {}
    for path in files:
        try:
            episodes = episodes_from_file(path)
        except OSError:
            continue
        for episode in episodes:
            for key, verdict in marks.uses_of(episode):
                got["seen"] += 1
                if key not in known:
                    got["unknown"] += 1
                    continue
                said[key] = verdict     # последнее слово по ключу остаётся за ним
    for key, verdict in said.items():
        parts = ledger.key_parts(key)
        if not parts or answered.get(parts) == verdict:
            continue
        ledger.helped(parts[0], parts[1], verdict, source=INLINE_SOURCE)
        got["logged"] += 1
    return got


def remember(record, log=None):
    """Строка журнала о вставке: разговор и время, то есть её ключ."""
    where = Path(log) if log else LOG
    where.parent.mkdir(parents=True, exist_ok=True)
    with where.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": record.session_id,
                             "injected_at": record.injected_at,
                             "sent": True}, ensure_ascii=False) + "\n")


def notes_of(log=None):
    """Вставки из журнала: разговор и время. Битые строки пропускаем."""
    where = Path(log) if log else LOG
    if not where.exists():
        return []
    out = []
    for line in where.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("injected_at") and row.get("sent"):
            out.append((row.get("session_id") or "unknown", row["injected_at"]))
    return out


def transcripts(only=None):
    """Файлы архива, по которым идёт отметка исхода.

    Сужение до названного каталога — то же правило и та же причина, что у
    понимания на конце хода: хук не должен ходить по чужим разговорам.
    """
    from archive.transcripts import TRANSCRIPTS
    found = sorted(TRANSCRIPTS.rglob("*.jsonl")) if not only \
        else sorted(Path(only).rglob("*.jsonl"))
    return [path for path in found if not only or only in str(path)]


def parser():
    """Разбор аргументов отдельно от работы: его зовут проверки.

    Хук подставляет ключи строкой и всегда выходит нулём. Ключ, которого нет,
    роняет проход молча — ни в разговоре, ни по коду возврата этого не видно.
    """
    ap = argparse.ArgumentParser(description="Подсказка агенту из xmemory")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--mode", default="single", choices=["single", "raw", "xresponse"])
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--hook", action="store_true", help="режим хука: запрос из json на входе")
    ap.add_argument("--no-record", action="store_true", help="не писать MemoryInjection")
    ap.add_argument("--settle", action="store_true",
                    help="отметить исход ходов, куда подставляли память, и выйти")
    ap.add_argument("--uses", action="store_true",
                    help="снять ответы агента про пользу подсказок и выйти")
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    return ap


def main():
    args = parser().parse_args()

    if args.settle:
        got = settle(transcripts(args.only))
        # «Пересчитано», а не «отмечено»: журнал не помечается разобранным, и
        # каждый заход проходит его целиком. Запись идёт по ключу, поверх, так
        # что вреда нет — но и числа новых отметок это не даёт.
        # «Дописано в ленту» отдельным числом: пересчитано столько же каждый
        # заход, а растёт лента только на новых и изменившихся ответах.
        print("вставок в журнале %d, пересчитано %d, дописано в ленту %d"
              % (got["seen"], got["settled"], got["logged"]))
        return

    if args.uses:
        got = harvest(transcripts(args.only))
        print("ответов агента %d, дописано в ленту %d, по чужому ключу %d"
              % (got["seen"], got["logged"], got["unknown"]))
        return

    session_id, here = None, None
    if args.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return
        query, session_id = payload.get("prompt") or "", payload.get("session_id")
        # Обстановка хода снимается со всего payload, а не с одного вопроса.
        # Прежде `cwd` и `permission_mode` выбрасывались — при том, что каталог
        # и есть главный ответ на вопрос «то же ли это место».
        here = situation_of(payload)
        if not query.strip():
            return
        # Просьба разметить факты уходит в запрос до всякой работы с памятью:
        # память умеет молчать, падать и опаздывать, а просьба не должна
        # зависеть ни от одного из этих исходов. Текст тот же самый, что у
        # pipeline.prompt: одна строка на всех, кто подмешивает её к запросу.
        try:
            print(prompt.text())
        except Exception:
            pass
    else:
        query = args.query or sys.stdin.read()

    # Одна дверь на чтение и на отметку: две открывали бы два пути наружу,
    # и отметка могла лечь не туда, откуда читали.
    door = port.door()
    try:
        # Заход целиком, вместе с записью о вставке: ключ вставки в журнал
        # пишет сама отметка, а молчание уходит в ленту со своим именем.
        text, kept, why = attend(query, session_id=session_id, door=door,
                                 here=here, mode=args.mode,
                                 min_score=args.min_score, hot=args.hook,
                                 record=not args.no_record)
    except Exception:
        if args.hook:
            return          # молчим: подсказка не имеет права ломать разговор
        raise

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"query": query[:300], "kept": len(kept),
                             "sent": bool(text), "reason": why},
                            ensure_ascii=False) + "\n")

    if not text:
        return              # нечему всплывать, молчим
    print(text)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--hook" not in sys.argv:
            raise
    sys.exit(0)
