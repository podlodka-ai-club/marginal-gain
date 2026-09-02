#!/usr/bin/env python3
"""Точка MessageDisplay: что видит человек, пока печатается ответ.

Харнесс отдаёт хуку очередную порцию завершённых строк ответа и печатает то,
что хук вернул. Запись разговора и контекст модели при этом не трогаются —
значит служебный блок остаётся там, откуда его читает разбор, и не доезжает
до человека. Это единственная точка, где такое возможно: `Stop` срабатывает,
когда ответ уже на экране.

Контракт харнесса (Claude Code 2.1.152 и новее):

    вход:  {"turn_id":…, "message_id":…, "index":…, "final":…, "delta":…}
    выход: {"hookSpecificOutput": {"hookEventName": "MessageDisplay",
                                   "displayContent": "…"}}
    ненулевой код возврата или пустой вывод — печатается исходная дельта.

Отсюда правило поломки: молчать. Испорченный вывод виден в каждом разговоре,
поэтому любая неожиданность здесь означает «печатай как было», а не «покажи
человеку кусок json».

Блок приходит несколькими дельтами, а хук на каждую запускается заново, и
памяти между запусками у него нет. Поэтому «мы внутри блока» лежит файлом,
названным по `message_id`: соседние ответы и соседние сессии друг другу не
мешают.
"""
import argparse, json, os, sys, time
from pathlib import Path

from domain import marks
from infra import config

STATE = config.state_dir() / "display"

# Сколько живёт забытая отметка. Ответ обрывается, `final` не приходит, и
# файл остаётся навсегда; следующий ответ с тем же message_id невозможен, так
# что старое просто мусор. Час — с запасом на самый долгий ход.
STALE_SECONDS = 3600


def state_file(message_id):
    """Отметка «мы внутри блока» для одного ответа.

    Имя чистим: `message_id` приходит снаружи, и путь из него собирать нельзя.
    """
    name = "".join(c for c in str(message_id or "unknown")
                   if c.isalnum() or c in "-_")[:64] or "unknown"
    return STATE / name


def forget_stale(now=None):
    """Убрать отметки, которые никто не закрыл. Зовётся на первой дельте."""
    now = now or time.time()
    try:
        for path in STATE.iterdir():
            if now - path.stat().st_mtime > STALE_SECONDS:
                path.unlink()
    except OSError:
        pass


def visible(delta, inside, scheme):
    """Видимая часть дельты и новое состояние «внутри блока».

    Работаем построчно: харнесс отдаёт завершённые строки, а маркеры стоят на
    своих строках. Эвристики по виду строк нет — только маркеры, иначе однажды
    срежется ответ человеку.
    """
    out = []
    for line in (delta or "").splitlines(keepends=True):
        if inside:
            if scheme.end in line:
                inside = False
            continue
        if scheme.begin in line:
            inside = True
            continue
        out.append(line)
    return "".join(out), inside


def answer(payload):
    """Ответ хука на одну дельту или None, если менять нечего.

    None означает «печатай как было»: пустой вывод для харнесса это отсутствие
    решения. Отдавать пустую строку в `displayContent`, когда мы ничего не
    срезали, нельзя — это стёрло бы с экрана обычный текст.
    """
    if not config.hide_marks():
        return None
    scheme = marks.scheme()
    delta = payload.get("delta") or ""
    marker = state_file(payload.get("message_id"))
    if not payload.get("index"):
        forget_stale()
    inside = marker.exists()
    shown, inside_now = visible(delta, inside, scheme)
    if inside_now and not payload.get("final"):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
    elif marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass
    if shown == delta and not inside:
        return None            # ничего не срезали, решение не нужно
    return {"hookSpecificOutput": {"hookEventName": "MessageDisplay",
                                   "displayContent": shown}}


def main():
    ap = argparse.ArgumentParser(description="Срезание служебного блока с экрана")
    ap.add_argument("--markers", action="store_true",
                    help="напечатать маркеры схемы: начало и конец, по строке")
    args = ap.parse_args()
    if args.markers:
        scheme = marks.scheme()
        print(scheme.begin)
        print(scheme.end)
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    got = answer(payload)
    if got is not None:
        print(json.dumps(got, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass                   # молчание печатает исходную дельту
    sys.exit(0)
