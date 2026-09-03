#!/usr/bin/env python3
"""Русский вопрос находит русский факт: поиск не спотыкается о слово.

Запуск: python3 -m unittest tests.test_search_normalization -v

Факты легли в базу на русском, но второй этап их не доставал: «завтрак» и
«забор» давали `кандидатов: 0`. Три ловушки — регистр, знак на конце слова,
словоформа — сидели в том, что поиск сравнивал поле со словом как есть.

Само правило разметки слова проверяется в `tests/test_query_normalization.py`.
Здесь — что по нему находит поиск и что не выбрасывает отсев: правило одно, и
обе стороны сравнения обязаны быть размечены им, а не каждая своим способом.

Свойствами, а не примерами: важно не «на этих трёх словах стало лучше», а
«форма вопроса не меняет находку никогда» и — отдельной парой свойств — «чужое
слово по-прежнему не находится». Последнее держит вторую сторону: сведение к
основе расширяет выдачу, и без него починка меняла бы обрыв на ложные находки.
"""
import os, tempfile, unittest
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models, query
from pipeline import suggest
from storage import db

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

# Буквы русского слова. Отдельно от латиницы: сведение к основе касается только
# русского, имена файлов и веток трогать нельзя.
RU = "абвгдежзийклмнопрстуфхцчшщыэюя"

# Слово вопроса не короче трёх букв (`domain.query.WORD`), поэтому и генератор
# начинает с трёх: короче — не слово, и проверять на нём нечего.
ru_word = st.text(alphabet=RU, min_size=3, max_size=14)

# Слово, у которого есть за что зацепиться: от четырёх букв. Короче — не тема
# факта, а шум, и свойства на нём ничего не сказали бы.
base = ru_word.filter(lambda w: len(w) >= 4)

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


def repo_with(tmp, subject, content):
    """База с одним фактом. Больше для этих свойств не нужно."""
    repo = db.Repository(Path(tmp) / "memory.db")
    repo.apply([models.Fact(fact_type="preference", subject=subject,
                            scope="global", content=content).mutation()])
    return repo


def finds(subject, content, question):
    """Находит ли поиск этот факт по этому вопросу."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = repo_with(tmp, subject, content)
        try:
            return [row for row in repo.search(question)
                    if row.get("object_type") == "Fact"]
        finally:
            repo.close()


class TestTheThreeTrapsOfTheLiveRun(unittest.TestCase):
    """Ровно те три случая, на которых прогон встал. Дословно из пар."""

    def test_a_russian_question_does_not_trip_over_case(self):
        found = finds("город", "Человек живёт в Казани", "в Казани")
        self.assertTrue(found, "заглавная русская буква не свелась к строчной")

    def test_a_lowercase_question_finds_a_capitalised_fact(self):
        found = finds("город", "Человек живёт в Казани", "где я живу казани")
        self.assertTrue(found, "строчный вопрос не нашёл заглавный факт")

    def test_a_full_stop_at_the_end_of_the_question_changes_nothing(self):
        """Вопрос пары про забор кончается «…есть дома.» — точка съедала выдачу."""
        subject, content = "забор", "У человека дома забор из штакетника"
        self.assertTrue(finds(subject, content, "какой у меня забор есть дома"))
        self.assertTrue(finds(subject, content, "какой у меня забор есть дома."),
                        "точка в конце предложения ушла в слово вопроса")

    def test_yo_and_ye_are_one_letter(self):
        """«живёт» в факте, «живет» в вопросе — и наоборот."""
        self.assertTrue(finds("город", "Человек живёт в Казани", "где он живет"))
        self.assertTrue(finds("город", "Человек живет в Казани", "где он живёт"))

    def test_yo_is_folded_in_a_name_too(self):
        """Имя ветки или файла стеммер не трогает — `ё` в нём сводим сами.

        Русское слово сводит к основе Snowball, и `ё` он сворачивает по дороге
        сам. Но имя с дефисом или точкой русским словом не считается и идёт
        насквозь: не сведи мы букву до него, `чёрный-фикс.py` в факте и
        `черный-фикс.py` в вопросе остались бы двумя разными словами.
        """
        self.assertTrue(
            finds("файл", "правился файл чёрный-фикс.py", "что там с черный-фикс.py"),
            "`ё` в имени не сведена")

    def test_a_different_word_form_still_finds_the_fact(self):
        subject, content = "завтрак", "Человек ест завтрак в восемь утра"
        for question in ("завтрак", "завтраки", "завтраком", "про завтраки"):
            self.assertTrue(finds(subject, content, question),
                            "словоформа %r не нашла факт" % question)


class TestCaseNeverMatters(unittest.TestCase):
    """Свойство: заглавная буква в факте не прячет его от строчного вопроса."""

    @SLOW
    @given(word=base)
    def test_a_capitalised_fact_is_found_by_a_lowercase_question(self, word):
        assume(query.words(word))
        self.assertTrue(finds("тема", "Человек живёт в %s" % word.capitalize(),
                              word),
                        "заглавная %r не нашлась по строчной" % word)


class TestPunctuationOnTheEdgeNeverMatters(unittest.TestCase):
    """Свойство: знак на краю слова не меняет выдачу."""

    @SLOW
    @given(word=base, tail=edge)
    def test_a_question_ending_in_a_sign_still_finds_the_fact(self, word, tail):
        assume(query.words(word))
        self.assertTrue(finds("тема", "У человека есть %s" % word, word + tail),
                        "знак %r на конце вопроса съел выдачу" % tail)


class TestAnyFormOfAWordFindsTheFact(unittest.TestCase):
    """Свойство: в какой форме ни спроси, факт находится."""

    @SLOW
    @given(lemma=paradigm, kept=st.integers(), asked=st.integers())
    def test_a_question_in_another_form_finds_the_fact(self, lemma, kept, asked):
        """Факт записан в одной форме, вопрос задан в другой. Находит."""
        forms = PARADIGMS[lemma].split()
        one, two = forms[kept % len(forms)], forms[asked % len(forms)]
        assume(query.words(two))
        self.assertTrue(finds("тема", "У человека есть %s" % one, two),
                        "вопрос %r не нашёл факт про %r" % (two, one))


class TestNoNewFalseHits(unittest.TestCase):
    """Вторая сторона: чужое слово по-прежнему не находится.

    Сведение к основе расширяет выдачу, и без этой пары свойств починка меняла
    бы обрыв на вброс чужого. Чужим считаем слово, у которого с фактом не
    сходится даже начало основы.
    """

    @SLOW
    @given(kept=base, asked=base)
    def test_a_word_sharing_nothing_with_the_fact_is_not_found(self, kept, asked):
        """Тема и текст факта нарочно латиницей: русский вопрос цепляется
        только за русское слово, и падение свойства значит именно ложную
        находку, а не совпадение с окружением."""
        a, b = query.stem(kept), query.stem(asked)
        assume(a not in b and b not in a)
        assume(query.words(asked))
        self.assertFalse(finds("topic-1", "note: %s" % kept, asked),
                         "чужое слово %r нашло факт про %r" % (asked, kept))

    @given(text=st.text(alphabet=RU + "abc ./:-", max_size=60))
    def test_the_key_never_invents_a_word(self, text):
        """Каждое слово ключа — начало слова текста. Основа режет, не дописывает.

        Без этого свойства «сведение к основе» могло бы склеить соседние слова
        или дописать букву, и находкой считалось бы то, чего в тексте нет.
        """
        source = query.key(text).split()
        origin = [m.group(0).lower() for m in query.WORD.finditer(text.lower())]
        for word in source:
            self.assertTrue(any(word in one for one in origin),
                            "слова ключа %r в тексте нет вовсе" % word)


class TestOneRuleOnBothSides(unittest.TestCase):
    """Ключ пишется и спрашивается одной функцией. Разойтись нечему."""

    @given(text=st.text(alphabet=RU + RU.upper() + "abc ./:-", max_size=60))
    def test_the_database_normalises_a_field_exactly_as_python_does(self, text):
        """То, с чем сравнивает запрос в базе, — это `query.key` и ничто иное."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = db.Repository(Path(tmp) / "memory.db")
            try:
                got = repo.conn.execute("SELECT %s(?)" % db.KEY_SQL,
                                        (text,)).fetchone()[0]
            finally:
                repo.close()
        self.assertEqual(got, query.key(text))

    @given(question=st.text(alphabet=RU + "abc ./:-", max_size=60))
    def test_the_sift_asks_the_same_words_as_the_search(self, question):
        self.assertEqual(suggest.terms_of(question), query.words(question))

    @given(lemma=paradigm, first=st.integers(), second=st.integers())
    def test_the_sift_judges_a_word_form_the_same_as_its_stem(
            self, lemma, first, second):
        """Отсев судит по тем же основам: иначе он режет свою же находку.

        Поиск нашёл запись по основе — значит и отсев обязан узнать в поле ту
        же основу. Сравнивай он поле как есть, вопрос в чужой форме («что было
        в ветках») выбрасывал бы находку, которую поиск только что вернул.
        """
        forms = PARADIGMS[lemma].split()
        one, two = forms[first % len(forms)], forms[second % len(forms)]
        assume(query.words(one) and query.words(two))
        record = {"object_type": "Event", "tool_name": "Bash", "project": "demo",
                  "git_branch": one}
        self.assertEqual(suggest.incidental(record, query.words(two)),
                         suggest.incidental(record, query.words(one)),
                         "отсев судит форму %r иначе, чем %r" % (two, one))

    def test_the_sift_keeps_an_event_the_search_found_by_its_key(self):
        """Отсев обязан узнать в поле ту же букву, какой поиск запись нашёл.

        Событие в ветке `чёрный`, вопрос — «что делали в черный». Поиск находит
        по ключу, где `ё` сведена; сравнивай отсев поле как есть — он не увидел
        бы в нём слова вопроса и выбросил бы свою же находку.
        """
        record = {"object_type": "Event", "tool_name": "Bash", "project": "demo",
                  "git_branch": "чёрный"}
        self.assertFalse(
            suggest.incidental(record, query.words("что делали в черный")),
            "отсев выбросил находку, которую поиск вернул")


if __name__ == "__main__":
    unittest.main()
