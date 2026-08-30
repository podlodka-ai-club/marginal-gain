#!/usr/bin/env python3
"""Модуль 2, Понимание. Разбирает разговор на Episode и Fact из схемы xmemory.

Наивно, без модели: границей эпизода считается сообщение человека, всё, что
агент сделал до следующего сообщения, попадает в этот эпизод. Итог пишется
текстом, из которого xmemory раскладывает узлы графа.

Разбор идёт по отметке о прочитанном, а не по всему архиву. Прежде каждый
заход перепахивал все транскрипты целиком, и потому понимание звали руками:
в конце хода такому месту нет. Отметка своя у каждого хранилища — ровно по той
же причине, что у сохранения, см. state_path.
"""
import argparse, contextlib, json
from pathlib import Path

from domain import features, marks
from domain.measure import score_of
from infra import locks
from storage import port
from archive.transcripts import DAYS, TRANSCRIPTS, episodes_from_file, parse_time
from archive.extract import NOT_CODE, PREF_TOPICS, facts_of, fact_key
from infra.scrub import redact

# Извлечение фактов уехало в archive.extract, чтение — в archive.transcripts.
# остаются видимы отсюда намеренно: стенд research/lab зовёт их через
# `import understand as u`, и ломать его переносом незачем.
__all__ = ["NOT_CODE", "PREF_TOPICS", "facts_of", "fact_key", "episodes_from_file",
           "outcome_of", "render_episode", "weigh", "features_of", "score_of",
           "render_fact", "parse_time", "marked_or_guessed", "unread", "digest",
           "read_file", "one_episode", "touched", "tail_of", "advance"]

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


def mark_of(episodes, count, stat):
    """Отметка: докуда файл разобран и чем кончился разобранный кусок."""
    return {"size": stat.st_size, "inode": stat.st_ino, "episodes": count,
            "tail": tail_of(episodes[count - 1]) if count else "",
            "done": count == len(episodes)}


def advance(mark, unseen, taken, before):
    """Отметка после частичной записи: взяли не весь непрочитанный кусок."""
    if taken == len(unseen):
        return mark
    return dict(mark, episodes=mark["episodes"] - len(unseen) + taken,
                tail=tail_of(unseen[taken - 1]) if taken else before.get("tail", ""),
                done=False)


def unread(path, before):
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
    episodes = episodes_from_file(path)
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
    return episodes[start:], mark_of(episodes, len(episodes), stat)


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


def render_episode(ep):
    """Текст, из которого xmemory собирает Episode и связанные Event."""
    started = parse_time(ep["started_at"])
    project = Path(ep["cwd"]).name if ep["cwd"] else "unknown"
    title = " ".join(ep["request"].split())[:80]
    lines = [
        "Episode %d of session %s." % (ep["number"], ep["session_id"]),
        "title: %s" % title,
        "project: %s" % project,
        "working_directory: %s" % (ep["cwd"] or "unknown"),
        "git_branch: %s" % (ep["branch"] or "none"),
        "started_at: %s" % (ep["started_at"] or "unknown"),
        "ended_at: %s" % (ep["ended_at"] or "unknown"),
        "outcome: %s" % outcome_of(ep),
    ]
    if started:
        lines.append("hour_of_day: %d" % started.hour)
        lines.append("day_of_week: %s" % DAYS[started.weekday()])
    summary = ["Человек попросил: %s" % " ".join(ep["request"].split())[:600]]
    if ep["files"]:
        summary.append("Правились файлы: %s." % ", ".join(ep["files"][:15]))
    if ep["commands"]:
        summary.append("Запускались команды: %s." % "; ".join(ep["commands"][:10]))
    if ep["errors"]:
        summary.append("Упирались в: %s" % ep["errors"][0])
    if ep["replies"]:
        summary.append("Итог: %s" % " ".join(ep["replies"][-1].split())[:600])
    lines.append("summary: %s" % " ".join(summary))
    return redact("\n".join(lines))


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
            episodes, mark = unread(path, state["files"].get(str(path), {}))
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
    """Один эпизод и его факты. Отдаёт новое число записанных целиком."""
    text = render_episode(ep)
    if not dry:
        door.write(text)
    if verbose:
        title = text.split("title: ")[1].splitlines()[0]
        print("EPISODE %d %s | %s" % (ep["number"], outcome_of(ep), title[:70]))
    found, dropped = marked_or_guessed(ep)
    got["lost"] += sum(dropped.values())           # разметка была, писать нельзя
    for fact, key in found:
        rec = weights.get(key) or {"n": 1, "last": ep["ended_at"],
                                   "projects": set()}
        score = score_of(rec, newest)
        if score < min_score:
            got["skipped"] += 1
            continue
        if not dry:
            door.write(render_fact(*fact, score=score, rec=rec))
        got["facts"] += 1
        if verbose:
            feats = features_of(rec)
            print("   FACT %.2f [%s/%s] x%d %s %s"
                  % (score, fact[0], fact[2], rec["n"], fact[3][:80],
                     " ".join("%s=%.3f" % (k, v) for k, v in feats.items())))
    # Счётчик двигается вместе с отметкой и по той же причине: оборвись запись
    # на фактах, эпизод не записан целиком и считать его нечестно.
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
