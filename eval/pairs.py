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
      "forbid": ["арахис"],                 чего в ответе быть не должно
      "expect_kinds": ["животное неплотское"],  категория, хотя бы одно слово
                                            из которой обязано быть в ответе
      "forbid_kinds": ["животная плоть"],   категория, ни одного слова из
                                            которой в ответе быть не должно
      "matters": "спрашивают список покупок" необязательно: при каком условии
                                            факт вообще становится значимым для
                                            ответа. Стендом не читается — заметка
                                            для того, кто пишет или правит пару,
                                            и место, куда положить условие, а не
                                            держать его в голове
    }

Категория — это имя и закрытый список кусков слов; списки лежат в конверте
набора один раз (`kinds`) и общие для всех пар, которые их называют. Пара
называет имя, а не слова: перечень из двух-трёх угаданных слов на вопрос «было
в ответе мясо» не отвечает — запрет `фарш` пропускает индейку и говядину, и
пара засчитывается пройденной по недосмотру. Имя разрешает в слова загрузчик
(`load`), кладя разрешённое в саму пару полем `vocab`; судья про конверт не
знает, а словарь остаётся в одном месте.

Категория может назвать и **отменяющие** основы: тогда она записана не списком,
а парой `{"words": [...], "unless": [...]}`. Совпадение не считается, если само
слово или его сосед слева или справа начинается с отменяющей основы:
«растительное молоко» и «молочные заменители» животным продуктом не являются,
хотя основа `молок` в них есть. Это не исключение под конкретный ответ, а та же
ось категории — животное против растительного, — досказанная там, где основа
слова её не различает.

Словарь категории собирается один раз от смысла категории и проверяется на
полноту фактическими ответами. Дописать в него слово после того, как прогон не
сошёлся, — подгонка теста под результат: набор после неё не меряет ничего. Не
сошлось при полном словаре — виновата пара, а не словарь.

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
сказано раньше, пара меряет угадываемость, а не память.

Мало того, что рука без памяти на паре не проходит: недостающая деталь не в
счёт, если ответ и без неё остаётся верным — просто менее личным. Правило
пары — влияние факта на ответ, а не его упоминание. Без факта ответ обязан
стать неверным или неполным, а не просто другим по формулировке. Проверка
одна и она прогоном: сыграй пару рукой с памятью и рукой без неё (`--arms
both`) и сравни ответы по существу. Разошлись по сути — применение
действительно требовало факта, пара годится. Оба ответа верны, как бы
по-разному ни звучали, — пара не годится. Рука без памяти в прогоне уже
есть, второй прогон с подменённой памятью не нужен.

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

# Поля пары, называющие категории, и то, как разрешённое кладётся в пару.
KIND_FIELDS = (("expect_kinds", "expect"), ("forbid_kinds", "forbid"))
VOCAB = "vocab"


def words_of(kind):
    """Слова категории. Категория — список основ или запись со списком."""
    if isinstance(kind, dict):
        return list(kind.get("words") or [])
    return list(kind or [])


def unless_of(kind):
    """Отменяющие основы категории. У списка их нет вовсе."""
    if isinstance(kind, dict):
        return list(kind.get("unless") or [])
    return []


class PairSetError(RuntimeError):
    """Набор пар собран не по этой форме. Читать его — мерить не то."""


def _kind_names(item, field):
    """Имена категорий поля. Пустого списка и не-строк здесь быть не может.

    Отсутствие поля и пустой список — разные вещи: первое значит «категорий не
    называем», второе значит «называем, но ни одной», то есть запрет, который
    ничего не запрещает, или ожидание, которого нечем не сбыться.
    """
    if field not in item:
        return []
    names = item[field]
    if not isinstance(names, list) or not names:
        raise PairSetError("%s: %s это непустой список имён категорий, а пришло %r"
                           % (item.get("id"), field, names))
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise PairSetError("%s: имя категории в %s — непустая строка, а пришло %r"
                               % (item.get("id"), field, name))
    return names


def validate(item, kinds=None):
    """Одна пара. Ошибку называем полем, а не «неверный формат».

    `kinds` — словари категорий из конверта. Имя, которого там нет, — ошибка
    формы, а не пустая категория: опечатка, проглоченная молча, стала бы
    запретом, который ничего не запрещает, и пара прошла бы по недосмотру.
    """
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
    if not (item.get("expect") or item.get("forbid")
            or item.get("expect_kinds") or item.get("forbid_kinds")):
        raise PairSetError("%s: ни слова, ни категории — исход не определён"
                           % item["id"])
    matters = item.get("matters")
    if matters is not None and not (isinstance(matters, str) and matters.strip()):
        raise PairSetError("%s: matters, если есть, непустая строка" % item["id"])
    for field, _ in KIND_FIELDS:
        for name in _kind_names(item, field):
            if kinds is not None and name not in kinds:
                raise PairSetError(
                    "%s: категория %r не названа в конверте набора; известны: %s"
                    % (item["id"], name, ", ".join(sorted(kinds)) or "ни одной"))
    return item


def resolve(item, kinds):
    """Имена категорий пары — в закрытые словари, полем `vocab`.

    Одно место, где имя превращается в слова. Судья получает уже разрешённое и
    про конверт не знает — ровно как не знает про него сейчас; а словарь
    остаётся в конверте одной записью, и правка его не расходится по парам.
    """
    got = {}
    for field, side in KIND_FIELDS:
        got[side] = {name: {"words": words_of(kinds[name]),
                            "unless": unless_of(kinds[name])}
                     for name in item.get(field) or []}
    if not any(got.values()):
        return item
    return dict(item, **{VOCAB: got})


def bare(item):
    """Пара без разрешённого словаря: то, что кладётся в файл.

    Разрешённое в файл не пишется — иначе словарь размножится по парам, и
    правка одной копии разойдётся с остальными молча.
    """
    if VOCAB not in item:
        return item
    return {name: value for name, value in item.items() if name != VOCAB}


def envelope(items, kinds=None, **meta):
    body = {"version": VERSION, "kind": KIND, "count": len(items)}
    body.update(meta)
    if kinds:
        body["kinds"] = {name: (dict(kind) if isinstance(kind, dict)
                                else list(kind))
                         for name, kind in kinds.items()}
    body["items"] = [validate(bare(dict(item)), kinds=kinds) for item in items]
    return body


def dump(path, items, kinds=None, **meta):
    Path(path).write_text(
        json.dumps(envelope(items, kinds=kinds, **meta),
                   ensure_ascii=False, indent=2) + "\n",
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
    kinds = body.get("kinds") or {}
    out = []
    for item in items:
        validate(item, kinds=kinds)
        out.append(resolve(item, kinds))
    return body, out


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
