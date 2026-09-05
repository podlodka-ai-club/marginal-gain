#!/usr/bin/env python3
"""Питание: одна задача, разные версии памяти, несовместимые ответы.

Запуск: python3 -m pytest tests/test_diet_triple.py -q

Обычная пара доказывает влияние факта разницей двух рук: с памятью и без.
Слабое место у неё одно — угадывание. Список покупок можно написать разумно, ни
о чём не помня, и он сойдётся с ожиданием случайно (так и вышло на первом живом
прогоне с овсянкой, см. `COINCIDED` в `eval/live.py`).

Здесь это сужено. Задача у пар дословно одна, а память разная: веган и мясоед.
Ответы версий делят друг друга: список, верный для одной, для другой неверен, и
**один ответ не закрывает обе версии**. Значит попасть надо дважды, каждый раз
в свою.

Версий заводилось три; вегетарианец снят. Обычный недельный список без всякой
памяти оказывался вегетарианским в двух прогонах из шести — то есть пара
проходила угадыванием у трети голых рук и мерила не память, а базовое поведение
модели. Веган и мясоед жёстче: тофу и фарш случайно в одном списке не выпадают,
и голая рука не прошла ни одну из них ни разу.

Чего этот набор не обещает: что случайный список не пройдёт ни одной версии.
Критерий мясоеда — «животная плоть есть, растительного белка нет», и обычный
мясной список ему удовлетворяет сам собой. Это меряется прогоном голой руки, а
не выводится из формы набора.

Судят три пары **категориями**, а не перечнем угаданных слов: запрет на `фарш`
пропускает индейку и говядину, и пара зачлась бы пройденной по недосмотру.
Категорий три, ось деления — животное против растительного, а внутри животного
плоть отделена от молочного с яйцами: ровно на этой границе и расходятся веган
с вегетарианцем. Словарь лежит в конверте набора один раз, см. `eval/pairs.py`.

Свойства:

1. Обе пары есть в наборе, у всех `aim: apply`, задача у всех дословно одна,
   а реплики первой сессии и место у каждой свои.
2. Ответ, собранный из меню одной версии, проходит её критерий и проваливает
   критерий соседней — в обе стороны.
3. Ни один ответ, какой список ни собери, не закрывает две версии сразу.
4. Критерий — категории: отдельных слов у пар нет вовсе, слова категорий
   куски слов, ловят любую падежную форму и не ловятся серединой чужого слова,
   а категории не пересекаются.
5. Факт достижим поиском по словам задачи: у каждой реплики первой сессии есть
   общее слово с задачей второй. Поиск в базе идёт словами вопроса
   (`storage.db.Repository.search`), и запись без общего слова не находится
   вовсе — прогон покажет «память ничего не нашла», хотя факт лежит в базе.
6. Семь прежних пар на месте, id уникальны, конверт считает столько же,
   сколько в списке.

Мутации, на которых проверки обязаны краснеть:
  * задачу одной из трёх переписали              → TestTripleIsInTheSet
  * категорию у пары подменили или сняли          → TestVersionsAreMutuallyExclusive
  * границы категорий размыли так, что один ответ → TestNoAnswerPassesTwoVersions
    проходит две версии
  * критерий вернули к перечню угаданных слов     → TestCriteriaAreClosedKinds
  * слово категории ловится серединой чужого      → TestCriteriaAreClosedKinds
  * факт переписали так, что поиск его не достаёт → TestTheFactIsReachableFromTheTask
  * прежнюю пару выкинули или переименовали       → TestTheOldSevenAreStillThere
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
CARNIVORE = "питание-мясоед"
VERSIONS = (VEGAN, CARNIVORE)

# Прежний набор: семь пар, вокруг которых версии добавляются.
OLD_SEVEN = ("завтрак", "город", "забор", "макбук", "овсянка-ужин",
             "макбук-не-в-тему", "кровь")

# Меню версии: из чего человек этой версии составил бы список покупок. Здесь,
# а не в наборе: набор несёт критерий, меню — то, чем критерий проверяется.
# Первым в каждом меню стоит то, чем версия себя и показывает.
MENU = {
    VEGAN: ["тофу", "нут", "чечевица", "хлеб", "овощи", "гречка",
            "овсяное питьё"],
    CARNIVORE: ["фарш", "индейка", "говядина", "курица", "хлеб", "овощи",
                "гречка", "картофель"],
}

# Продукты без белка: общая часть любого списка, исход сама по себе не решает.
PLAIN = ["хлеб", "овощи", "гречка", "картофель", "яблоки", "чай", "соль",
         "макароны", "рис", "сахар"]

# Слова из тех же снятых ответов, белком не являющиеся. Ни одно слово
# категории не имеет права поймать ни одно из них: короткая основа «кур» ловит
# куркуму, «греч» — грецкий орех, и мясо находится там, где его нет.
COLLIDERS = ("куркума", "кукуруза", "картофель", "морковь", "макароны",
             "макаронные", "капуста", "крупа", "консервированные",
             "замороженные", "специи", "сахар", "соль", "сухофрукты",
             "помидоры", "огурцы", "петрушка", "укроп", "чеснок", "яблоки",
             "апельсины", "бананы", "варенье", "печенье", "хлеб", "чай",
             "кофе", "рис", "гречка", "овсяные", "зелень", "перец", "минут")

# Падежные хвосты: кусок слова обязан ловить своё слово в любой форме.
ENDINGS = ("", "а", "у", "ом", "е", "и", "ами", "ой", "ые")


def versions_of(items):
    return {item["id"]: item for item in items if item["id"] in VERSIONS}


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
    выкини версии из набора — и «у всех aim: apply» проходит, потому что их
    нет вовсе.
    """

    def setUp(self):
        self.triple = versions_of(load_items())
        self.assertEqual(sorted(VERSIONS), sorted(self.triple),
                         "версии неполны, проверять нечего: %s"
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
        for one in VERSIONS:
            for other in VERSIONS:
                if one == other:
                    continue
                self.assertEqual(set(), lines[one] & lines[other],
                                 "%s и %s говорят одно и то же" % (one, other))

    def test_every_version_lives_in_its_own_place(self):
        """Общее место склеило бы три противоречащих факта в одной выдаче.

        Имя места ещё и уезжает в путь каталога хода, а путь агент видит: место
        «еда-вег» рука без памяти читала как «вегетарианец» и «угадывала».
        """
        places = [pair["tell"][0]["place"] for pair in self.triple.values()]
        self.assertEqual(len(places), len(set(places)),
                         "версии стоят в одном месте: %s" % places)
        for id_, pair in self.triple.items():
            where = pair["tell"][0]["place"].lower()
            for kind, words in (pair.get("vocab") or {}).get("expect", {}).items():
                for word in words:
                    self.assertNotIn(word, where,
                                     "%s: имя места выдаёт ответ: %r"
                                     % (id_, where))

    def test_every_version_says_why_its_fact_matters(self):
        for id_, pair in self.triple.items():
            self.assertTrue((pair.get("matters") or "").strip(),
                            "%s: не сказано, при каком условии факт значим" % id_)


class TestVersionsAreMutuallyExclusive(Base):
    """Ответ одной версии проваливает критерии двух других. Все шесть сторон."""

    @given(data=st.data())
    @FAST
    def test_an_answer_from_one_menu_passes_only_its_own_version(self, data):
        mine = data.draw(st.sampled_from(VERSIONS))
        pair = self.triple[mine]
        answer = data.draw(a_list_from(MENU[mine], must=MENU[mine][:1]))
        self.assertTrue(passes(pair, answer),
                        "%s не принял свой же ответ: %s" % (mine, answer))
        for other in VERSIONS:
            if other == mine:
                continue
            self.assertFalse(
                passes(self.triple[other], answer),
                "ответ версии %s прошёл критерий версии %s: %s"
                % (mine, other, answer))


class TestNoAnswerPassesTwoVersions(Base):
    """Один ответ не закрывает две версии — какой список ни собери.

    Это и есть «угадать нельзя» в проверяемом виде. Сильного утверждения
    «любой список без памяти не проходит ни одной версии» здесь нет и быть не
    может: критерий мясоеда — «плоть есть, растительного белка нет», и обычный
    мясной список ему удовлетворяет. Что держится — версии делят ответы между
    собой, и один ответ на все три не годится.

    Слова берём из всех трёх меню и общей части разом, любыми смесями: правило
    обязано держаться на смесях, а не только на трёх аккуратных списках.
    """

    @given(answer=a_list_from(sum(MENU.values(), []) + PLAIN))
    @FAST
    def test_at_most_one_version_accepts_an_answer(self, answer):
        took = [id_ for id_, pair in self.triple.items() if passes(pair, answer)]
        self.assertLessEqual(len(took), 1,
                             "один ответ закрыл версии %s: %s" % (took, answer))


class TestCriteriaAreClosedKinds(Base):
    """Критерий версий — именованные категории, а не перечень угаданных слов."""

    def setUp(self):
        super().setUp()
        raw = pairs.load(HOUSEHOLD)[0].get("kinds") or {}
        self.kinds = {name: pairs.words_of(kind) for name, kind in raw.items()}
        self.unless = {name: pairs.unless_of(kind) for name, kind in raw.items()}

    def test_the_three_pairs_judge_by_kinds_only(self):
        for id_, pair in self.triple.items():
            self.assertEqual([], pair.get("expect") or [],
                             "%s судит ещё и отдельными словами" % id_)
            self.assertEqual([], pair.get("forbid") or [],
                             "%s судит ещё и отдельными словами" % id_)
            self.assertTrue(pair.get("expect_kinds"), "%s ничего не ждёт" % id_)
            self.assertTrue(pair.get("forbid_kinds"),
                            "%s ничего не запрещает" % id_)

    def test_the_axis_is_animal_against_plant(self):
        """Три категории и разделение пар по ним — как решено оператором."""
        self.assertEqual(sorted(KIND_NAMES), sorted(self.kinds))
        want = {
            VEGAN: ([PLANT], sorted([FLESH, DAIRY])),
            CARNIVORE: ([FLESH], [PLANT]),
        }
        for id_, (expect, forbid) in want.items():
            pair = self.triple[id_]
            self.assertEqual(expect, sorted(pair["expect_kinds"]), id_)
            self.assertEqual(forbid, sorted(pair["forbid_kinds"]), id_)

    def test_the_vocabulary_lives_in_the_envelope_once(self):
        self.assertTrue(self.kinds, "словаря в конверте нет")
        for id_, pair in self.triple.items():
            self.assertIn("vocab", pair, "%s: словарь не разрешён" % id_)

    def test_no_word_of_a_kind_is_a_phrase(self):
        for name, words in self.kinds.items():
            self.assertTrue(words, "категория %r пуста" % name)
            for word in words:
                self.assertNotIn(" ", word,
                                 "%s: слово фразой: %r" % (name, word))
                self.assertTrue(word.isalpha(),
                                "%s: в куске слова не только буквы: %r"
                                % (name, word))

    @given(ending=st.sampled_from(ENDINGS))
    @FAST
    def test_every_kind_word_catches_its_own_inflections(self, ending):
        """Кусок ловит своё слово в любом падеже и не ловится серединой чужого.

        Вторая половина важнее первой: основы категорий коротки, и «нут» в
        «минут» — это мясо там, где его нет.
        """
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
        """Короткая основа не имеет права ловить соседнее слово из того же списка.

        Список взят из тех же снятых ответов: это продукты, которые модель
        реально писала и которые белком не являются. Дописать в категорию
        основу «кур» — значит найти мясо в куркуме, и никакая проверка формы
        этого не увидит.
        """
        for name, words in self.kinds.items():
            for word in words:
                self.assertFalse(
                    evaluate._in_kind(word, "купить %s в магазине" % collider,
                                      tuple(self.unless[name])),
                    "%s: кусок %r поймал %r" % (name, word, collider))

    def test_a_plant_made_product_is_not_read_as_animal(self):
        """«Растительное молоко» и «мясные заменители» — не животный продукт.

        Основа слова этого не различает, поэтому у животных категорий стоят
        отменяющие основы. Без них веган проваливал свой критерий на верном
        ответе — так и вышло на первом прогоне.
        """
        for said in ("растительное молоко", "соевое молоко",
                     "молочные заменители", "растительное мясо",
                     "веганский сыр", "панировочные сухари"):
            for name in ("животная плоть", "животное неплотское"):
                got = [w for w in self.kinds[name]
                       if evaluate._in_kind(w, "купить %s" % said,
                                            tuple(self.unless[name]))]
                self.assertEqual([], got,
                                 "%s: %r прочли как животный продукт (%s)"
                                 % (name, said, got))

    def test_the_plain_animal_word_still_counts(self):
        """Отмена не отменяет обычное: молоко, творог и курица на месте."""
        for said, name in (("молоко", "животное неплотское"),
                           ("творог", "животное неплотское"),
                           ("яйца", "животное неплотское"),
                           ("курица", "животная плоть"),
                           ("говядина", "животная плоть")):
            got = [w for w in self.kinds[name]
                   if evaluate._in_kind(w, "купить %s" % said,
                                        tuple(self.unless[name]))]
            self.assertTrue(got, "%s: %r больше не ловится" % (name, said))

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
    через `query.words`, содержимое записи через `query.key`. Своя копия
    правила разъехалась бы с поиском молча.
    """

    def test_every_line_shares_a_word_with_the_task(self):
        for id_, pair in self.triple.items():
            asked = set(query.words(pair["task"]["say"]))
            self.assertTrue(asked, "%s: в задаче не осталось слов поиска" % id_)
            for turn in pair["tell"]:
                said = set(query.key(turn["say"]).split())
                self.assertTrue(
                    asked & said,
                    "%s: реплика %r не делит с задачей ни одного слова — "
                    "поиск её не достанет" % (id_, turn["say"]))


class TestTheOldSevenAreStillThere(unittest.TestCase):
    """Тройка добавлена рядом с прежними парами, а не вместо них."""

    def test_every_old_pair_is_still_in_the_set(self):
        present = {item["id"] for item in load_items()}
        missing = [id_ for id_ in OLD_SEVEN if id_ not in present]
        self.assertEqual([], missing, "прежняя пара пропала: %s" % missing)

    def test_the_old_pairs_judge_by_words_as_before(self):
        """Категорий у прежних пар нет: по ним снята история цифр в журнале."""
        for item in load_items():
            if item["id"] in VERSIONS:
                continue
            self.assertNotIn("expect_kinds", item, item["id"])
            self.assertNotIn("forbid_kinds", item, item["id"])
            self.assertNotIn("vocab", item, item["id"])

    def test_pair_ids_are_unique(self):
        ids = [item["id"] for item in load_items()]
        self.assertEqual(len(ids), len(set(ids)), "id повторяются: %s" % ids)

    def test_the_envelope_counts_what_the_list_holds(self):
        body, items = pairs.load(HOUSEHOLD)
        self.assertEqual(len(OLD_SEVEN) + len(VERSIONS), body["count"])
        self.assertEqual(body["count"], len(items))


if __name__ == "__main__":
    unittest.main()
