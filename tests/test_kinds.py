#!/usr/bin/env python3
"""Категория с закрытым словарём: чем пара судит «было это в ответе или нет».

Запуск: python3 -m pytest tests/test_kinds.py -q

Перечень угаданных слов в `forbid` не отвечает на вопрос «было в ответе мясо».
Запрет `фарш` пропускает «индейку» и «говядину», и пара засчитывается
пройденной по недосмотру. Значит пара называет **категорию**, а словарь
категории лежит в наборе один раз и общий для всех пар, которые её называют.

Форма: у конверта появляется `kinds` — имя категории и закрытый список кусков
слов. У пары появляются `expect_kinds` и `forbid_kinds` — имена категорий,
а не слова. Разрешение имени в слова делает загрузчик набора и только он:
судья видит уже разрешённый словарь в самой паре (`vocab`) и про конверт не
знает, ровно как не знает про него сейчас.

Свойства:

1. Пара, назвавшая неизвестную конверту категорию, не проходит проверку формы:
   опечатка в имени иначе значила бы пустой словарь, то есть запрет, который
   ничего не запрещает.
2. `expect_kinds`/`forbid_kinds`, если есть, — непустые списки строк.
3. Загрузка разрешает имена в слова, запись — снимает разрешённое обратно:
   файл несёт имена, а не копию словаря. Круг «записать и прочесть» ничего не
   теряет и ничего не размножает.
4. Ожидание категории — «хотя бы одно слово из неё», запрет категории —
   «ни одного слова из неё».
4a. Слово категории совпадает с начала слова, а не подстрокой: «нут» — это
   «нута» и «нутом», но не «минут». Основы категории коротки, и голая
   подстрока находила бы мясо там, где его нет.
4b. Категория может назвать **отменяющие** основы (`unless`): совпадение не
   считается, если само слово или его сосед слева или справа начинается с
   отменяющей основы. «Растительное молоко» и «молочные заменители» — не
   животный продукт, хотя основа `молок` в них есть.
5. Пара без категорий судится ровно как раньше: тот же исход, что у пары,
   у которой поля категорий вовсе нет.
6. Пара, судящая одними категориями, — законная: исход у неё определён, и
   признак «нужное было во вбросе» у неё считается, а не молчит.

Мутации, на которых проверки обязаны краснеть:
  * `validate` пропускает неизвестное имя категории  → TestUnknownKindIsRejected
  * загрузчик не разрешает имена в слова             → TestNamesResolveToWords
  * запрет категории судит по одному слову из десяти → TestForbiddenKindCatchesEveryWord
  * ожидание категории требует все слова, а не одно  → TestExpectedKindWantsAnyWord
  * категории протекли в старое правило              → TestPairsWithoutKindsJudgeAsBefore
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import evaluate, pairs

FAST = settings(deadline=None, max_examples=100)

KINDS = {
    "плоть": ["мяс", "куриц", "индейк", "говядин"],
    # Форма с отменяющими основами: словарь и то, что его отменяет, рядом.
    "неплотское": {"words": ["молок", "молочн", "творог", "яйц"],
                   "unless": ["растительн", "соев", "замен"]},
    "растительное": ["тофу", "нут", "чечевиц"],
}
WORDS = {name: pairs.words_of(kind) for name, kind in KINDS.items()}
UNLESS = {name: pairs.unless_of(kind) for name, kind in KINDS.items()}

WORD = st.text(min_size=1, max_size=10).filter(lambda s: s.strip())


def a_pair(**over):
    body = {"id": "проба", "aim": "apply",
            "tell": [{"say": "сказали", "place": "тут"}],
            "task": {"say": "спросили", "place": "тут"},
            "expect": ["слово"], "forbid": []}
    body.update(over)
    return body


def said(*words):
    return "Список: %s." % ", ".join(words)


def vocab_of(name):
    return {"words": WORDS[name], "unless": UNLESS[name]}


class TestUnknownKindIsRejected(unittest.TestCase):
    """Имя категории, которого нет в конверте, останавливает набор."""

    @given(name=WORD.filter(lambda s: s not in KINDS))
    @FAST
    def test_an_expect_kind_outside_the_envelope_is_rejected(self, name):
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(a_pair(expect_kinds=[name]), kinds=KINDS)

    @given(name=WORD.filter(lambda s: s not in KINDS))
    @FAST
    def test_a_forbid_kind_outside_the_envelope_is_rejected(self, name):
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(a_pair(forbid_kinds=[name]), kinds=KINDS)

    @given(name=st.sampled_from(sorted(KINDS)))
    @FAST
    def test_a_named_kind_from_the_envelope_passes(self, name):
        item = a_pair(expect_kinds=[name], forbid_kinds=[name])
        self.assertEqual(item, pairs.validate(dict(item), kinds=KINDS))

    @given(bad=st.sampled_from(([], "плоть", 7, [""], [None])))
    @FAST
    def test_kinds_must_be_a_non_empty_list_of_names(self, bad):
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(a_pair(expect_kinds=bad), kinds=KINDS)


class TestNamesResolveToWords(unittest.TestCase):
    """Файл несёт имена, судья — слова. Разрешает имена загрузчик."""

    def round_trip(self, items):
        with tempfile.TemporaryDirectory() as tmp:
            where = Path(tmp) / "набор.json"
            pairs.dump(where, items, kinds=KINDS)
            raw = json.loads(where.read_text(encoding="utf-8"))
            body, back = pairs.load(where)
        return raw, body, back

    def test_loading_puts_the_closed_vocabulary_into_the_pair(self):
        item = a_pair(expect_kinds=["неплотское"], forbid_kinds=["плоть"])
        _, _, back = self.round_trip([item])
        self.assertEqual({"неплотское": vocab_of("неплотское")},
                         back[0]["vocab"]["expect"])
        self.assertEqual({"плоть": {"words": WORDS["плоть"], "unless": []}},
                         back[0]["vocab"]["forbid"])

    def test_writing_keeps_names_and_not_a_copy_of_the_vocabulary(self):
        item = a_pair(expect_kinds=["неплотское"])
        raw, _, _ = self.round_trip([item])
        self.assertEqual(["неплотское"], raw["items"][0]["expect_kinds"])
        self.assertNotIn("vocab", raw["items"][0],
                         "словарь размножился по парам — правка одной копии "
                         "разойдётся с остальными молча")
        self.assertEqual(KINDS, raw["kinds"])

    def test_a_pair_naming_no_kind_gets_no_vocabulary(self):
        _, _, back = self.round_trip([a_pair()])
        self.assertNotIn("vocab", back[0])

    def test_writing_a_loaded_set_again_changes_nothing(self):
        """Круг «прочесть и записать» не размножает разрешённое в файл."""
        item = a_pair(expect_kinds=["неплотское"], forbid_kinds=["плоть"])
        raw, body, back = self.round_trip([item])
        again, _, _ = self.round_trip(back)
        self.assertEqual(raw, again)

    def test_writing_a_loaded_set_without_naming_kinds_still_loads(self):
        """Записать прочитанное, не называя словарь заново, — законно.

        Иначе получается файл, где пары называют категории, которых в конверте
        нет, и следующий `load` его отвергает: круг чтения и записи разрывался
        бы молча, а замечает это только тот, кто в этот файл заглянет.

        Побайтового равенства двух файлов здесь не требуется и быть не может:
        словарь пересобирается из разрешённого, то есть приходит в одной форме
        (`words`/`unless`) и без категорий, которых не назвала ни одна пара.
        Требуется, чтобы пары судились тем же самым.
        """
        item = a_pair(expect_kinds=["неплотское"], forbid_kinds=["плоть"])
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "первый.json"
            second = Path(tmp) / "второй.json"
            pairs.dump(first, [item], kinds=KINDS)
            was = pairs.load(first)[1]
            pairs.dump(second, was)
            again = pairs.load(second)[1]
            self.assertEqual([p[pairs.VOCAB] for p in was],
                             [p[pairs.VOCAB] for p in again])


class TestForbiddenKindCatchesEveryWord(unittest.TestCase):
    """Запрет категории ловит любое слово из неё, а не то, что вспомнили."""

    @given(word=st.sampled_from(WORDS["плоть"]),
           tail=st.sampled_from(("", "а", "ой", "ые", "у")))
    @FAST
    def test_any_word_of_a_forbidden_kind_fails_the_answer(self, word, tail):
        item = a_pair(expect=[], forbid=[], forbid_kinds=["плоть"],
                      vocab={"expect": {}, "forbid": {"плоть": vocab_of("плоть")}})
        verdict = evaluate.judge(item, said("хлеб", word + tail), "", None)
        self.assertFalse(verdict["ok"])
        self.assertIn(word, verdict["false_hits"])

    @given(words=st.lists(st.sampled_from(WORDS["растительное"]),
                          min_size=1, max_size=3))
    @FAST
    def test_words_outside_the_kind_do_not_trip_it(self, words):
        item = a_pair(expect=[], forbid=[], forbid_kinds=["плоть"],
                      vocab={"expect": {}, "forbid": {"плоть": vocab_of("плоть")}})
        verdict = evaluate.judge(item, said("хлеб", *words), "", None)
        self.assertTrue(verdict["ok"])
        self.assertEqual([], verdict["false_hits"])


class TestExpectedKindWantsAnyWord(unittest.TestCase):
    """Ожидание категории — хотя бы одно её слово, а не все сразу."""

    def item(self):
        return a_pair(expect=[], forbid=[], expect_kinds=["растительное"],
                      vocab={"expect": {"растительное": vocab_of("растительное")},
                             "forbid": {}})

    @given(word=st.sampled_from(WORDS["растительное"]))
    @FAST
    def test_one_word_of_the_kind_is_enough(self, word):
        verdict = evaluate.judge(self.item(), said("хлеб", word), "", None)
        self.assertTrue(verdict["ok"], "%s не хватило" % word)

    @given(words=st.lists(st.sampled_from(WORDS["плоть"]), max_size=3))
    @FAST
    def test_none_of_the_kind_is_not_enough(self, words):
        verdict = evaluate.judge(self.item(), said("хлеб", *words), "", None)
        self.assertFalse(verdict["ok"])
        self.assertIn("растительное", verdict["missed"])


class TestAKindWordMatchesFromTheWordStart(unittest.TestCase):
    """Основа категории — начало слова, а не любое место в нём."""

    def item(self, kind):
        return a_pair(expect=[], forbid=[], forbid_kinds=[kind],
                      vocab={"expect": {}, "forbid": {kind: KINDS[kind]}})

    @given(word=st.sampled_from(WORDS["плоть"]),
           head=st.text(alphabet="абвгдежзи", min_size=1, max_size=5))
    @FAST
    def test_a_kind_word_inside_another_word_does_not_trip(self, word, head):
        verdict = evaluate.judge(self.item("плоть"),
                                 said("хлеб", head + word), "", None)
        self.assertTrue(verdict["ok"],
                        "%r прочли как %r" % (head + word, word))

    @given(word=st.sampled_from(WORDS["плоть"]),
           tail=st.sampled_from(("", "а", "ой", "ые", "у")))
    @FAST
    def test_the_same_word_at_a_word_start_does_trip(self, word, tail):
        verdict = evaluate.judge(self.item("плоть"),
                                 said("хлеб", word + tail), "", None)
        self.assertFalse(verdict["ok"])


class TestAPairMayJudgeByKindsAlone(unittest.TestCase):
    """Категорий достаточно: ни отдельных слов, ни пустого исхода.

    Пара, у которой весь критерий — категории, обязана проходить проверку формы
    и обязана считать признак «ожидаемое было во вбросе». Читайся она как «ни
    expect, ни forbid», набор бы её не принял вовсе; молчи признак — прогон не
    отличил бы «дали не то» от «отдала, не применил».
    """

    def test_kinds_alone_define_an_outcome(self):
        item = a_pair(expect=[], forbid=[], expect_kinds=["растительное"],
                      forbid_kinds=["плоть"])
        self.assertEqual(item, pairs.validate(dict(item), kinds=KINDS))

    def test_a_pair_with_nothing_at_all_is_still_rejected(self):
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(a_pair(expect=[], forbid=[]), kinds=KINDS)

    @given(word=st.sampled_from(WORDS["растительное"]))
    @FAST
    def test_the_feed_is_measured_by_kinds_too(self, word):
        item = a_pair(expect=[], forbid=[], expect_kinds=["растительное"],
                      vocab={"expect": {"растительное": vocab_of("растительное")},
                             "forbid": {}})
        fed = evaluate.judge(item, said("хлеб"), said(word), None)
        self.assertTrue(fed["found_in_answer"], "категорию во вбросе не увидели")
        empty = evaluate.judge(item, said("хлеб"), said("хлеб"), None)
        self.assertFalse(empty["found_in_answer"])


class TestAKindCanCancelItself(unittest.TestCase):
    """Отменяющая основа снимает совпадение — и только рядом с собой.

    «Растительное молоко» и «молочные заменители» животным продуктом не
    являются, хотя основа `молок` в них есть. Отменяет либо само слово, либо
    его сосед слева или справа: дальше соседа отмена не действует, иначе одно
    слово «растительное» в начале списка сняло бы весь запрет разом.
    """

    def item(self):
        return a_pair(expect=[], forbid=[], forbid_kinds=["неплотское"],
                      vocab={"expect": {}, "forbid": {"неплотское": vocab_of("неплотское")}})

    @given(said_as=st.sampled_from(("растительное молоко",
                                    "растительным молоком",
                                    "соевое молоко",
                                    "молочные заменители")))
    @FAST
    def test_a_cancelled_match_does_not_count(self, said_as):
        verdict = evaluate.judge(self.item(), "Список: хлеб, %s." % said_as,
                                 "", None)
        self.assertTrue(verdict["ok"],
                        "%r прочли как животный продукт: %s"
                        % (said_as, verdict["false_hits"]))

    @given(said_as=st.sampled_from(("молоко", "молоком", "творог", "яйца",
                                    "молочные продукты")))
    @FAST
    def test_the_plain_word_still_counts(self, said_as):
        verdict = evaluate.judge(self.item(), "Список: хлеб, %s." % said_as,
                                 "", None)
        self.assertFalse(verdict["ok"], "%r не поймали" % said_as)

    @given(order=st.sampled_from((
            "молоко, растительное масло, хлеб",
            "растительное масло, молоко, хлеб",
            "- Молоко\n- Растительное масло",
            "- Растительное масло\n- Молоко")))
    @FAST
    def test_cancelling_does_not_cross_a_punctuation_mark(self, order):
        """Соседняя строка списка — другая покупка, а не уточнение к этой.

        Считай мы соседство по плоскому потоку слов — «растительное» из
        следующего пункта отменяло бы настоящее коровье молоко в предыдущем, и
        исход пары зависел бы от порядка строк в ответе.
        """
        verdict = evaluate.judge(self.item(), "Список: %s." % order, "", None)
        self.assertFalse(verdict["ok"],
                         "молоко отменили из соседнего пункта: %r" % order)

    def test_cancelling_reaches_one_word_and_no_further(self):
        """Отмена — про соседа, а не про весь список."""
        verdict = evaluate.judge(
            self.item(), "Список: растительное масло, хлеб, творог, яйца.",
            "", None)
        self.assertFalse(verdict["ok"],
                         "отмена через два слова сняла запрет целиком")


class TestPairsWithoutKindsJudgeAsBefore(unittest.TestCase):
    """Старое правило не сдвинулось: пара без категорий судится как судилась."""

    @given(expect=st.lists(st.sampled_from(("овсян", "казан")), max_size=2),
           forbid=st.lists(st.sampled_from(("забор", "краск")), max_size=2),
           answer=st.text(alphabet="овсянказбркi ", max_size=30))
    @FAST
    def test_the_verdict_does_not_depend_on_the_new_fields_being_absent(
            self, expect, forbid, answer):
        plain = a_pair(expect=expect, forbid=forbid)
        empty = copy.deepcopy(plain)
        empty.update(expect_kinds=[], forbid_kinds=[],
                     vocab={"expect": {}, "forbid": {}})
        self.assertEqual(evaluate.judge(plain, answer, "", None),
                         evaluate.judge(empty, answer, "", None))


if __name__ == "__main__":
    unittest.main()
