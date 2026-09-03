#!/usr/bin/env python3
"""Пары сессий: вход прогона. Стенд не знает, о чём набор.

Мера у прогона одна: **применил ли агент то, что ему сказали раньше**. Отсюда
и форма входа — пара сессий:

  сессия 1  человек что-то сообщает. Ход обычный, память наполняется сама.
  сессия 2  чистая сессия и задача, для решения которой это знание нужно.
            Сам факт в задаче не повторяется — иначе меряется чтение задачи,
            а не память.

Меряется применение, а не припоминание. Поэтому в паре нет «правильного
ответа»: есть то, что обязано в ответе оказаться, и то, чего в нём быть не
должно. И то и другое — данные пары, а не знание стенда.

Форма нарочно бытовая по словарю: `say`, `place`, `touched`. Ни «проект», ни
«файл», ни «ветка» здесь не названы — подставь набор про готовку, и стенд
отработает его так же, как набор про код. Ровно это и есть требование: стенд
принимает пары как данные и не зашивает в себя ни тематику, ни наш архив.

Запись пары:

    {
      "id": "kitchen-1",
      "aim": "apply",                       apply — знание нужно применить
                                            avoid — знание есть, но не к месту
      "tell": [                             сессия 1, ходов может быть несколько
        {"say": "Дома аллергия на арахис.",
         "place": "дом",                    где это сказано; необязательно
         "touched": ["меню.txt"],           что при этом правили; необязательно,
                                            стендом пока не используется
         "mark": "main",                    пометка обстановки; необязательно
         "ref": "ep-7"}                     то же самое сказано и в другой паре:
                                            сыграть один раз; необязательно
      ],
      "task": {"say": "Составь список покупок на неделю.",
               "place": "дом"},             откуда ставится задача; необязательно,
                                            по умолчанию — место первой сессии
      "expect": ["овсян"],                  что обязано быть в ответе
      "forbid": ["арахис"]                  чего в ответе быть не должно
    }

`expect` и `forbid` проверяются вхождением строки, и это надо помнить, когда
пишешь пару. Запрет ловит слово и в отрицании: агент, честно применивший
«арахис не покупаем», пишет «без арахиса» — и запрет `арахис` считает это
приплетённым. Формулируй применение положительным ожиданием, а запрет держи на
том, чему в ответе взяться неоткуда вовсе.

`aim` не выводится из наличия `expect`: отрицательный случай — это замысел
набора, а не форма записи. Догадайся стенд сам — и пара, где знание нужно
применить, но проверяется только запретом, читалась бы как отрицательная.

Пишешь новую пару — `expect`/`forbid` не выводятся из формулировки задачи, их
даёт только факт из сессии 1. Если ожидание можно угадать, не зная, что было
сказано раньше, пара меряет угадываемость, а не память. Проверка одна и она
прогоном: сыграй пару рукой без нашей памяти (`--arms bare` либо `both`) — не
прошла, значит применение действительно требовало факта; прошла — пара не
годится, как бы правдоподобно ни выглядела.

Мост к нынешнему набору лежит в `from_goldenset`. Он один знает про
`eval-cases.json` и `eval-script.json`; сам прогон про них не знает ничего.
Мерит этот мост дословное припоминание — такова природа старого набора, — и
именно поэтому он мост, а не формат.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Нынешний набор. Имена его файлов известны здесь и больше нигде: прогон о них
# не знает, и подстановка другого набора не требует в нём ни строчки.
LEGACY_CASES = ROOT / "eval-cases.json"
LEGACY_SCRIPT = ROOT / "eval-script.json"

VERSION = 1
KIND = "pairs"

AIMS = ("apply", "avoid")


class PairSetError(RuntimeError):
    """Набор пар собран не по этой форме. Читать его — мерить не то."""


def validate(item):
    """Одна пара. Ошибку называем полем, а не «неверный формат»."""
    if not isinstance(item, dict):
        raise PairSetError("пара должна быть записью, а не %s" % type(item).__name__)
    if not item.get("id"):
        raise PairSetError("у пары нет id")
    aim = item.get("aim")
    if aim not in AIMS:
        raise PairSetError("%s: aim это %s, а пришло %r"
                           % (item["id"], " или ".join(AIMS), aim))
    tell = item.get("tell")
    if not isinstance(tell, list):
        raise PairSetError("%s: сессия 1 должна быть списком ходов" % item["id"])
    # Пустая сессия 1 разрешена только отрицательной паре: она про то, что
    # память, наполненную соседями, к этой задаче приплетать не надо, и своего
    # наполнения ей не нужно. Положительной паре пустая сессия 1 означала бы
    # «примени то, чего никто не говорил» — случай, непроходимый по построению.
    if not tell and aim != "avoid":
        raise PairSetError("%s: сессия 1 пуста — применять будет нечего" % item["id"])
    for turn in tell:
        if not isinstance(turn, dict) or not (turn.get("say") or "").strip():
            raise PairSetError("%s: ход сессии 1 без реплики" % item["id"])
    task = item.get("task")
    if not isinstance(task, dict) or not (task.get("say") or "").strip():
        raise PairSetError("%s: нет задачи для сессии 2" % item["id"])
    if not (item.get("expect") or item.get("forbid")):
        raise PairSetError("%s: ни expect, ни forbid — исход не определён"
                           % item["id"])
    return item


def envelope(items, **meta):
    body = {"version": VERSION, "kind": KIND, "count": len(items)}
    body.update(meta)
    body["items"] = [validate(dict(item)) for item in items]
    return body


def dump(path, items, **meta):
    Path(path).write_text(
        json.dumps(envelope(items, **meta), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def load(path):
    """Набор пар из файла. Чужую сборку не читаем вовсе.

    То же правило и та же причина, что у золотого набора: цифра, снятая на
    наборе другой версии, несравнима с прежней, а выглядит точно так же.
    """
    where = Path(path)
    body = json.loads(where.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("kind") != KIND:
        raise PairSetError("%s это не набор пар" % where.name)
    if body.get("version") != VERSION:
        raise PairSetError("%s версии %s, а нужна %d"
                           % (where.name, body.get("version"), VERSION))
    items = body.get("items") or []
    if body.get("count") != len(items):
        raise PairSetError("%s: в конверте %s пар, в списке %d — файл оборван"
                           % (where.name, body.get("count"), len(items)))
    for item in items:
        validate(item)
    return body, items


# --- мост к золотому набору -------------------------------------------------

def from_goldenset(cases, turns):
    """Пары из нынешнего набора: случай плюс эпизоды, из которых он вырос.

    Единственное место, где известна форма `eval-cases.json` и
    `eval-script.json`. Прогону она неизвестна, и потому подстановка другого
    набора не требует ни строчки в нём.

    Что этот мост меряет, надо знать: старый набор проверяет вхождение строк в
    ответ, то есть дословное припоминание. Применение знания он не меряет и
    мерить не начнёт оттого, что его переложили в другую форму.

    Случай без источника — это «промолчи». Своего наполнения ему не нужно:
    к моменту вопроса память полна соседскими эпизодами, и проверяется ровно
    то, что она их сюда не приплетёт. Такая пара идёт с пустой сессией 1.
    """
    where = {}
    for turn in turns:
        where[(turn.get("session"), turn.get("episode"))] = turn
    out = []
    for case in cases:
        tell = []
        for source in case.get("source") or []:
            turn = where.get(tuple(source))
            if turn is None:
                continue
            tell.append({"say": turn.get("request") or "",
                         "place": turn.get("project") or "",
                         "touched": list(turn.get("files") or []),
                         "mark": turn.get("git_branch") or "",
                         # Один эпизод кормит несколько случаев. Помечаем его,
                         # чтобы прогон сыграл его один раз: сыграй по разу на
                         # каждую ссылку — и повторяемость знания вырастет от
                         # сборки набора, а не от работы.
                         "ref": "%s#%s" % (source[0], source[1])})
        tell = [turn for turn in tell if (turn["say"] or "").strip()]
        aim = "avoid" if (case.get("forbid") and not case.get("expect")) else "apply"
        if not tell and aim != "avoid":
            continue
        out.append({
            "id": case["id"],
            "aim": aim,
            "tell": tell,
            "task": {"say": case["query"]},
            "expect": list(case.get("expect") or []),
            "forbid": list(case.get("forbid") or []),
            "kind": case.get("kind", ""),
        })
    return out


def from_files(cases, script):
    """Пары из файлов старого набора. Здесь и только здесь известны их имена."""
    from eval import goldenset
    _, known = goldenset.load(cases, "cases")
    _, turns = goldenset.load(script, "script")
    return from_goldenset(known, turns)


def default():
    """Набор по умолчанию: нынешний, через мост."""
    return from_files(LEGACY_CASES, LEGACY_SCRIPT)
