#!/usr/bin/env python3
"""Модуль 1, Сохранение. Всё содержимое разговора уходит в хранилище.

Никакого отбора, никакой обрезки. Единственное исключение это затирание
секретов: токены и ключи наружу не уходят никогда.

Запись структурная: разговор ложится строкой Session, каждая реплика, команда
и результат — строкой Event, между ними ставится связь. Раньше отсюда уходила
сплошная проза, и ключ выводил экстрактор: при промахе записи схлопывались, а
на хранилище без экстрактора не появлялось ни одного Event. Ключ задан здесь.

Текстовый путь остался запасным: консоль структурной записи не умеет.
"""
import argparse, json, os
from pathlib import Path

from domain import models
from infra import config
from storage import port
from archive.transcripts import TRANSCRIPTS, read_new, when

# Книжка учёта своя: что уже прочитано и доставлено. Разбор транскрипта
# лежит в archive, состояния там нет.
STATE = config.state_dir() / "save-state.json"


def state_path():
    """Книжка учёта своя у каждого хранилища.

    Отметка говорит только «докуда файл разобран», и одна на всех она значит
    «разобран для одного — считается за всех». С тех пор как ход пишет в
    локальную базу, а ручной прогон уходит в сеть, общая отметка кормила бы
    только первого: хук срабатывает каждые несколько минут и выигрывает эту
    гонку всегда, а сетевой прогон приходил бы к дочитанному архиву.

    Имя хранилища читается при вызове, а не при импорте: путь наружу тоже
    выбирается в момент вызова door(), и книжка обязана идти за ним.
    """
    return STATE.with_name("%s-%s%s" % (STATE.stem, port.store_name(), STATE.suffix))


def load_state():
    """Отметки хранилища, а при их отсутствии — прежняя общая книжка.

    Наследство читается, но не переписывается: первая же запись ляжет в свой
    файл. Иначе разделение отметок означало бы, что архив разбирают заново с
    начала — сорок две тысячи событий по второму разу.
    """
    target = state_path()
    source = target if target.exists() else STATE
    state = json.loads(source.read_text()) if source.exists() else {}
    state.setdefault("files", {})
    state.setdefault("sessions", {})
    return state


def save_state(state):
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(target)


def event_of(item):
    """Запись Event по схеме. Ключ — разговор плюс порядковый номер."""
    hour, day = when(item["at"])
    return models.Event(
        session_id=item["session"] or "unknown",
        sequence_number=item["seq"],
        event_type=item["event_type"],
        content=item["text"],
        tool_name=item["tool"],
        occurred_at=item["at"] or None,
        project=Path(item["cwd"]).name if item["cwd"] else None,
        working_directory=item["cwd"] or None,
        git_branch=item["branch"] or None,
        hour_of_day=hour,
        day_of_week=day)


def session_of(item):
    """Запись Session. Разговор один на много событий, ключ у него один."""
    return models.Session(
        session_id=item["session"] or "unknown",
        project=Path(item["cwd"]).name if item["cwd"] else None,
        working_directory=item["cwd"] or None,
        git_branch=item["branch"] or None,
        started_at=item["at"] or None)


def send(items, door=None):
    """Структурная запись пачкой: строки и связи между ними одним вызовом.

    Разговор пишется раньше своих событий: связь ссылается на обе стороны по
    ключу, и порядок в списке мутаций сохраняется.
    """
    records, relations, seen = [], [], {}
    for item in items:
        event = event_of(item)
        session = seen.get(event.session_id)
        if session is None:
            session = seen[event.session_id] = session_of(item)
            records.append(session)
        records.append(event)
        relations.append(models.link("session_events", session=session, event=event))
    if not records:
        return 0
    (door or port.door()).write_objects(records, relations)
    return len(records)


def deliver(items, door=None):
    """Структурная запись, а при её отсутствии — текст.

    Спрашиваем дверь, что она умеет, а не как её зовут. Сравнение с "cli"
    означало, что пятый путь без структурной записи молча роняет разговор:
    ошибка не поймана — значит, потеряно.
    """
    door = door or port.door()
    if hasattr(door, "write_objects"):
        return send(items, door)
    for item in items:
        door.write(render(item))
    return len(items)


def render(item):
    where = Path(item["cwd"]).name if item["cwd"] else "неизвестно"
    head = "Разговор %s, проект %s, ветка %s, время %s. %s:" % (
        item["session"] or "?", where, item["branch"] or "нет",
        item["at"] or "неизвестно", item["role"])
    return "%s\n%s" % (head, item["text"])


def ingest(files, limit=None, dry=True, reset=False, verbose=False, door=None):
    """Проход по файлам архива. Отдельно от разбора доводов, чтобы звать извне.

    Потребителю очереди нужен ровно этот проход, а не командная строка вокруг
    него. Отметка о прочитанном общая, поэтому повторный заход по тому же
    файлу ничего не задваивает.

    Дверь берём один раз на весь проход и передаём дальше: иначе на каждом
    файле открывалась бы своя, и путь наружу мог бы смениться посреди прохода
    вместе с окружением.
    """
    if not dry:
        door = door or port.door()
    state = load_state()
    if reset:
        for path in files:
            state["files"].pop(str(path), None)
        state["sessions"] = {}

    # Номер события уникален в пределах разговора, а не файла. Разговор часто
    # разложен по нескольким файлам архива: 59 из 153 на 2026-08-26. Пока номер
    # считался по файлу, второй файл начинал с нуля и затирал события первого
    # по ключу (session_id, sequence_number).
    sessions = dict(state.get("sessions") or {})
    sent, stopped, unfinished, reached = 0, False, [], 0
    for path in files:
        reached += 1
        before = state["files"].get(str(path), {})
        items, _ = read_new(path, before)
        # Отметка, до которой всё разобрано. Двигается только на границе
        # строки: остановка посреди строки либо пропустила бы её соседей,
        # либо выдала бы уже отданное вторым разом.
        resume, batch = before, []
        for item in items:
            # Потолок проверяем на границе строки, а не между записями.
            # Перебор на несколько записей безобиден, разрыв строки — нет.
            if limit and sent >= limit and resume is not before:
                stopped = True
                break
            talk = item["session"] or "unknown"
            item["seq"] = sessions.get(talk, 0)
            sessions[talk] = item["seq"] + 1
            batch.append(item)
            sent += 1
            if item["last_in_line"]:
                resume = item["cursor"]
            if verbose:
                print("%s #%d %s %d симв."
                      % (path.name, item["seq"], item["role"], len(item["text"])))
        # Записи уходят пачкой на файл, и только потом двигается отметка:
        # если запись не удалась, исключение долетит сюда и отметка останется
        # прежней. Иначе разговор считался бы сохранённым, не будучи им.
        if not dry and batch:
            deliver(batch, door)
            # Счётчики и отметка двигаются вместе и только после успешной
            # записи. Раньше счётчики шли вперёд, а отметка при сработавшем
            # потолке стояла — и файл перечитывался с начала, ложась заново
            # под новыми ключами (session_id, sequence_number).
            state["sessions"] = dict(sessions)
            state["files"][str(path)] = resume
            save_state(state)
        elif not dry:
            # Читать было нечего: отметку всё равно закрепляем, иначе пустой
            # проход по дочитанному файлу каждый раз открывал бы его заново.
            state["files"][str(path)] = resume
            save_state(state)
        if resume is before or resume.get("offset", 0) < path.stat().st_size:
            unfinished.append(path)
        if stopped:
            break
    # Недочитанные называем поимённо: потребитель очереди возвращает их себе,
    # иначе они пропали бы вместе со снятой подменой. Файлы, до которых из-за
    # потолка вовсе не дошли, недочитаны тем более.
    unfinished += [p for p in files[reached:] if p not in unfinished]
    return {"sent": sent, "unfinished": unfinished}


def main():
    ap = argparse.ArgumentParser(description="Сохранение разговоров в xmemory")
    ap.add_argument("--send", dest="dry", action="store_false", default=True,
                    help="реально писать в xmemory (по умолчанию холостой прогон)")
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--reset", action="store_true", help="перечитать выбранные файлы с начала")
    ap.add_argument("--limit", type=int, help="остановиться после стольких записей")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    scope = args.only or config.only()
    if scope:
        files = [f for f in files if scope in str(f)]
    got = ingest(files, limit=args.limit, dry=args.dry,
                 reset=args.reset, verbose=args.verbose)

    print("файлов %d, записей %d, недочитано %d, режим %s"
          % (len(files), got["sent"], len(got["unfinished"]),
             "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
