#!/usr/bin/env python3
"""Пара про питание: критерий категориями, а не перечнем угаданных слов.

Запуск: python3 -m pytest tests/test_diet_pair.py -q

Заводилось три версии одной задачи «составь список покупок на неделю» — память
вегетарианца, вегана и мясоеда, — и замысел был в том, что верные ответы
исключают друг друга, а значит угадать нельзя. Прогоны оставили одну версию.

  * **Вегетарианец снят.** Голая рука прошла его критерий в двух прогонах из
    шести: обычный недельный список без всякой памяти и так бывает
    вегетарианским. Пара мерила базовое поведение модели.
  * **Мясоед снят.** Голая рука сама набирала животную плоть — мясо есть в
    любом списке покупок, — и пара держалась только на запрете растительного
    белка, то есть на том, назовёт ли случайный список фасоль, сою или орехи.
    Разбор прогона: у ответа голой руки `hits=['животная плоть']`,
    `false_hits=['соев', 'фасол']`. Это тот же дефект, что у вегетарианца,
    вывернутый наизнанку. Заодно запрет валил и верные ответы: список с мясом,
    где мимоходом стоит «Орехи или фрукты», мясоеду не запрещён ничем.
  * **Веган остался.** Его запрет — животное целиком, и списка без мяса и без
    молочного разом голая рука не написала ни разу. Ожидание (растительный
    белок) само по себе угадываемо, держит пару именно запрет.

Что вернуло бы мясоеда: ожидание не на всю категорию, а на конкретный продукт,
который память назвала и которого случайный список не называет (`индейк`).
Это меняет решение оператора о категориях и потому здесь не сделано.

Судит пара **категориями**: запрет на `фарш` пропускает индейку и говядину, и
пара зачлась бы пройденной по недосмотру. Категорий три, ось — животное против
растительного, а внутри животного плоть отделена от молочного с яйцами. Словарь
лежит в конверте набора один раз, см. `eval/pairs.py`.

Свойства:

1. Пара есть в наборе, `aim: apply`, место её не выдаёт ответ.
2. Критерий — только категории; отдельных слов у пары нет вовсе.
3. Слова категорий — куски слов: ловят любую падежную форму, не ловятся
   серединой чужого слова и не ловят соседние продукты из тех же ответов.
4. Растительный продукт не читается как животный: «растительное молоко» и
   «молочные заменители» отменяются, обычное молоко — нет.
5. Категории не пересекаются: слово принадлежит одной.
6. Факт достижим поиском по словам задачи: у каждой реплики первой сессии есть
   общее слово с задачей второй. Поиск в базе идёт словами вопроса
   (`storage.db.Repository.search`), и запись без общего слова не находится
   вовсе — прогон покажет «память ничего не нашла», хотя факт лежит в базе.
7. Семь прежних пар на месте, категорий у них нет, id уникальны.

Мутации, на которых проверки обязаны краснеть:
  * критерий вернули к перечню угаданных слов      → TestCriteriaAreClosedKinds
  * слово категории ловится серединой чужого       → TestCriteriaAreClosedKinds
  * в категорию дописали короткую основу «кур»     → TestCriteriaAreClosedKinds
  * отмену растительного сняли                     → TestCriteriaAreClosedKinds
  * место пары называет ответ                      → TestThePairIsInTheSet
  * факт переписали так, что поиск его не достаёт  → TestTheFactIsReachableFromTheTask
  * прежней паре дописали категорию                → TestTheOldSevenAreStillThere
"""
import os
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import query
from eval import evaluate, pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

FAST = settings(deadline=None, max_examples=100)

FLESH = "животная плоть"
DAIRY = "животное неплотское"
PLANT = "растительный белок"
KIND_NAMES = (FLESH, DAIRY, PLANT)

VEGAN = "питание-веган"

# Прежний набор: семь пар, вокруг которых версия добавляется.
OLD_SEVEN = ("завтрак", "город", "забор", "макбук", "овсянка-ужин",
             "макбук-не-в-тему", "кровь")

# Слова из снятых ответов, белком не являющиеся. Ни одно слово категории не
# имеет права поймать ни одно из них: короткая основа «кур» ловит куркуму,
# «греч» — грецкий орех, и животное находится там, где его нет.
COLLIDERS = ("куркума", "кукуруза", "картофель", "морковь", "макароны",
             "макаронные", "капуста", "крупа", "консервированные",
             "замороженные", "специи", "сахар", "соль", "сухофрукты",
             "помидоры", "огурцы", "петрушка", "укроп", "чеснок", "яблоки",
             "апельсины", "бананы", "варенье", "печенье", "хлеб", "чай",
             "кофе", "рис", "гречка", "овсяные", "зелень", "перец", "минут",
             "сельдерей", "киноа")

# Растительное, названное словами животного. Ни одно не имеет права прочитаться
# как животный продукт — на этом веган и провалил свой первый прогон.
PLANT_MADE = ("растительное молоко", "соевое молоко", "овсяное молоко",
              "молочные заменители", "растительное мясо", "веганский сыр",
              "йогурт веган", "тофу в панировке", "овощи в сыром виде")

# Обычное животное: отмена не имеет права снять его.
PLAIN_ANIMAL = (("молоко", DAIRY), ("творог", DAIRY), ("яйца", DAIRY),
                ("сыр", DAIRY), ("курица", FLESH), ("говядина", FLESH),
                ("индейка", FLESH), ("рыба", FLESH))

ENDINGS = ("", "а", "у", "ом", "е", "и", "ами", "ой", "ые")


def load_items():
    return pairs.load(HOUSEHOLD)[1]


class Base(unittest.TestCase):
    """Пара в наборе — предусловие каждой проверки ниже.

    Без него класс, перебирающий версии циклом, зеленел бы на пустом словаре:
    выкини пару из набора — и «у всех aim: apply» проходит, потому что их нет.
    """

    def setUp(self):
        body, items = pairs.load(HOUSEHOLD)
        self.pair = {item["id"]: item for item in items}.get(VEGAN)
        self.assertIsNotNone(self.pair, "пары про питание в наборе нет")
        raw = body.get("kinds") or {}
        self.kinds = {name: pairs.words_of(kind) for name, kind in raw.items()}
        self.unless = {name: tuple(pairs.unless_of(kind))
                       for name, kind in raw.items()}

    def seen(self, name, text):
        """Слова категории, которые в тексте встретились. Тем же судьёй."""
        return [word for word in self.kinds[name]
                if evaluate._in_kind(word, text, self.unless[name])]


class TestThePairIsInTheSet(Base):

    def test_it_asks_the_agent_to_apply(self):
        self.assertEqual("apply", self.pair["aim"])

    def test_the_place_does_not_give_the_answer_away(self):
        """Имя места уезжает в путь каталога хода, а путь агент видит.

        Место «еда-вег» рука без памяти читала как «вегетарианец» и «угадывала»
        — стоило прогона.
        """
        where = self.pair["tell"][0]["place"].lower()
        for kind in (self.pair.get("vocab") or {}).get("expect", {}).values():
            for word in pairs.words_of(kind):
                self.assertNotIn(word, where,
                                 "имя места выдаёт ответ: %r" % where)

    def test_it_says_why_its_fact_matters(self):
        self.assertTrue((self.pair.get("matters") or "").strip(),
                        "не сказано, при каком условии факт значим")


class TestCriteriaAreClosedKinds(Base):
    """Критерий — именованные категории, а не перечень угаданных слов."""

    def test_the_pair_judges_by_kinds_only(self):
        self.assertEqual([], self.pair.get("expect") or [],
                         "пара судит ещё и отдельными словами")
        self.assertEqual([], self.pair.get("forbid") or [],
                         "пара судит ещё и отдельными словами")
        self.assertEqual([PLANT], self.pair["expect_kinds"])
        self.assertEqual(sorted([FLESH, DAIRY]), sorted(self.pair["forbid_kinds"]))

    def test_the_axis_is_animal_against_plant(self):
        self.assertEqual(sorted(KIND_NAMES), sorted(self.kinds))

    def test_the_vocabulary_lives_in_the_envelope_once(self):
        self.assertTrue(self.kinds, "словаря в конверте нет")
        self.assertIn("vocab", self.pair, "словарь не разрешён")

    def test_no_word_of_a_kind_is_a_phrase(self):
        for name, words in self.kinds.items():
            self.assertTrue(words, "категория %r пуста" % name)
            for word in words:
                self.assertNotIn(" ", word, "%s: слово фразой: %r" % (name, word))
                self.assertTrue(word.isalpha(),
                                "%s: в куске слова не только буквы: %r"
                                % (name, word))

    @given(ending=st.sampled_from(ENDINGS))
    @FAST
    def test_every_kind_word_catches_its_own_inflections(self, ending):
        """Кусок ловит своё слово в любом падеже и не ловится серединой чужого."""
        for name, words in self.kinds.items():
            for word in words:
                form = word + ending
                self.assertTrue(
                    evaluate._in_kind(word, "купить %s в магазине" % form),
                    "%s: форма %r не ловится куском %r" % (name, form, word))
                self.assertFalse(
                    evaluate._in_kind(word, "купить за%s в магазине" % form),
                    "%s: кусок %r прочли серединой слова" % (name, word))

    @given(collider=st.sampled_from(COLLIDERS))
    @FAST
    def test_no_kind_word_catches_a_word_that_is_not_protein(self, collider):
        """Короткая основа не имеет права ловить соседний продукт из тех же ответов."""
        for name in self.kinds:
            self.assertEqual([], self.seen(name, "купить %s в магазине" % collider),
                             "%s: поймали %r" % (name, collider))

    @given(said=st.sampled_from(PLANT_MADE))
    @FAST
    def test_a_plant_made_product_is_not_read_as_animal(self, said):
        """«Растительное молоко» — не молоко. На этом веган провалил первый прогон."""
        for name in (FLESH, DAIRY):
            self.assertEqual([], self.seen(name, "купить %s" % said),
                             "%s: %r прочли как животный продукт" % (name, said))

    @given(pick=st.sampled_from(PLAIN_ANIMAL))
    @FAST
    def test_the_plain_animal_word_still_counts(self, pick):
        said, name = pick
        self.assertTrue(self.seen(name, "купить %s" % said),
                        "%s: %r больше не ловится" % (name, said))

    def test_cancelling_does_not_cross_a_list_item(self):
        """Соседняя строка списка — другая покупка, а не уточнение к этой."""
        for text in ("молоко, растительное масло, хлеб",
                     "растительное масло, молоко, хлеб",
                     "- Молоко\n- Растительное масло",
                     "- Растительное масло\n- Молоко"):
            self.assertTrue(self.seen(DAIRY, text),
                            "молоко отменили из соседнего пункта: %r" % text)

    def test_the_kinds_do_not_overlap(self):
        """Слово принадлежит одной категории: иначе ось деления не ось."""
        seen = {}
        for name, words in self.kinds.items():
            for word in words:
                self.assertNotIn(word, seen,
                                 "%r стоит и в %r, и в %r"
                                 % (word, seen.get(word), name))
                seen[word] = name


class TestTheFactIsReachableFromTheTask(Base):
    """У каждой реплики есть общее слово с задачей — иначе поиск её не достанет.

    Стоило прогона: «Я вегетарианец: мяса и рыбы не ем совсем» и «Составь
    список покупок на неделю» не делят ни одного слова, и запись, честно
    лежащая в базе, в выдачу не попала ни разу. Снаружи это выглядело как
    слабая память, а было непроходимой парой.

    Сравниваем той же парой функций, какой сравнивает сама база: слова вопроса
    через `query.words`, содержимое записи через `query.key`.
    """

    def test_every_line_shares_a_word_with_the_task(self):
        asked = set(query.words(self.pair["task"]["say"]))
        self.assertTrue(asked, "в задаче не осталось слов поиска")
        for turn in self.pair["tell"]:
            said = set(query.key(turn["say"]).split())
            self.assertTrue(asked & said,
                            "реплика %r не делит с задачей ни одного слова — "
                            "поиск её не достанет" % turn["say"])


class TestTheOldSevenAreStillThere(unittest.TestCase):
    """Версия добавлена рядом с прежними парами, а не вместо них."""

    def test_every_old_pair_is_still_in_the_set(self):
        present = {item["id"] for item in load_items()}
        missing = [id_ for id_ in OLD_SEVEN if id_ not in present]
        self.assertEqual([], missing, "прежняя пара пропала: %s" % missing)

    def test_the_old_pairs_judge_by_words_as_before(self):
        """Категорий у прежних пар нет: по ним снята история цифр в журнале."""
        for item in load_items():
            if item["id"] == VEGAN:
                continue
            self.assertNotIn("expect_kinds", item, item["id"])
            self.assertNotIn("forbid_kinds", item, item["id"])
            self.assertNotIn("vocab", item, item["id"])

    def test_pair_ids_are_unique(self):
        ids = [item["id"] for item in load_items()]
        self.assertEqual(len(ids), len(set(ids)), "id повторяются: %s" % ids)

    def test_the_envelope_counts_what_the_list_holds(self):
        body, items = pairs.load(HOUSEHOLD)
        self.assertEqual(len(OLD_SEVEN) + 1, body["count"])
        self.assertEqual(body["count"], len(items))


if __name__ == "__main__":
    unittest.main()
