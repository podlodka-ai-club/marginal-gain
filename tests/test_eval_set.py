#!/usr/bin/env python3
"""Набор эталонов носит версию, и её проверяют при чтении.

Запуск: python3 -m unittest tests.test_eval_set -v

Набор — данные, собранные кодом. Меняется код сборки — меняется смысл цифры,
полученной на наборе. До сих пор набор лежал голым списком: файл, собранный
прежними правилами, читался молча и давал число, несравнимое с прежним.
Так и вышло с подписью факта: набор, собранный на подписи-проекте, спрашивал
«какие файлы правились в проекте <путь к файлу>» — валидные случаи, которые
не мог пройти никто, и это не падало, а искажало замер.

Версия закрывает ровно это: чужой набор не читается вовсе, а ошибка говорит,
чем его пересобрать.

Проверки заданы свойствами: важно не «этот файл читается», а «любой чужой
набор не читается, а свой возвращается тем же, чем был».
"""
import json, os, tempfile, unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import goldenset

HERE = Path(__file__).resolve().parent.parent
KINDS = ("cases", "fixture", "script")

# Случай набора: словарь с простыми значениями. Форму набора эти свойства не
# описывают — её описывают проверки в test_adapters; здесь важен только конверт.
ITEM = st.dictionaries(
    st.sampled_from(["id", "kind", "query", "expect", "repeated"]),
    st.one_of(st.text(max_size=20), st.booleans(), st.integers(min_value=-5, max_value=5),
              st.lists(st.text(max_size=10), max_size=3)),
    max_size=5)

ITEMS = st.lists(ITEM, max_size=10)

QUICK = settings(deadline=None, max_examples=30)


class TestTheEnvelopeKeepsWhatItWasGiven(unittest.TestCase):
    """Конверт добавляет версию и не трогает содержимое."""

    @QUICK
    @given(kind=st.sampled_from(KINDS), items=ITEMS)
    def test_what_is_written_is_what_is_read(self, kind, items):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            goldenset.dump(path, kind, items, split="2026-08-09")
            meta, got = goldenset.load(path, kind)
            self.assertEqual(got, items)
            self.assertEqual(meta["version"], goldenset.VERSION)
            self.assertEqual(meta["kind"], kind)
            self.assertEqual(meta["split"], "2026-08-09")

    @QUICK
    @given(kind=st.sampled_from(KINDS), items=ITEMS)
    def test_the_file_says_how_many_it_holds(self, kind, items):
        """Число в конверте сходится со списком: усечённый файл виден сразу."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            goldenset.dump(path, kind, items)
            body = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(body["count"], len(items))


class TestAForeignSetIsNotRead(unittest.TestCase):
    """Чужой набор не читается молча. Молчание тут дороже падения."""

    @QUICK
    @given(kind=st.sampled_from(KINDS),
           other=st.integers(min_value=-3, max_value=99).filter(
               lambda n: n != goldenset.VERSION))
    def test_another_version_is_refused(self, kind, other):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            path.write_text(json.dumps({"version": other, "kind": kind,
                                        "count": 0, "items": []}),
                            encoding="utf-8")
            with self.assertRaises(goldenset.SetVersionError) as caught:
                goldenset.load(path, kind)
            self.assertIn("goldenset", str(caught.exception),
                          "ошибка не говорит, чем пересобрать набор")

    @QUICK
    @given(items=ITEMS)
    def test_the_old_headless_format_is_refused(self, items):
        """Прежний набор — голый список. Ровно он и давал молчаливый перекос."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            path.write_text(json.dumps(items), encoding="utf-8")
            with self.assertRaises(goldenset.SetVersionError):
                goldenset.load(path, "cases")

    @QUICK
    @given(kind=st.sampled_from(KINDS), items=ITEMS,
           wrong=st.integers(min_value=0, max_value=20))
    def test_a_truncated_file_is_refused(self, kind, items, wrong):
        """Конверт обещает число записей. Разошлось — файл оборван, читать нельзя.

        Набор пишется целиком и читается целиком; половина набора даёт цифру,
        которая выглядит как полная. Обещание в заголовке — единственное, чем
        обрыв отличается от честно маленького набора.
        """
        if wrong == len(items):
            return
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            body = {"version": goldenset.VERSION, "kind": kind,
                    "count": wrong, "items": items}
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.assertRaises(goldenset.SetVersionError):
                goldenset.load(path, kind)

    @QUICK
    @given(kind=st.sampled_from(KINDS), other=st.sampled_from(KINDS))
    def test_a_set_of_another_kind_is_refused(self, kind, other):
        """Случаи вместо реплик — не ошибка чтения, а неверный замер."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            goldenset.dump(path, kind, [])
            if kind == other:
                self.assertEqual(goldenset.load(path, other)[1], [])
                return
            with self.assertRaises(goldenset.SetVersionError):
                goldenset.load(path, other)


class TestTheShippedSetCarriesItsVersion(unittest.TestCase):
    """То, что уехало в репозиторий, читается штатным путём и носит версию."""

    def test_every_shipped_file_is_of_this_version(self):
        for name, kind in (("eval-cases.json", "cases"),
                           ("eval-fixture.json", "fixture"),
                           ("eval-script.json", "script")):
            meta, items = goldenset.load(HERE / name, kind)
            self.assertEqual(meta["version"], goldenset.VERSION, name)
            self.assertTrue(items, "%s пуст" % name)
            self.assertEqual(meta["count"], len(items), name)

    def test_the_shipped_set_knows_how_facts_are_signed(self):
        """Подпись факта — часть смысла набора, поэтому она в конверте.

        Набор, собранный на прежней подписи, спрашивал про проект именем
        файла. Пометка в конверте — то, по чему это видно, не перечитывая
        сто случаев.
        """
        meta, _ = goldenset.load(HERE / "eval-cases.json", "cases")
        self.assertEqual(meta["identity"], goldenset.IDENTITY)

    def test_a_file_question_asks_about_a_project(self):
        """Ни один вопрос про файлы не спрашивает про путь вместо проекта."""
        _, cases = goldenset.load(HERE / "eval-cases.json", "cases")
        asked = [c for c in cases if c["kind"] == "fact"]
        self.assertTrue(asked, "случаев про файлы в наборе нет")
        for case in asked:
            tail = case["query"].split("в проекте ")[-1]
            self.assertNotIn("/", tail, case["id"])


if __name__ == "__main__":
    unittest.main()
