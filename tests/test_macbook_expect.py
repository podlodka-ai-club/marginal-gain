#!/usr/bin/env python3
"""`макбук`: мысль «запасного ноутбука нет» принимается в любой форме.

Запуск: python3 -m pytest tests/test_macbook_expect.py -q

Пара ждала подстроку `единствен`, а рука выражала ту же мысль иначе — «рабочий
ноутбук один», «другого нет», «запасного нет». Все три верны по существу и все
три пара засчитывала промахом: вхождение одной строки на «хотя бы одну из
форм» не отвечает.

Отсюда критерий — **категория** с закрытым словарём основ (`kinds` в конверте
набора, разбор в `eval/pairs.py`): ожидание категории сбывается любым её
словом, а не всеми сразу. Ось категории одна — «второго рабочего ноутбука
нет»: `единствен`, `один` (…ноутбук один), `другого` (…другого нет), `запасн`
(…запасного нет).

Свойства:

1. Пара в наборе, `aim: apply`, судит одной категорией: ни `expect`, ни
   `forbid` у неё нет, место и задача ответа не выдают.
2. Словарь категории — закрытый список основ (не фразы, только буквы), лежит в
   конверте один раз.
3. Любая из форм, которыми рука выражает «запасного нет», рядом с фразой про
   риск проходит судью: ни одной ложной находки, `ok` истинно.
4. Пара по-прежнему кусается: общий ответ про ремонт, не утверждающий
   единственности, судью не проходит — применение факта всё ещё решает исход.
5. Основа категории совпадает с начала слова, ловит падежи и не ловится
   серединой чужого слова.

Мутации, на которых проверки обязаны краснеть:
  * критерий вернули к `expect: ["единствен"]`      → пункт 3 краснеет на всех
                                                      формах, кроме первой
  * из словаря убрали `один`/`другого`/`запасн`     → пункт 3 краснеет
  * ожидание категории потребовало все слова        → пункт 3 краснеет
  * в словарь дописали основу пошире (`ноут`)       → пункт 4 краснеет
  * категорию назвали и в задаче/месте пары          → пункт 1 краснеет
"""
import os
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import evaluate, pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

FAST = settings(deadline=None, max_examples=100)

BODY, ITEMS = pairs.load(HOUSEHOLD)
PAIR = {item["id"]: item for item in ITEMS}["макбук"]
KINDS = BODY.get("kinds") or {}

# Единственная категория, которую пара называет в ожидании.
KIND_NAME = (PAIR.get("expect_kinds") or [None])[0]

# Формы, которыми рука выражает «второго рабочего ноутбука нет». Первая уже
# принималась подстрокой, три следующие — те, на которых пара промахивалась.
NO_SPARE = (
    "это твой единственный рабочий ноутбук",
    "рабочий ноутбук у тебя один",
    "другого рабочего ноутбука у тебя нет",
    "запасного ноутбука нет",
    "запасной машины нет",
)

# Фраза про риск: пара положительная, ответ обязан ещё и признать риск.
RISK = (
    "на неделю останешься без рабочей машины — это серьёзный риск",
    "всю неделю ремонта работать будет не на чём",
    "риск для работы прямой: рабочих задач за эту неделю не сделать",
)

# Общий ответ без факта: риск назван, единственность — нет. Голая рука
# отвечает примерно так, и пара обязана его завалить.
GENERIC = (
    "Да, риск есть: на время ремонта останешься без ноутбука. "
    "Сделай резервную копию и уточни срок ремонта в сервисе.",
    "Риск умеренный. Забэкапь важное в облако, узнай, дают ли подменный "
    "аппарат, и планируй лёгкие задачи на этот период.",
    "Клавиатуру чинят за пару дней. Перед сдачей сними бэкап и выйди из "
    "рабочих аккаунтов, тогда рисков почти нет.",
    "Смотря насколько ты загружен. Если неделю можешь не работать за "
    "компьютером — сдавай спокойно, только сделай копию данных.",
)

ENDINGS = ("", "а", "у", "ом", "е", "и", "ый", "ые", "ого")


class TestThePairIsInTheSet(unittest.TestCase):
    """Пара судит одной категорией, и ни задача, ни место её не выдают."""

    def test_it_asks_the_agent_to_apply(self):
        self.assertEqual("apply", PAIR["aim"])

    def test_it_judges_by_a_single_kind_and_no_bare_words(self):
        self.assertEqual([], PAIR.get("expect") or [],
                         "пара всё ещё судит отдельным словом")
        self.assertEqual([], PAIR.get("forbid") or [],
                         "у положительной пары появился запрет")
        self.assertEqual([KIND_NAME], PAIR.get("expect_kinds"))
        self.assertNotIn("forbid_kinds", PAIR)

    def test_neither_task_nor_place_names_a_kind_word(self):
        blurred = " ".join([
            PAIR["task"].get("say") or "",
            PAIR["task"].get("place") or "",
            " ".join(turn.get("place") or "" for turn in PAIR["tell"]),
        ]).lower()
        for word in pairs.words_of(KINDS[KIND_NAME]):
            self.assertNotIn(word, blurred,
                             "слово категории %r названо в задаче/месте" % word)

    def test_it_says_why_its_fact_matters(self):
        self.assertTrue((PAIR.get("matters") or "").strip())


class TestTheVocabularyIsAClosedListOfStems(unittest.TestCase):
    """Словарь категории лежит в конверте один раз и состоит из основ."""

    def test_the_kind_lives_in_the_envelope(self):
        self.assertIn(KIND_NAME, KINDS, "категории пары нет в конверте")
        self.assertIn("vocab", PAIR, "имя категории не разрешено в слова")

    def test_every_word_is_a_bare_stem(self):
        words = pairs.words_of(KINDS[KIND_NAME])
        self.assertTrue(words, "словарь категории пуст")
        for word in words:
            self.assertNotIn(" ", word, "слово категории фразой: %r" % word)
            self.assertTrue(word.isalpha(), "в основе не только буквы: %r" % word)


class TestEveryFormOfNoSparePasses(unittest.TestCase):
    """Любая форма мысли «запасного нет» рядом с риском проходит судью."""

    @settings(deadline=None, max_examples=200)
    @given(said=st.sampled_from(NO_SPARE), risk=st.sampled_from(RISK))
    def test_a_no_spare_answer_passes(self, said, risk):
        answer = "%s, поэтому %s." % (said.capitalize(), risk)
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertEqual([], verdict["false_hits"],
                         "верный ответ завален: %r" % answer)
        self.assertTrue(verdict["ok"], "форма не принята: %r" % answer)

    def test_the_old_substring_form_still_passes(self):
        answer = ("Это твой единственный рабочий ноутбук, "
                  "на неделю останешься без рабочей машины.")
        self.assertTrue(evaluate.judge(PAIR, answer, answer, None)["ok"])


class TestThePairStillBites(unittest.TestCase):
    """Ответ без утверждения единственности пару не проходит."""

    @settings(deadline=None, max_examples=100)
    @given(generic=st.sampled_from(GENERIC))
    def test_a_generic_repair_answer_does_not_pass(self, generic):
        verdict = evaluate.judge(PAIR, generic, generic, None)
        self.assertFalse(verdict["ok"],
                         "ответ без факта прошёл: %r" % generic)
        self.assertIn(KIND_NAME, verdict["missed"])


class TestAKindWordMatchesFromWordStart(unittest.TestCase):
    """Основа ловит свои падежи и не ловится серединой чужого слова."""

    @given(ending=st.sampled_from(ENDINGS))
    @FAST
    def test_inflections_hit_and_mid_word_does_not(self, ending):
        for word in pairs.words_of(KINDS[KIND_NAME]):
            form = word + ending
            self.assertTrue(
                evaluate._in_kind(word, "потому что ноутбук %s" % form),
                "форма %r не поймана основой %r" % (form, word))
            self.assertFalse(
                evaluate._in_kind(word, "потому что ноутбукза%s" % form),
                "основу %r прочли серединой слова" % word)


if __name__ == "__main__":
    unittest.main()
