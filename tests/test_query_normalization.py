#!/usr/bin/env python3
"""Правило разметки слова: регистр, знак на краю, основа. Одно на всех.

Запуск: python3 -m unittest tests.test_query_normalization -v

Факты легли в базу на русском, а вопрос на русском их не доставал. Три ловушки,
все три в том, как слово сравнивается со словом.

1. Регистр. Встроенный в SQLite `lower()` знает одну латиницу: `Казань` он
   оставляет как есть. Сводить приходится на стороне Python. Сюда же `ё`:
   человек пишет его как придётся.
2. Знак на краю слова. Правило слова пускает точку внутрь (`db.py` — одно
   слово), а заодно втягивало точку в конце предложения: «…есть дома.» искало
   `дома.` и не находило `дома`.
3. Словоформа. «завтраки» не находило «завтрак».

Здесь проверяется само правило; что по нему находит поиск — в
`tests/test_search_normalization.py`.

Свойствами, а не примерами: важно не «на этих трёх словах стало лучше», а
«регистр не влияет никогда», «знак на краю не влияет никогда», «формы одного
слова сходятся в одну основу» и «имя файла проходит насквозь».
"""
import os, unittest

from hypothesis import assume, given, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import query

# Буквы русского слова.
RU = "абвгдежзийклмнопрстуфхцчшщыэюя"

# Слово вопроса не короче трёх букв (`domain.query.WORD`), поэтому и генератор
# начинает с трёх: короче — не слово, и проверять на нём нечего.
ru_word = st.text(alphabet=RU, min_size=3, max_size=14)

# Живые парадигмы: слово и его формы. Собирать форму как «основа плюс
# окончание» больше нечем — своего списка окончаний в продукте нет, — да и
# незачем: проверять надо то, что человек напишет в вопросе.
#
# Существительные и прилагательные, без глаголов. Русский Snowball не сводит
# «жила» и «живут» к одному, и требовать этого от него значит требовать
# морфологии, которой у него нет. Факт же говорит о вещах, а не о действиях:
# завтрак, забор, город.
PARADIGMS = {
    "завтрак": "завтрак завтрака завтраку завтраком завтраке завтраки завтраков",
    "забор": "забор забора забору забором заборе заборы заборов заборам",
    "дом": "дом дома дому домом доме домов домам",
    "казань": "казань казани казанью",
    "город": "город города городу городом городе городов городам",
    "машина": "машина машины машине машину машиной машин машинам",
    "неделя": "неделя недели неделе неделю неделей недель неделям",
    "чёрный": "чёрный черный чёрная чёрные чёрного чёрным",
}

paradigm = st.sampled_from(sorted(PARADIGMS))

# Знаки, которые правило слова пускает внутрь: они же липнут к краю в тексте.
EDGE = "./:-"
edge = st.text(alphabet=EDGE, min_size=1, max_size=3)



class TestCaseNeverMatters(unittest.TestCase):
    """Свойство: регистр не меняет ключ поиска. Ни в вопросе, ни в факте."""

    @given(text=st.text(alphabet=RU + RU.upper() + "abcZ ./:-", max_size=60))
    def test_the_key_is_the_same_whatever_the_case(self, text):
        self.assertEqual(query.key(text), query.key(text.upper()))
        self.assertEqual(query.key(text), query.key(text.lower()))

    @given(text=st.text(alphabet=RU + "ёЁ" + RU.upper(), max_size=40))
    def test_yo_never_changes_the_key(self, text):
        self.assertEqual(query.key(text), query.key(text.replace("ё", "е")))

    @given(word=ru_word)
    def test_question_words_are_the_same_whatever_the_case(self, word):
        assume(query.words(word))
        self.assertEqual(query.words(word), query.words(word.upper()))


class TestPunctuationOnTheEdgeNeverMatters(unittest.TestCase):
    """Свойство: знак на краю слова не меняет ни ключ, ни выдачу."""

    @given(word=ru_word, tail=edge)
    def test_an_edge_sign_does_not_change_the_words(self, word, tail):
        assume(query.words(word))
        self.assertEqual(query.words(word + tail), query.words(word))

    @given(word=ru_word, head=edge, tail=edge)
    def test_signs_on_both_edges_do_not_change_the_words(self, word, head, tail):
        assume(query.words(word))
        self.assertEqual(query.words(head + word + tail), query.words(word))

    @given(name=st.sampled_from(["db.py", "on_prompt.py", "localhost:5008",
                                 "job-hunt", "eval/live.py", "3.11"]))
    def test_a_sign_inside_a_name_is_kept(self, name):
        """Знак внутри слова — буква. Обрезка краёв не смеет резать имя."""
        self.assertEqual(query.words(name), [name.lower()])


class TestWordFormsMeetInOneStem(unittest.TestCase):
    """Свойство: формы одного слова сводятся к одной основе."""

    @given(word=ru_word)
    def test_a_stem_is_a_beginning_of_its_word(self, word):
        self.assertTrue(word.startswith(query.stem(word)),
                        "основа %r не начало слова %r" % (query.stem(word), word))

    @given(text=st.text(alphabet=RU + "abc ./:-", max_size=60))
    def test_a_stem_is_never_shorter_than_the_floor(self, text):
        """Обрывок в две буквы совпадает почти с любым русским текстом.

        Snowball сводит «они» к «он», «эти» к «эт», и пропусти мы такой
        обрывок в слова вопроса — поиск вернул бы полбазы. Порог держит вторую
        сторону починки: сведение к основе расширяет выдачу, и расширять её
        бесконечно нельзя.
        """
        for word in query.key(text).split() + query.words(text):
            self.assertGreaterEqual(len(word), query.FLOOR,
                                    "обрывок %r прошёл порог слова" % word)

    def test_a_pronoun_does_not_drag_the_whole_base(self):
        """Тот же порог примером: «они» и «эти» словами вопроса не становятся."""
        self.assertEqual(query.words("они эти мои"), [])

    @given(lemma=paradigm, first=st.integers(), second=st.integers())
    def test_all_forms_of_one_word_share_one_stem(self, lemma, first, second):
        """Все формы слова сходятся в одну основу. Это и есть весь смысл."""
        forms = PARADIGMS[lemma].split()
        one, two = forms[first % len(forms)], forms[second % len(forms)]
        self.assertEqual(query.stem(one), query.stem(two),
                         "формы %r и %r разошлись" % (one, two))


class TestNamesAreLeftAlone(unittest.TestCase):
    """Сведение к основе — про русское слово. Имена оно не трогает."""

    @given(name=st.sampled_from(["db.py", "on_prompt.py", "localhost:5008",
                                 "job-hunt", "eval/live.py", "README",
                                 "polnyj-progon", "memory-encoder"]))
    def test_a_name_survives_stemming_whole(self, name):
        self.assertEqual(query.stem(name.lower()), name.lower())

    @given(word=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3,
                        max_size=12))
    def test_a_latin_word_survives_stemming_whole(self, word):
        self.assertEqual(query.stem(word), word)


if __name__ == "__main__":
    unittest.main()
