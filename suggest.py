#!/usr/bin/env python3
"""Модуль 3, Подсказка. Спрашивает xmemory и отдаёт агенту то, что прошло порог.

Своей головы у модуля нет: ценность оценивает понимание, здесь только порог.
Адресат подсказки это агент, а не человек, поэтому лишний текст не просто
бесполезен, он засоряет контекст агента и уводит его в сторону. Молчание это
нормальный и частый исход.
"""
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

import xmem

LOG = Path.home() / ".local" / "state" / "memory-encoder" / "suggest-log.jsonl"

MIN_SCORE = 0.5      # ниже этого факт не подтверждён повторением, см. ADR 0002
MAX_ITEMS = 5        # больше пяти внимание модели размазывается
MAX_CHARS = 1200     # потолок на весь кусок, который уходит в контекст агента

SCORE = re.compile(r"Оценка уверенности:\s*([0-9]+(?:\.[0-9]+)?)")


def pieces(answer):
    """Ответ памяти разбираем на куски и достаём оценку каждого."""
    try:
        data = json.loads(answer)
        body = data.get("answer", answer)
    except (json.JSONDecodeError, AttributeError):
        body = answer
    if isinstance(body, list):
        chunks = [c.get("content", str(c)) if isinstance(c, dict) else str(c) for c in body]
    else:
        chunks = re.split(r"\n\s*\n", str(body))
    out = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        found = SCORE.search(text)
        out.append((float(found.group(1)) if found else None, text))
    return out


def gate(items, min_score=MIN_SCORE, max_items=MAX_ITEMS, max_chars=MAX_CHARS):
    """Порог. Кусок без оценки не пропускаем: неизвестное не лучше слабого."""
    kept = sorted(((s, t) for s, t in items if s is not None and s >= min_score), reverse=True)
    out, size = [], 0
    for score, text in kept[:max_items]:
        clean = SCORE.sub("", text).strip()
        if size + len(clean) > max_chars:
            break
        out.append((score, clean))
        size += len(clean)
    return out


def render(kept):
    """Формат под агента: сжатые утверждения, без обращений и предисловий."""
    lines = ["Из памяти прошлых разговоров:"]
    for score, text in kept:
        lines.append("- %s (уверенность %.2f)" % (" ".join(text.split()), score))
    return "\n".join(lines)


def suggest(query, mode="single", min_score=MIN_SCORE):
    answer = xmem.read(query, mode=mode)
    kept = gate(pieces(answer), min_score=min_score)
    return render(kept) if kept else "", kept, answer


def note_injection(session_id, text):
    """MemoryInjection: что подставили. Помогло ли, узнаем потом."""
    xmem.write("\n".join([
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

    try:
        text, kept, raw = suggest(query, args.mode, args.min_score)
    except Exception:
        if args.hook:
            return          # молчим: подсказка не имеет права ломать разговор
        raise

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"query": query[:300], "kept": len(kept),
                             "sent": bool(text), "raw_chars": len(raw)}, ensure_ascii=False) + "\n")

    if not text:
        return              # нечему всплывать, молчим
    print(text)
    if not args.no_record:
        try:
            note_injection(session_id, text)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--hook" not in sys.argv:
            raise
    sys.exit(0)
