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

from domain import lifespan, models
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
MAX_ITEMS = 5        # больше пяти внимание модели размазывается
MAX_CHARS = 1200     # потолок на весь кусок, который уходит в контекст агента

SCORE = re.compile(r"Оценка уверенности:\s*([0-9]+(?:\.[0-9]+)?)")

# Служебные поля записи: агенту не говорят ничего, а потолок съедают.
NOISE = ("first_seen_at", "observed_at", "created_at", "updated_at", "id",
         "object_type", "via_graph", "sequence_number", "session_id",
         "fact_type", "scope", "subject", "valid_until", "lapsed_at")


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


@telemetry.traced("threshold_filter", lambda arg, out: {
    "in": len(arg["items"]), "out": len(out), "min_score": arg["min_score"]})
def gate(items, min_score=MIN_SCORE, max_items=MAX_ITEMS, max_chars=MAX_CHARS):
    """Порог. Кусок без оценки пропускаем, но ставим после оценённых.

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
    rows = [(i[0], i[1], i[2] if len(i) > 2 else None) for i in items]
    scored = sorted(((s, t, r) for s, t, r in rows if s is not None and s >= min_score),
                    key=lambda item: item[0], reverse=True)
    # Проза читателя без оценки это не факт, а его собственные слова: так
    # приходит и «no matching files». Пропускаем только структурные записи.
    plain = [(None, t, r) for s, t, r in rows if s is None and r]
    out, size = [], 0
    for score, text, record in (scored + plain)[:max_items]:
        clean = SCORE.sub("", text).strip()
        if not clean:
            continue
        if size + len(clean) > max_chars:
            continue     # длинный кусок пропускаем, а не обрываем на нём выдачу
        out.append((score, clean, record))
        size += len(clean)
    return out


def render(kept):
    """Формат под агента: сжатые утверждения, без обращений и предисловий."""
    lines = ["Из памяти прошлых разговоров:"]
    for score, text, _ in kept:
        one = " ".join(text.split())
        lines.append("- %s (уверенность %.2f)" % (one, score) if score is not None
                     else "- %s" % one)
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
    "kept": len(out[1]), "sent_chars": len(out[0]), "silent": not out[0]})
def suggest(query, mode="single", min_score=MIN_SCORE, door=None):
    door = door or port.door()
    answer = door.read(query, mode=mode)
    kept = gate(pieces(answer), min_score=min_score)
    # Соседи добираются после порога и ставятся следом: прямое попадание
    # первым, добавка второй. Потолки те же — их считает тот же `gate`.
    added = near(kept, door)
    if added:
        room = MAX_ITEMS - len(kept)
        if room > 0:
            size = sum(len(t) for _, t, _ in kept)
            for score, text, record in added[:room]:
                clean = " ".join(text.split())
                if size + len(clean) > MAX_CHARS:
                    continue
                kept.append((score, clean, record))
                size += len(clean)
    return render(kept) if kept else "", kept, answer


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


def render_injection(record):
    """Та же запись прозой — для двери, которая структурной записи не умеет."""
    return "\n".join([
        "MemoryInjection.",
        "session_id: %s" % record.session_id,
        "injected_at: %s" % record.injected_at,
        "injected_content: %s" % (record.injected_content or ""),
        "notes: %s" % (record.notes or ""),
    ])


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
        return {"seen": 0, "settled": 0}
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
    got = {"seen": len(known), "settled": 0}
    records = []
    for talk, at in known:
        outcome = "unknown"
        for started, found in ends.get(talk, []):
            if started >= at:
                outcome = found
                break
        helped = {"done": True, "blocked": False}.get(outcome)
        records.append(models.MemoryInjection(
            session_id=talk, injected_at=at,
            session_outcome=outcome, helped=helped))
        got["settled"] += 1
    if records and hasattr(door, "write_objects"):
        door.write_objects(records)
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
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    return ap


def main():
    args = parser().parse_args()

    if args.settle:
        got = settle(transcripts(args.only))
        # «Пересчитано», а не «отмечено»: журнал не помечается разобранным, и
        # каждый заход проходит его целиком. Запись идёт по ключу, поверх, так
        # что вреда нет — но и числа новых отметок это не даёт.
        print("вставок в журнале %d, пересчитано %d" % (got["seen"], got["settled"]))
        return

    session_id = None
    if args.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return
        query, session_id = payload.get("prompt") or "", payload.get("session_id")
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
    cancel = deadline() if args.hook else (lambda: None)
    try:
        text, kept, raw = suggest(query, args.mode, args.min_score, door=door)
    except Exception:
        if args.hook:
            return          # молчим: подсказка не имеет права ломать разговор
        raise
    finally:
        cancel()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"query": query[:300], "kept": len(kept),
                             "sent": bool(text), "raw_chars": len(raw)},
                            ensure_ascii=False) + "\n")

    if not text:
        return              # нечему всплывать, молчим
    print(text)
    if not args.no_record:
        try:
            # Ключ вставки в журнал пишет сама отметка: молчание журнальной
            # строки не оставляет, а вставка без ключа не находится потом.
            note_injection(session_id, text, kept, door=door)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--hook" not in sys.argv:
            raise
    sys.exit(0)
