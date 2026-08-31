#!/usr/bin/env python3
"""Модуль 2, Понимание. Разбирает разговор на Episode и Fact из схемы xmemory.

Наивно, без модели: границей эпизода считается сообщение человека, всё, что
агент сделал до следующего сообщения, попадает в этот эпизод. Итог уходит
записями схемы: ключ эпизода и ключ факта задаём мы, и между ними ставится
связь episode_facts. Дверь без структурной записи получает тот же итог
прозой — её собирает та же запись схемы, см. render_episode.

Разбор идёт по отметке о прочитанном, а не по всему архиву. Прежде каждый
заход перепахивал все транскрипты целиком, и потому понимание звали руками:
в конце хода такому месту нет. Отметка своя у каждого хранилища — ровно по той
же причине, что у сохранения, см. state_path.
"""
import argparse, contextlib, json
from pathlib import Path

from domain import features, marks, models
from domain.measure import score_of
from infra import locks
from storage import port
from archive.transcripts import (TRANSCRIPTS, episodes_and_events,
                                 episodes_from_file, parse_time, when)
from archive.extract import NOT_CODE, PREF_TOPICS, facts_of, fact_key
from infra.scrub import redact

# Извлечение фактов уехало в archive.extract, чтение — в archive.transcripts.
# остаются видимы отсюда намеренно: стенд research/lab зовёт их через
# `import understand as u`, и ломать его переносом незачем.
__all__ = ["NOT_CODE", "PREF_TOPICS", "facts_of", "fact_key", "episodes_from_file",
           "outcome_of", "render_episode", "weigh", "features_of", "score_of",
           "render_fact", "parse_time", "marked_or_guessed", "unread", "digest",
           "read_file", "one_episode", "touched", "tail_of", "advance",
           "episode_of", "fact_of", "summary_of", "deliver"]

# Своя книжка учёта: докуда архив разобран пониманием. Отметка сохранения не
# годится — оно читает файл построчно, а понимание режет его на эпизоды.
STATE = Path.home() / ".local" / "state" / "memory-encoder" / "understand-state.json"


def state_path():
    """Книжка учёта своя у каждого хранилища.

    Отметка говорит «докуда разобрано», и одна на всех она значит «разобрано
    для одного — считается за всех». Ход пишет в локальную базу и срабатывает
    каждые несколько минут, ручной прогон уходит в сеть: общая отметка кормила
    бы только первого. То же правило и по той же причине, что в save.state_path.

    Имя хранилища читается при вызове, а не при импорте: путь наружу тоже
    выбирается в момент вызова door(), и книжка обязана идти за ним.

    Замок, в отличие от книжки, общий: он про писателя в базу, а не про то,
    докуда разобран архив, см. infra.locks.PASS.
    """
    return STATE.with_name("%s-%s%s" % (STATE.stem, port.store_name(), STATE.suffix))


def load_state():
    target = state_path()
    state = json.loads(target.read_text()) if target.exists() else {}
    state.setdefault("files", {})
    return state


def save_state(state):
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(target)


def touched(path, before):
    """Изменился ли файл с прошлого захода. Стоит одного stat, без разбора.

    Отделено от разбора нарочно: под потолком заход разбирает считанные файлы,
    а проверить обязан все. Складывай он весь архив в память ради потолка в
    сотню эпизодов — заход стоил бы как полный проход, а отметки нетронутых
    файлов остались бы прежними, и следующий ход перечитал бы всё заново.
    """
    stat = path.stat()
    return not (before.get("done") and before.get("inode") == stat.st_ino
                and before.get("size") == stat.st_size)


def tail_of(ep):
    """Отпечаток эпизода: по нему видно, дорос он с прошлого захода или нет.

    Берём то, что растёт вместе с эпизодом: время последнего события и число
    накопленного. Сравнивать сам текст незачем — он длинный, а меняется вместе
    с этими числами.
    """
    return "%s|%d|%d|%d|%d" % (ep["ended_at"], len(ep["files"]),
                               len(ep["commands"]), len(ep["replies"]),
                               len(ep["errors"]))


def mark_of(episodes, count, stat, bases=None, event_bases=None, event_counts=None):
    """Отметка: докуда файл разобран и чем кончился разобранный кусок.

    Вместе с отметкой лежит нумерация: с какого номера начинаются эпизоды
    каждого разговора в этом файле и сколько их тут. По этим двум числам
    восстанавливается счётчик разговора, см. counters_of.
    """
    return {"size": stat.st_size, "inode": stat.st_ino, "episodes": count,
            "tail": tail_of(episodes[count - 1]) if count else "",
            "bases": dict(bases or {}), "counts": dict(counts_of(episodes)),
            "event_bases": dict(event_bases or {}),
            "event_counts": dict(event_counts or {}),
            "done": count == len(episodes)}


def counts_of(episodes):
    """Сколько эпизодов каждого разговора в этом файле."""
    out = {}
    for ep in episodes:
        talk = ep["session_id"] or "unknown"
        out[talk] = out.get(talk, 0) + 1
    return out


def counters_of(state, bases="bases", counts="counts"):
    """Докуда занумерован каждый разговор. Складывается из отметок файлов.

    Отдельной книжки для счётчиков нет нарочно. Две книжки об одном и том же
    расходятся на первом же `--reset`: отметки выбранных файлов забыты, а
    счётчик помнит их номера, и следующий разбор начинает разговор с чужого
    места. Здесь забытая отметка сама освобождает свои номера.
    """
    out = {}
    for mark in (state.get("files") or {}).values():
        seen = mark.get(counts) or {}
        for talk, base in (mark.get(bases) or {}).items():
            out[talk] = max(out.get(talk, 0), base + seen.get(talk, 0))
    return out


def event_counters_of(state):
    """То же самое для событий: докуда занумерованы события разговора.

    Номера событий присваивает сохранение, а понимание их повторяет: связь
    эпизод — событие адресует событие ключом `разговор + номер`, и промах здесь
    означал бы связь на строку, которой в хранилище нет. Правило нумерации
    общее и лежит в одном месте, см. transcripts.record_of.
    """
    return counters_of(state, bases="event_bases", counts="event_counts")


def event_bases_of(counts, before, counters):
    """С какого номера идут события этого файла в каждом разговоре."""
    bases = dict(before.get("event_bases") or {})
    for talk in counts:
        bases.setdefault(talk, counters.get(talk, 0))
    return bases


def absolute_events(episodes, bases):
    """Номера событий эпизода — от начала разговора, а не от начала файла."""
    for ep in episodes:
        base = bases.get(ep["session_id"] or "unknown", 0)
        ep["events"] = [base + number for number in ep.get("events") or []]
    return episodes


def bases_of(episodes, before, counters):
    """С какого номера идут эпизоды этого файла в каждом разговоре.

    Присваивается один раз, при первом разборе файла, и дальше живёт в его
    отметке: иначе перечитанный хвостовой эпизод получил бы новый номер и лёг
    бы в хранилище вторым.
    """
    bases = dict(before.get("bases") or {})
    for talk in counts_of(episodes):
        bases.setdefault(talk, counters.get(talk, 0))
    return bases


def renumber(episodes, bases):
    """Номер эпизода — по разговору, а не по файлу.

    Ключ эпизода это разговор плюс номер. Разговор часто разложен по нескольким
    файлам архива (66 из 160 на 2026-08-31), и счёт внутри файла означал, что
    первый эпизод второго файла затирает первый эпизод первого: из 1944 эпизодов
    архива доезжало 1736. Ровно тем же способом и по той же причине считает
    номера событий сохранение, см. save.ingest.
    """
    seen = {}
    for ep in episodes:
        talk = ep["session_id"] or "unknown"
        seen[talk] = seen.get(talk, 0) + 1
        ep["number"] = bases.get(talk, 0) + seen[talk]
    return episodes


def advance(mark, unseen, taken, before):
    """Отметка после частичной записи: взяли не весь непрочитанный кусок."""
    if taken == len(unseen):
        return mark
    return dict(mark, episodes=mark["episodes"] - len(unseen) + taken,
                tail=tail_of(unseen[taken - 1]) if taken else before.get("tail", ""),
                done=False)


def unread(path, before, counters=None, event_counters=None):
    """Эпизоды файла, которых понимание ещё не разбирало, и отметка после них.

    Неизменившийся файл не открываем вовсе: узнать это стоит одного stat, а
    разобрать архив целиком — секунд. Конец хода зовёт понимание каждый раз, и
    заход, которому нечего делать, обязан стоить ничего.

    Хвостовой эпизод перечитывается, только если он правда дорос. На конце
    хода файл растёт всегда — отступай мы назад при каждом росте, каждый
    эпизод и каждый его факт ложились бы в хранилище дважды. Дорос ли хвост,
    говорит его отпечаток, см. tail_of.

    Файл, брошенный посреди разбора потолком, продолжается ровно с места:
    иначе потолок в один эпизод крутил бы один и тот же файл вечно.
    """
    stat = path.stat()
    if (before.get("done") and before.get("inode") == stat.st_ino
            and before.get("size") == stat.st_size):
        return [], before
    episodes, event_counts = episodes_and_events(path)
    bases = bases_of(episodes, before, counters or {})
    renumber(episodes, bases)
    event_bases = event_bases_of(event_counts, before, event_counters or {})
    absolute_events(episodes, event_bases)
    start = before.get("episodes", 0)
    if before.get("inode") != stat.st_ino or stat.st_size < before.get("size", 0):
        start = 0                       # файл подменили или обрезали
    elif start > len(episodes):
        # Файл переписали на месте: узел прежний, размер вырос, а эпизодов
        # стало меньше. Отметка указывает за конец — верить ей больше нельзя.
        start = 0
    elif start and stat.st_size != before.get("size") \
            and tail_of(episodes[start - 1]) != before.get("tail"):
        start -= 1                      # хвостовой эпизод дорос, берём его снова
    return episodes[start:], mark_of(episodes, len(episodes), stat, bases,
                                    event_bases, event_counts)


def marked_or_guessed(ep):
    """Факты эпизода с их ключами: сперва разметка модели, иначе шаблоны.

    Разметку предпочитаем не из веры, а из устройства: её ключ назвала модель,
    а шаблон ключ угадывает. Смешивать оба источника в одном эпизоде нельзя —
    один и тот же факт пришёл бы дважды под разными ключами и удвоил бы себе
    подтверждения. Пока разметки в архиве нет, работают прежние правила, и
    цифра по размеченным копится только вперёд.
    """
    facts, dropped = marks.facts_of(ep)
    if facts:
        return [(fact, marks.key(fact)) for fact in facts], dropped
    return [(fact, fact_key(*fact)) for fact in facts_of(ep)], dropped


def outcome_of(ep):
    if ep["errors"]:
        return "blocked"
    if ep["replies"] or ep["files"] or ep["commands"]:
        return "done"
    return "abandoned"


def summary_of(ep):
    """Пересказ эпизода одной строкой: просьба, что трогали, чем кончилось."""
    out = ["Человек попросил: %s" % " ".join(ep["request"].split())[:600]]
    if ep["files"]:
        out.append("Правились файлы: %s." % ", ".join(ep["files"][:15]))
    if ep["commands"]:
        out.append("Запускались команды: %s." % "; ".join(ep["commands"][:10]))
    if ep["errors"]:
        out.append("Упирались в: %s" % ep["errors"][0])
    if ep["replies"]:
        out.append("Итог: %s" % " ".join(ep["replies"][-1].split())[:600])
    return " ".join(out)


def episode_of(ep):
    """Запись Episode по схеме. Ключ — разговор плюс номер эпизода.

    Разговор без идентификатора в архиве встречается, а половина ключа пустой
    быть не может: зовём такой «unknown» — тем же словом и по той же причине,
    что сохранение в save.session_of.
    """
    hour, day = when(ep["started_at"])
    return models.Episode(
        session_id=ep["session_id"] or "unknown",
        episode_number=ep["number"],
        title=" ".join(ep["request"].split())[:80],
        summary=summary_of(ep),
        outcome=outcome_of(ep),
        project=Path(ep["cwd"]).name if ep["cwd"] else None,
        working_directory=ep["cwd"] or None,
        git_branch=ep["branch"] or None,
        started_at=ep["started_at"] or None,
        ended_at=ep["ended_at"] or None,
        hour_of_day=hour,
        day_of_week=day)


def fact_of(ep, fact):
    """Запись Fact по схеме. Ключ — тип, тема и охват; их назвали мы сами.

    Раньше факт уходил прозой, и ключ ему выводил разборщик на той стороне.
    Промах разборщика схлопывал разные факты в одну строку, и связать факт с
    эпизодом было не по чему: ключ, которого мы не знаем, в связь не поставить.
    """
    fact_type, subject, scope, content = fact
    return models.Fact(
        fact_type=fact_type, subject=subject, scope=scope, content=content,
        # Проект есть у проектного факта. Глобальный факт — про человека, а не
        # про проект, и приписать ему проект мало того что неправда: поиск
        # взвешивает поле project наравне с темой, и предпочтение всплывало бы
        # первым на любой вопрос про этот проект. Текстовый путь поля project
        # не пишет вовсе, см. render_fact.
        project=(Path(ep["cwd"]).name if ep["cwd"] and scope != "global" else None),
        updated_at=ep["ended_at"] or None)


def render_episode(ep):
    """Тот же эпизод прозой — для двери, которая структурной записи не умеет.

    Собирается из той же записи схемы, что уходит структурой. Считай оба
    описания порознь — они разъедутся молча, и хранилище перестанет сходиться
    само с собой: половина эпизодов легла бы по одним полям, половина по другим.
    """
    record = episode_of(ep)
    lines = [
        "Episode %d of session %s." % (record.episode_number, record.session_id),
        "title: %s" % record.title,
        "project: %s" % (record.project or "unknown"),
        "working_directory: %s" % (record.working_directory or "unknown"),
        "git_branch: %s" % (record.git_branch or "none"),
        "started_at: %s" % (record.started_at or "unknown"),
        "ended_at: %s" % (record.ended_at or "unknown"),
        "outcome: %s" % record.outcome,
    ]
    if record.hour_of_day is not None:
        lines.append("hour_of_day: %d" % record.hour_of_day)
        lines.append("day_of_week: %s" % record.day_of_week)
    lines.append("summary: %s" % record.summary)
    return redact("\n".join(lines))


def deliver(ep, episode, facts, door):
    """Структурная запись, а при её отсутствии — текст.

    Спрашиваем дверь, что она умеет, а не как её зовут: тем же правилом и по
    той же причине, что в save.deliver — сравнение с именем «cli» роняло бы
    пятый путь без структурной записи молча.

    Эпизод, его факты и связи между ними уходят одним вызовом. Порядок в
    списке сохраняется, поэтому связь ставится после обеих строк, которые она
    соединяет.
    """
    if hasattr(door, "write_objects"):
        # Разговор идёт первой записью: связь ссылается на обе стороны по
        # ключу, и порядок в списке мутаций сохраняется. Без этой связи эпизод
        # был островом — события разговора лежали отдельно, эпизоды отдельно,
        # и вопрос «что было в этом эпизоде» не собирался ничем.
        session = models.Session(session_id=episode.session_id)
        # События эпизода уже лежат в хранилище: их пишет сохранение, и здесь
        # мы адресуем их ключом, а не создаём заново. Номер события общий —
        # правило нумерации одно на два модуля, см. transcripts.record_of.
        events = [models.Event(session_id=episode.session_id, sequence_number=number)
                  for number in ep.get("events") or []]
        return door.write_objects(
            [session, episode] + [record for record, _ in facts],
            [models.link("session_episodes", session=session, episode=episode)]
            + [models.link("episode_events", episode=episode, event=event)
               for event in events]
            + [models.link("episode_facts", episode=episode, fact=record)
               for record, _ in facts])
    door.write(render_episode(ep))
    for _, line in facts:
        door.write(line)


def weigh(files):
    """Мера факта: сколько раз подтверждён, когда в последний раз, в скольких проектах.

    Считается по всему архиву, без сети. Порог отсекает то, что встретилось
    один раз и давно: такое чаще шум, чем знание.
    """
    from collections import defaultdict
    seen = defaultdict(lambda: {"n": 0, "last": "", "projects": set()})
    for path in files:
        try:
            episodes = episodes_from_file(path)
        except OSError:
            # Архив живой: список снят раньше, чем считан вес, и файл могли
            # успеть удалить или переименовать. Пропавший вес это неточность
            # меры, упавший проход — потерянный ход целиком.
            continue
        for ep in episodes:
            project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
            for fact, key in marked_or_guessed(ep)[0]:
                rec = seen[key]
                rec["n"] += 1
                rec["projects"].add(project)
                if ep["ended_at"] > rec["last"]:
                    rec["last"] = ep["ended_at"]
    return seen


def features_of(rec):
    """Признаки узла факта. Считаются рядом с мерой и в неё не входят."""
    return features.compute(rec)


def render_fact(fact_type, subject, scope, content, score=None, rec=None):
    lines = ["Fact.", "content: %s" % content, "fact_type: %s" % fact_type,
             "subject: %s" % subject, "scope: %s" % scope]
    if rec is not None:
        lines.append("Подтверждений в архиве: %d, проектов: %d, последний раз: %s."
                     % (rec["n"], len(rec["projects"]), (rec["last"] or "неизвестно")[:10]))
    if score is not None:
        lines.append("Оценка уверенности: %.2f." % score)
    return "\n".join(lines)


def fresh_state(files, reset):
    """Книжка учёта, при надобности забывшая выбранные файлы."""
    state = load_state()
    if reset:
        for path in files:
            state["files"].pop(str(path), None)
    return state


def digest(files, archive=None, limit=None, dry=True, min_score=0.0,
           verbose=False, door=None, reset=False, lock=None):
    """Проход по архиву от отметки о прочитанном. Отдельно от разбора доводов.

    Конец хода зовёт ровно этот проход, а не командную строку вокруг него.

    Мера факта остаётся мерой по всему архиву: факт, встреченный в другом
    проекте, тоже подтверждение. Но считается она, только если есть что
    разбирать: заход, которому нечего делать, не должен открывать все триста
    файлов ради веса, который никому не понадобится.

    Отметка двигается только после записи. Холостой прогон ничего не пишет и
    потому ничего не закрывает: иначе проба на сухую отняла бы архив у записи.

    Замок берётся на запись, а не на весь заход. Он общий с очередью, а вес
    считается по всему архиву — полторы секунды и растёт. Держи мы замок всё
    это время, очередь следующего хода уходила бы ни с чем каждый раз.
    """
    state = fresh_state(files, reset)
    fresh = []
    for path in files:
        try:
            if touched(path, state["files"].get(str(path), {})):
                fresh.append(path)
        except OSError:
            continue                   # файл исчез между списком и проверкой

    got = {"episodes": 0, "facts": 0, "skipped": 0, "lost": 0, "broken": 0,
           "files": 0, "busy": False}
    if not fresh:
        return got

    weights = weigh(archive if archive is not None else files)
    newest = max((r["last"] for r in weights.values() if r["last"]), default="")
    door = door or (None if dry else port.door())

    held = locks.alone(lock) if lock else contextlib.nullcontext(True)
    with held as mine:
        if not mine:
            # Занято значит «уже пишут»: замок общий с очередью, потому что
            # база одна. Ждать нечего, следующий ход позовёт нас снова.
            got["busy"] = True
            return got
        # Отметку перечитываем под замком: пока считался вес, её мог сдвинуть
        # другой проход, и наша копия успела устареть.
        if lock:
            state = fresh_state(files, reset)
        write_all(fresh, state, got, weights, newest, limit, dry, min_score,
                  verbose, door)
    return got


def write_all(fresh, state, got, weights, newest, limit, dry, min_score,
              verbose, door):
    """Разбор и запись изменившихся файлов. Всё, ради чего держится замок."""
    for path in fresh:
        try:
            # Счётчики разговоров пересчитываются на каждом файле: предыдущий
            # мог занять номера того же разговора прямо в этом проходе.
            episodes, mark = unread(path, state["files"].get(str(path), {}),
                                    counters_of(state), event_counters_of(state))
        except OSError:
            continue
        if not episodes:
            # Разговор без единого сообщения человека — тоже разобранный
            # разговор. Не закрепи мы отметку, пустой файл открывался бы
            # заново каждый заход, а таких в архиве набирается.
            if not dry:
                state["files"][str(path)] = mark
                save_state(state)
            continue
        taken, bad = read_file(episodes, got, weights, newest, limit, dry,
                               min_score, verbose, door)
        if bad is not None:
            # Сбой записи не должен ни ронять проход, ни съедать остальные
            # файлы. Отметка встаёт на последнем записанном эпизоде, остаток
            # файла разберётся со следующего захода, остальные идут своим ходом.
            got["broken"] += 1
            print("сбой на разборе %s: %s" % (path.name, bad))
        got["files"] += 1
        if not dry:
            # Отметка на границе эпизода, а не записи: остановись мы посреди
            # эпизода, его факты разошлись бы с ним самим.
            state["files"][str(path)] = advance(
                mark, episodes, taken, state["files"].get(str(path), {}))
            save_state(state)
        if limit and got["episodes"] >= limit:
            break


def read_file(episodes, got, weights, newest, limit, dry, min_score, verbose, door):
    """Эпизоды одного файла в хранилище. Отдаёт, сколько записано, и сбой.

    Эпизод считается взятым только после своих фактов: оборвись запись
    посередине, отметка встанет перед ним и он перепишется целиком. Записать
    дважды дешевле, чем оставить эпизод без половины фактов.

    Сбой ловим здесь, а не выше. Вылети он из функции — вызывающий не узнал бы,
    сколько успело записаться, и откатил бы отметку в начало файла: на неровной
    сети каждый заход переписывал бы весь непрочитанный кусок заново.
    """
    taken = 0
    for ep in episodes:
        try:
            taken = one_episode(ep, taken, got, weights, newest, dry,
                                min_score, verbose, door)
        except Exception as bad:
            return taken, bad
        if limit and got["episodes"] >= limit:
            break
    return taken, None


def one_episode(ep, taken, got, weights, newest, dry, min_score, verbose, door):
    """Один эпизод и его факты. Отдаёт новое число записанных целиком.

    Отбор идёт до записи, запись — одним вызовом. Иначе факт уходил бы в
    хранилище раньше, чем стало известно, есть ли у него эпизод, и связь
    ставить было бы не на что.
    """
    episode = episode_of(ep)
    if verbose:
        # Вычистка нужна и здесь: в хранилище её делает сама запись
        # (Record.values), а отчёт печатается в журнал хука на каждом ходе.
        print("EPISODE %d %s | %s"
              % (ep["number"], episode.outcome, redact(episode.title or "")[:70]))
    found, dropped = marked_or_guessed(ep)
    got["lost"] += sum(dropped.values())           # разметка была, писать нельзя
    chosen = []
    for fact, key in found:
        rec = weights.get(key) or {"n": 1, "last": ep["ended_at"],
                                   "projects": set()}
        score = score_of(rec, newest)
        if score < min_score:
            got["skipped"] += 1
            continue
        chosen.append((fact_of(ep, fact),
                       render_fact(*fact, score=score, rec=rec)))
        if verbose:
            feats = features_of(rec)
            print("   FACT %.2f [%s/%s] x%d %s %s"
                  % (score, fact[0], fact[2], rec["n"], fact[3][:80],
                     " ".join("%s=%.3f" % (k, v) for k, v in feats.items())))
    if not dry:
        deliver(ep, episode, chosen, door)
    # Счётчики двигаются вместе с отметкой и по той же причине: оборвись запись,
    # эпизод не записан целиком и считать его — вместе с фактами — нечестно.
    got["facts"] += len(chosen)
    got["episodes"] += 1
    return taken + 1


def main():
    ap = argparse.ArgumentParser(description="Разбор разговоров на Episode и Fact")
    ap.add_argument("--send", dest="dry", action="store_false", default=True)
    ap.add_argument("--only", help="только транскрипты, чей путь содержит эту строку")
    ap.add_argument("--limit", type=int, help="потолок эпизодов за заход")
    ap.add_argument("--reset", action="store_true",
                    help="забыть отметку и разобрать выбранное заново")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="порог: факты ниже него не пишем. Эпизод при этом "
                         "считается разобранным, и вернуть отсеянное можно "
                         "только через --reset")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    every = sorted(TRANSCRIPTS.rglob("*.jsonl"))
    files = [f for f in every if args.only in str(f)] if args.only else every

    got = digest(files, archive=every, limit=args.limit, dry=args.dry,
                 min_score=args.min_score, verbose=args.verbose,
                 reset=args.reset, lock=locks.PASS)
    if got["busy"]:
        print("проход по архиву уже идёт, этот заход не нужен")
        return
    print("файлов с новым %d, эпизодов %d, фактов %d, отсеяно порогом %d, "
          "отброшено разметкой %d, файлов со сбоем %d, режим %s"
          % (got["files"], got["episodes"], got["facts"], got["skipped"],
             got["lost"], got["broken"], "холостой" if args.dry else "запись"))


if __name__ == "__main__":
    main()
