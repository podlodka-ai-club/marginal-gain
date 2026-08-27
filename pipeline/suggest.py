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

from infra import telemetry
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
NOISE = ("first_seen_at", "observed_at", "created_at", "updated_at", "id")


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
        chunks = [(_text(c), isinstance(c, dict)) for c in body]
    elif isinstance(body, dict):
        chunks = [(_text(body), True)]
    else:
        chunks = [(c, False) for c in re.split(r"\n\s*\n", str(body))]
    out = []
    for chunk, structured in chunks:
        text = chunk.strip()
        if not text:
            continue
        found = SCORE.search(text)
        out.append((float(found.group(1)) if found else None, text, structured))
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
    rows = [(i[0], i[1], i[2] if len(i) > 2 else False) for i in items]
    scored = sorted(((s, t) for s, t, _ in rows if s is not None and s >= min_score),
                    reverse=True)
    # Проза читателя без оценки это не факт, а его собственные слова: так
    # приходит и «no matching files». Пропускаем только структурные записи.
    plain = [(None, t) for s, t, struct in rows if s is None and struct]
    out, size = [], 0
    for score, text in (scored + plain)[:max_items]:
        clean = SCORE.sub("", text).strip()
        if not clean:
            continue
        if size + len(clean) > max_chars:
            continue     # длинный кусок пропускаем, а не обрываем на нём выдачу
        out.append((score, clean))
        size += len(clean)
    return out


def render(kept):
    """Формат под агента: сжатые утверждения, без обращений и предисловий."""
    lines = ["Из памяти прошлых разговоров:"]
    for score, text in kept:
        one = " ".join(text.split())
        lines.append("- %s (уверенность %.2f)" % (one, score) if score is not None
                     else "- %s" % one)
    return "\n".join(lines)


@telemetry.traced("pipeline", lambda arg, out: {
    "kept": len(out[1]), "sent_chars": len(out[0]), "silent": not out[0]})
def suggest(query, mode="single", min_score=MIN_SCORE, door=None):
    answer = (door or port.door()).read(query, mode=mode)
    kept = gate(pieces(answer), min_score=min_score)
    return render(kept) if kept else "", kept, answer


def note_injection(session_id, text, door=None):
    """MemoryInjection: что подставили. Помогло ли, узнаем потом."""
    (door or port.door()).write("\n".join([
        "MemoryInjection.",
        "session_id: %s" % (session_id or "unknown"),
        "injected_at: %s" % datetime.now(timezone.utc).isoformat(),
        "injected_content: %s" % " ".join(text.split())[:600],
        "notes: подставлено модулем подсказки по порогу %.2f" % MIN_SCORE,
    ]))


def main():
    ap = argparse.ArgumentParser(description="Подсказка агенту из xmemory")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--mode", default="single", choices=["single", "raw", "xresponse"])
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--hook", action="store_true", help="режим хука: запрос из json на входе")
    ap.add_argument("--no-record", action="store_true", help="не писать MemoryInjection")
    args = ap.parse_args()

    session_id = None
    if args.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return
        query, session_id = payload.get("prompt") or "", payload.get("session_id")
        if not query.strip():
            return
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
                             "sent": bool(text), "raw_chars": len(raw)}, ensure_ascii=False) + "\n")

    if not text:
        return              # нечему всплывать, молчим
    print(text)
    if not args.no_record:
        try:
            note_injection(session_id, text, door=door)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--hook" not in sys.argv:
            raise
    sys.exit(0)
