#!/usr/bin/env python3
"""Тройка про питание: одна задача, три версии памяти, три несовместимых ответа.

Запуск: python3 -m pytest tests/test_diet_triple.py -q

Обычная пара доказывает влияние факта разницей двух рук: с памятью и без.
Слабое место у неё одно — угадывание. Список покупок можно написать разумно, ни
о чём не помня, и он сойдётся с ожиданием случайно (так и вышло на первом живом
прогоне с овсянкой, см. `COINCIDED` в `eval/live.py`).

Тройка это закрывает. Задача у трёх пар дословно одна, а память разная:
вегетарианец, веган, мясоед. Правильные ответы исключают друг друга — список,
верный для одной версии, для двух других неверен. Значит попасть надо не в
«разумный ответ», а в конкретную версию из трёх, и угадать нельзя: угадывание
даёт один ответ на все три и проваливает как минимум две.

Свойства:

1. Три пары есть в наборе, у всех `aim: apply`, задача у всех дословно одна,
   а реплики первой сессии и место у каждой свои.
2. Ответ, собранный из меню одной версии, проходит её критерий и проваливает
   критерии двух других — в обе стороны, для всех шести упорядоченных пар.
3. Нейтральный список (ничего диетического) не проходит ни одну из трёх:
   угадывание не засчитывается.
4. Ожидания и запреты — куски слов, а не фразы: одно слово без пробелов, и
   вхождение ловит любую падежную форму.
5. Семь прежних пар на месте, id уникальны, конверт считает столько же, сколько
   в списке.

Мутации, на которых проверки обязаны краснеть:
  * задачу одной из трёх переписали              → TestTripleIsInTheSet
  * ожидание/запрет ослабили так, что версии      → TestVersionsAreMutuallyExclusive
    перестали исключать друг друга
  * критерий стал проходить на нейтральном списке → TestGuessingCannotPass
  * ожидание записали целой фразой или словоформой→ TestCriteriaAreWordStems
  * прежнюю пару выкинули или переименовали       → TestTheOldSevenAreStillThere
"""
import os
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import evaluate, pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

FAST = settings(deadline=None, max_examples=100)

VEGETARIAN = "питание-вегетарианец"
VEGAN = "питание-веган"
CARNIVORE = "питание-мясоед"
TRIPLE = (VEGETARIAN, VEGAN, CARNIVORE)

# Прежний набор: семь пар, вокруг которых тройка добавляется.
OLD_SEVEN = ("завтрак", "город", "забор", "макбук", "овсянка-ужин",
             "макбук-не-в-тему", "кровь")

# Меню версии: из чего человек этой версии составил бы список покупок. Здесь,
# а не в наборе: набор несёт критерий, меню — то, чем критерий проверяется.
# Ни одно меню не содержит слов, которых эта версия не купила бы.
MENU = {
    # Тофу в вегетарианском меню стоит нарочно: вегетарианец его покупает, и
    # без него проверка не увидела бы, чем именно версия вегана отличает свой
    # ответ от соседнего.
    VEGETARIAN: ["творог", "творога", "яйца", "сыр", "молоко", "гречка",
                 "овощи", "хлеб", "тофу"],
    VEGAN: ["тофу", "нут", "чечевица", "гречка", "овощи", "хлеб",
            "овсяное питьё"],
    CARNIVORE: ["фарш", "фарша", "индейка", "индейки", "говядина", "гречка",
                "овощи", "творог"],
}

# Список, который пишет агент без памяти. Слова взяты не из головы, а из
# снятых прогонов голой руки (`eval-triple-answers.json`): обычный недельный
# список это хлеб, яйца, молоко, творог, курица, говядина, рыба и овощи.
NEUTRAL = ["хлеб", "яйца", "молоко", "сыр", "творог", "рыба", "картофель",
           "морковь", "гречка", "яблоки", "чай", "фарш"]

# Мясо в списке без памяти было всегда — во всех снятых ответах голой руки.
# Поэтому оно есть в каждом сгенерированном списке: убери его, и «нейтральный»
# список перестанет быть тем, что пишет агент, а станет тем, что удобно тесту.
NEUTRAL_ALWAYS = ("курица", "говядина")

# Слова, вокруг которых построены критерии, во всех формах, какие встретятся в
# ответе. Кусок слова обязан ловиться в каждой: запиши критерий словоформой —
# и «творог» в ответе не совпадёт с ожиданием «творогом».
FORMS = {
    "творог": ("творог", "творога", "творогу", "творогом", "твороге"),
    "тофу": ("тофу",),
    "фарш": ("фарш", "фарша", "фаршу", "фаршем", "фарше"),
    "индейка": ("индейка", "индейки", "индейку", "индейкой", "индейке"),
    "курица": ("курица", "курицы", "курицу", "курицей", "куриное",
               "куриные", "куриная", "курином"),
}


def triple_of(items):
    return {item["id"]: item for item in items if item["id"] in TRIPLE}


def load_items():
    return pairs.load(HOUSEHOLD)[1]


def a_list_from(words, must=()):
    """Список покупок из этих слов; названное в `must` в нём есть всегда."""
    return st.lists(st.sampled_from(words), min_size=1, max_size=8).map(
        lambda picked: "Список покупок на неделю: %s."
                       % ", ".join(list(must) + picked))


def passes(pair, answer):
    """Тем же судьёй, каким прогон судит живой ответ, — не своей копией."""
    return evaluate.judge(pair, answer, known="", error=None)["ok"]


class Base(unittest.TestCase):
    """Тройка целиком — предусловие каждой проверки ниже.

    Без него класс, перебирающий версии циклом, зеленел бы на пустом словаре:
    выкини тройку из набора — и «у всех трёх aim: apply» проходит, потому что
    трёх нет вовсе.
    """

    def setUp(self):
        self.triple = triple_of(load_items())
        self.assertEqual(sorted(TRIPLE), sorted(self.triple),
                         "тройка неполна, проверять нечего: %s"
                         % sorted(self.triple))


class TestTripleIsInTheSet(Base):
    """Три пары, одна задача, разные факты."""

    def test_every_version_asks_the_agent_to_apply(self):
        for id_, pair in self.triple.items():
            self.assertEqual("apply", pair["aim"], "%s: не apply" % id_)

    def test_the_task_is_word_for_word_the_same_in_all_three(self):
        said = {id_: pair["task"]["say"] for id_, pair in self.triple.items()}
        self.assertEqual(1, len(set(said.values())),
                         "задача разошлась по версиям: %s" % said)

    def test_the_facts_of_the_three_versions_do_not_overlap(self):
        lines = {id_: {turn["say"] for turn in pair["tell"]}
                 for id_, pair in self.triple.items()}
        for one in TRIPLE:
            for other in TRIPLE:
                if one == other:
                    continue
                self.assertEqual(set(), lines[one] & lines[other],
                                 "%s и %s говорят одно и то же" % (one, other))

    def test_every_version_lives_in_its_own_place(self):
        """Общее место склеило бы три противоречащих факта в одной выдаче."""
        places = [pair["tell"][0]["place"] for pair in self.triple.values()]
        self.assertEqual(len(places), len(set(places)),
                         "версии стоят в одном месте: %s" % places)

    def test_every_version_says_why_its_fact_matters(self):
        for id_, pair in self.triple.items():
            self.assertTrue((pair.get("matters") or "").strip(),
                            "%s: не сказано, при каком условии факт значим" % id_)


class TestVersionsAreMutuallyExclusive(Base):
    """Ответ одной версии проваливает критерии двух других. Все шесть сторон."""

    @given(data=st.data())
    @FAST
    def test_an_answer_from_one_menu_passes_only_its_own_version(self, data):
        mine = data.draw(st.sampled_from(TRIPLE))
        pair = self.triple[mine]
        answer = data.draw(a_list_from(MENU[mine], must=pair["expect"]))
        self.assertTrue(passes(pair, answer),
                        "%s не принял свой же ответ: %s" % (mine, answer))
        for other in TRIPLE:
            if other == mine:
                continue
            self.assertFalse(
                passes(self.triple[other], answer),
                "ответ версии %s прошёл критерий версии %s: %s"
                % (mine, other, answer))


class TestGuessingCannotPass(Base):
    """Разумный список без памяти не проходит ни одну из трёх версий."""

    @given(answer=a_list_from(NEUTRAL, must=NEUTRAL_ALWAYS))
    @FAST
    def test_a_neutral_shopping_list_fails_every_version(self, answer):
        for id_, pair in self.triple.items():
            self.assertFalse(passes(pair, answer),
                             "%s прошла на списке без памяти: %s" % (id_, answer))


class TestCriteriaAreWordStems(Base):
    """Ожидания и запреты — куски слов: проверка вхождением строки работает."""

    def tokens(self):
        for id_, pair in self.triple.items():
            for token in (pair.get("expect") or []) + (pair.get("forbid") or []):
                yield id_, token

    def test_no_token_is_a_phrase(self):
        for id_, token in self.tokens():
            self.assertNotIn(" ", token, "%s: ожидание фразой: %r" % (id_, token))
            self.assertTrue(token.isalpha(),
                            "%s: в куске слова не только буквы: %r" % (id_, token))

    @given(before=st.text(max_size=12), after=st.text(max_size=12))
    @FAST
    def test_every_token_catches_its_word_in_every_form(self, before, after):
        """Кусок слова ловит слово в любом падеже и в любом окружении.

        Спрашиваем не про сам кусок (это совпало бы с собой и на словоформе), а
        про слово, которое он обязан поймать: критерий, записанный «творогом»,
        мимо «творога» в ответе пройдёт молча.
        """
        for id_, token in self.tokens():
            named = [stem for stem, forms in FORMS.items()
                     if all(token.lower() in form.lower() for form in forms)]
            self.assertEqual(
                1, len(named),
                "%s: кусок %r не ловит ни одно слово целиком во всех формах "
                "(подошло: %s)" % (id_, token, named))
            for form in FORMS[named[0]]:
                said = "%sВзять %s в магазине.%s" % (before, form, after)
                self.assertIn(token.lower(), said.lower(),
                              "%s: форма %r не ловится куском %r"
                              % (id_, form, token))


class TestTheOldSevenAreStillThere(unittest.TestCase):
    """Тройка добавлена рядом с прежними парами, а не вместо них."""

    def test_every_old_pair_is_still_in_the_set(self):
        present = {item["id"] for item in load_items()}
        missing = [id_ for id_ in OLD_SEVEN if id_ not in present]
        self.assertEqual([], missing, "прежняя пара пропала: %s" % missing)

    def test_pair_ids_are_unique(self):
        ids = [item["id"] for item in load_items()]
        self.assertEqual(len(ids), len(set(ids)), "id повторяются: %s" % ids)

    def test_the_envelope_counts_what_the_list_holds(self):
        body, items = pairs.load(HOUSEHOLD)
        self.assertEqual(len(OLD_SEVEN) + len(TRIPLE), body["count"])
        self.assertEqual(body["count"], len(items))


if __name__ == "__main__":
    unittest.main()
