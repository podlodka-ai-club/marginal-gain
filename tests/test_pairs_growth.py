#!/usr/bin/env python3
"""Набор вырос с восьми пар до двадцати. Что именно выросло — здесь.

Запуск: python3 -m pytest tests/test_pairs_growth.py -q

Восьми пар мало: из тройки про питание выжила одна, и цифра с такого набора
скачет от пары к паре. Двенадцать новых написаны по фактам из фикстуры коллеги
про вымышленные проекты atlas, borealis и draco — только по сильным, тем, где у
агента есть уверенный неверный дефолт, который факт перебивает.

Растёт набор дописыванием, и у роста два риска, которых json-файл не заметит:
прежние восемь правятся заодно с новыми (по ним снята история цифр в журнале),
а новая пара приезжает без того, чем она вообще меряет. Форму каждой пары
проверяет `tests/test_pairs_form.py`, снимок опорной четвёрки —
`tests/test_pairs_contract.py`; здесь проверяется состав набора.

Свойства:

1. В наборе двадцать пар, и счётчик конверта сходится со списком.
2. Восемь прежних пар на месте, двенадцать новых доехали, id не повторяются.
3. У каждой новой пары сказано, почему факт решает исход (`matters`).
4. Каждая новая пара положительная и чего-то ждёт: без факта ответ обязан
   стать неверным, а не просто другим по формулировке.

Мутации, на которых проверки обязаны краснеть:
  * пара из набора пропала или набор не вырос → TestTheSetGrewToTwenty
  * новую пару завели без `matters`           → TestTheSetGrewToTwenty
  * новую пару завели одним запретом          → TestTheSetGrewToTwenty
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

# Восемь пар, с которых набор начинал: по ним снята история цифр в журнале,
# и трогать их эта задача права не имела.
OLD = ("завтрак", "город", "забор", "макбук", "овсянка-ужин",
       "макбук-не-в-тему", "кровь", "питание-веган")

# Двенадцать новых: две написаны оператором, десять — по фактам фикстуры.
NEW = ("порт-draco", "ветка-draco", "секреты-borealis", "префикс-borealis",
       "готовность-borealis", "линт-atlas", "пакеты-atlas", "воркер-borealis",
       "лок-borealis", "нода-borealis", "тесты-atlas", "миграции-borealis")


class TestTheSetGrewToTwenty(unittest.TestCase):

    def setUp(self):
        self.body, self.items = pairs.load(HOUSEHOLD)
        self.by_id = {item["id"]: item for item in self.items}

    def test_the_envelope_counts_what_the_list_holds(self):
        self.assertEqual(len(self.items), self.body["count"])

    def test_the_set_holds_twenty_pairs(self):
        self.assertEqual(20, len(self.items))

    def test_the_eight_earlier_pairs_are_all_still_here(self):
        missing = [id_ for id_ in OLD if id_ not in self.by_id]
        self.assertEqual([], missing, "пропала прежняя пара: %s" % missing)

    def test_the_twelve_new_pairs_are_all_here(self):
        missing = [id_ for id_ in NEW if id_ not in self.by_id]
        self.assertEqual([], missing, "новая пара не доехала: %s" % missing)

    def test_no_pair_is_named_twice(self):
        self.assertEqual(len(self.items), len({item["id"] for item in self.items}))

    def test_every_new_pair_says_why_the_fact_decides_the_answer(self):
        for id_ in NEW:
            note = (self.by_id[id_].get("matters") or "").strip()
            self.assertTrue(note, "%s: не сказано, почему факт решает исход" % id_)

    def test_every_new_pair_asks_for_something_and_not_only_forbids(self):
        """Новые пары все положительные: без факта ответ обязан стать неверным."""
        for id_ in NEW:
            item = self.by_id[id_]
            self.assertEqual("apply", item["aim"], id_)
            self.assertTrue(item.get("expect"), "%s: нечего применять" % id_)


if __name__ == "__main__":
    unittest.main()
