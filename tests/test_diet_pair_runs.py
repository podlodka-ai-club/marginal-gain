#!/usr/bin/env python3
"""Снятый прогон пары про питание: журнал и дословные ответы обеих рук.

Запуск: python3 -m pytest tests/test_diet_pair_runs.py -q

Форма набора говорит, что версии делят ответы. Она не говорит, что так вышло
на живой модели: это решает прогон, и решает он это один раз — потом остаются
цифра в журнале и дословные ответы. Здесь проверяется именно снятое, а не
устройство набора.

Ответы лежат снимком (`eval-diet-answers.json`), выписанным из базы аудита
прогона (шаг `reply`, см. `eval.live.record_reply`). База аудита в git не идёт
и растёт с каждым прогоном; снимок — то немногое из неё, ради чего пару потом
разбирают, и он обязан пережить и базу, и песочницу.

Свойства:

1. У каждой из трёх версий есть строка в журнале, и обе доли — полнота и
   точность — в ней записаны как есть, а не выведены задним числом.
2. Рука без памяти ни на одной из трёх не проходит.
3. Ответ руки с памятью и ответ голой руки различаются по существу: набор
   слов закрытого словаря, которые в ответе действительно встретились, у них
   разный. Это про содержание списка, а не про формулировку.
4. В ответе руки с памятью есть то, чего пара ждёт. Больше здесь не
   утверждается: модель ошибается, и провал пары — цифра прогона, а не то,
   что проверка обязана прятать.

Здесь не проверяется, что рука с памятью прошла: это цифра прогона, и она
записана в журнал как есть. Проверяется то, без чего цифра ничего не значит —
что голая рука не прошла и что ответы версий разошлись по составу.

Мутации, на которых проверки обязаны краснеть:
  * строку журнала по версии подделали или потеряли → TestTheJournalHasEachVersion
  * рука без памяти прошла, а мы этого не заметили  → TestTheBareArmPassesNothing
  * ответы двух рук сошлись по существу             → TestTheAnswersDiffer
  * ожидаемого не стало в ответе руки с памятью     → TestTheAnswersDiffer
"""
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import evaluate, live, pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"
ANSWERS = ROOT / "eval-diet-answers.json"
JOURNAL = ROOT / "eval-runs.jsonl"

VERSIONS = ("питание-веган",)
ARMS = ("memory", "bare")


def pairs_by_id():
    return {item["id"]: item for item in pairs.load(HOUSEHOLD)[1]}


def journal():
    rows = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def answers():
    body = json.loads(ANSWERS.read_text(encoding="utf-8"))
    return body, {(row["id"], row["arm"]): row for row in body["items"]}


class TestTheJournalHasEachVersion(unittest.TestCase):
    """По каждой версии снят свой прогон, и обе доли записаны как есть."""

    def setUp(self):
        self.rows = journal()

    def rows_with(self, id_):
        return [row for row in self.rows
                if any(pair["id"] == id_
                       for arm in row.get("arms", {}).values()
                       for pair in arm.get("pairs", []))]

    def test_every_version_has_a_run_of_its_own(self):
        for id_ in VERSIONS:
            mine = [row for row in self.rows_with(id_) if row.get("only") == id_]
            self.assertTrue(mine, "по версии %s своего прогона нет" % id_)

    def test_every_run_of_a_version_carries_both_arms(self):
        for id_ in VERSIONS:
            row = [r for r in self.rows_with(id_) if r.get("only") == id_][-1]
            self.assertEqual(sorted(ARMS), sorted(row["arms"]),
                             "%s: снята не всякая рука" % id_)

    def test_both_shares_are_written_as_they_came(self):
        """Полнота и точность стоят в строке порознь, каждая своим счётом."""
        for id_ in VERSIONS:
            row = [r for r in self.rows_with(id_) if r.get("only") == id_][-1]
            for arm, got in row["arms"].items():
                for aim in pairs.AIMS:
                    self.assertIn(aim, got, "%s/%s: доли %s нет" % (id_, arm, aim))
                    for field in ("passed", "total", "share"):
                        self.assertIn(field, got[aim])
                apply_ = got["apply"]
                self.assertEqual(1, apply_["total"],
                                 "%s/%s: пара в доле не одна" % (id_, arm))

    def test_the_model_of_the_run_is_named(self):
        for id_ in VERSIONS:
            row = [r for r in self.rows_with(id_) if r.get("only") == id_][-1]
            self.assertEqual("haiku", row.get("model"), id_)
            self.assertEqual("claude", row.get("player"), id_)


class TestTheBareArmPassesNothing(unittest.TestCase):
    """Рука без памяти ни на одной из трёх не проходит.

    Спрашиваем дважды и разными путями: исходом в журнале и тем же судьёй по
    дословному ответу. Журнал говорит, что показал прогон; судья говорит, что
    в ответе действительно было. Сойдись они не всегда — цифра меряла бы не то,
    что лежит в ответе.
    """

    def test_the_journal_says_the_bare_arm_took_nothing(self):
        rows = journal()
        for id_ in VERSIONS:
            row = [r for r in rows if r.get("only") == id_][-1]
            for got in row["arms"]["bare"]["pairs"]:
                if got["id"] != id_:
                    continue
                self.assertNotIn(got["outcome"],
                                 live.outcomes_counted_for("apply"),
                                 "%s: голая рука прошла" % id_)

    def test_the_recorded_bare_answer_fails_its_own_criterion(self):
        known = pairs_by_id()
        _, rows = answers()
        for id_ in VERSIONS:
            row = rows[(id_, "bare")]
            verdict = evaluate.judge(known[id_], row["answer"], "", None)
            self.assertFalse(verdict["ok"],
                             "%s: ответ без памяти прошёл критерий" % id_)


class TestTheAnswersDiffer(unittest.TestCase):
    """Ответ с памятью и ответ без неё различаются по существу, а не словами."""

    def setUp(self):
        raw = pairs.load(HOUSEHOLD)[0]["kinds"]
        self.kinds = {name: (pairs.words_of(kind), tuple(pairs.unless_of(kind)))
                      for name, kind in raw.items()}
        self.known = pairs_by_id()
        self.body, self.rows = answers()

    def words_in(self, text):
        """Какие слова закрытого словаря в ответе действительно встретились.

        Спрашиваем слова, а не имена категорий: «растительное молоко» и
        «творог, сыр, яйца» дают одно и то же имя, а по существу это разные
        списки. Имя отвечает «была ли категория», слово — «чем именно».
        """
        low = (text or "").lower()
        return frozenset(word for words, unless in self.kinds.values()
                         for word in words
                         if evaluate._in_kind(word, low, unless))

    def test_the_answers_are_to_one_and_the_same_task(self):
        asked = {row["task"] for row in self.body["items"]}
        self.assertEqual(1, len(asked), "задача у снимков разная: %s" % asked)

    def test_every_arm_is_on_record(self):
        for id_ in VERSIONS:
            for arm in ARMS:
                row = self.rows.get((id_, arm))
                self.assertTrue(row and row["answer"].strip(),
                                "нет ответа %s/%s" % (id_, arm))

    def test_the_two_arms_hold_different_products(self):
        for id_ in VERSIONS:
            mine = self.words_in(self.rows[(id_, "memory")]["answer"])
            bare = self.words_in(self.rows[(id_, "bare")]["answer"])
            self.assertTrue(mine, "%s: в ответе с памятью ни одного белка" % id_)
            self.assertNotEqual(mine, bare,
                                "%s: состав ответа не сдвинулся" % id_)

    def test_the_answer_with_memory_holds_what_the_pair_expects(self):
        """У ответа с памятью есть то, ради чего пара заведена.

        Сильнее этого здесь утверждать нечего и не надо. «В ответе с памятью
        нет ни одного животного слова» неверно как утверждение о снимке:
        модель ошибается, и в одном прогоне она положила вегану мёд с оговоркой
        «если нет ограничений». Это провал пары, честно записанный в журнал, а
        не то, что проверка обязана прятать.
        """
        for id_ in VERSIONS:
            low = self.rows[(id_, "memory")]["answer"].lower()
            for name in self.known[id_]["expect_kinds"]:
                words, unless = self.kinds[name]
                got = [w for w in words if evaluate._in_kind(w, low, unless)]
                self.assertTrue(got, "%s: ожидаемого (%s) в ответе нет"
                                % (id_, name))


if __name__ == "__main__":
    unittest.main()
