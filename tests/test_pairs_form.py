#!/usr/bin/env python3
"""Форма пары: изъяны, которые json-файл принимает молча.

Запуск: python3 -m pytest tests/test_pairs_form.py -q

Набор растёт по паре за раз, а линейка у всех пар одна и та же — вхождение строки. Отсюда два изъяна, которые файл принимает
не поморщившись, прогон тратит на них живые деньги, а возвращает цифру, не
мерящую ничего:

1. **Запрет сидит внутри ожидания.** `/health` живёт внутри `/healthz`, `npm`
   внутри `pnpm`, `pnpm start` внутри `pnpm run start`. Такая пара непроходима
   по построению: ответ, удовлетворивший ожиданию, тем же куском строки
   поднимает запрет — `ok` у неё ложно при любом ответе. Класс тот же, что у
   пустого `tell` у `apply`-пары, и ловиться должен там же, в `validate`, до
   прогона, а не через час живого стенда.
2. **Ожидание названо в самой задаче.** Тогда меряется чтение задачи, а не
   память: рука без памяти проходит пару, списав ответ с вопроса.

Обратная сторона изъяна 1 изъяном не является: ожидание внутри запрета
(`/health` ждём, `/healthz` запрещаем) оставляет ответ, который удовлетворяет
одному и не задевает другого, — пара проходима, и запрещать её нечем.

Свойства:

1. Запрет, сидящий внутри ожидания той же пары, — не проходит `validate`,
   какого бы регистра он ни был.
2. Такая пара и правда непроходима: судья на любом ответе, удовлетворившем
   ожиданию, возвращает `ok` ложным. Свойство проверяется судьёй, а не
   пересказом — иначе запрет держится на слове докстринга.
3. Пара, где ни один запрет не сидит в ожидании, проходит и возвращается той
   же записью.
4. Ожидание внутри запрета — не изъян, пара проходит.
5. В самом наборе ни у одной пары запрет не сидит в ожидании.
6. В самом наборе ни одно ожидание не названо в тексте задачи.
Мутации, на которых проверки обязаны краснеть:
  * проверку «запрет внутри ожидания» убрали из `validate` → TestForbidInsideExpect
  * ожидаемое слово вписали в текст задачи новой пары      → TestTaskNeverNamesTheExpectation
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

# --- строительные блоки пары ------------------------------------------------
#
# Ожидание и запрет берутся из непересекающихся алфавитов: так «запрет не сидит
# внутри ожидания» выполняется по построению, а не отсевом, и генератор не
# тратит примеры на выбраковку.

WANTED = st.text(alphabet="абвгде", min_size=1, max_size=8)
BANNED = st.text(alphabet="xyzw", min_size=1, max_size=8)
SAY = st.text(min_size=1, max_size=40).filter(lambda s: s.strip())


def a_turn():
    return st.builds(dict, say=SAY, place=st.text(max_size=10))


def a_pair(expect, forbid, aim="apply"):
    return st.builds(
        dict,
        id=st.text(min_size=1, max_size=10).filter(lambda s: s.strip()),
        aim=st.just(aim),
        tell=st.lists(a_turn(), min_size=1, max_size=3),
        task=a_turn(),
        expect=expect,
        forbid=forbid,
    )


CLEAN = a_pair(st.lists(WANTED, min_size=1, max_size=3),
               st.lists(BANNED, min_size=0, max_size=3))


@st.composite
def a_pair_with_forbid_inside_expect(draw):
    """Пара, у которой запрет — кусок собственного ожидания."""
    pair = draw(CLEAN)
    wanted = draw(st.sampled_from(pair["expect"]))
    start = draw(st.integers(min_value=0, max_value=len(wanted) - 1))
    end = draw(st.integers(min_value=start + 1, max_value=len(wanted)))
    piece = wanted[start:end]
    return dict(pair, forbid=list(pair["forbid"]) + [piece]), wanted, piece


class TestForbidInsideExpect(unittest.TestCase):
    """Запрет внутри ожидания — изъян формы, а не находка прогона."""

    @given(built=a_pair_with_forbid_inside_expect())
    @FAST
    def test_a_forbid_inside_an_expect_is_rejected(self, built):
        broken, _, _ = built
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(built=a_pair_with_forbid_inside_expect())
    @FAST
    def test_the_case_of_the_letters_does_not_save_it(self, built):
        """Судья сравнивает в нижнем регистре — значит и проверка обязана."""
        broken, _, piece = built
        louder = dict(broken, forbid=[f.upper() for f in broken["forbid"]])
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(louder)

    @given(built=a_pair_with_forbid_inside_expect(),
           left=st.text(max_size=10), right=st.text(max_size=10))
    @FAST
    def test_such_a_pair_can_never_pass_the_judge(self, built, left, right):
        """Непроходима по построению — спрошено у судьи, а не пересказано.

        Ответ, удовлетворивший ожиданию, содержит и запрет: это один и тот же
        кусок строки. Значит `ok` ложно при любом ответе, и мерить парой нечего.
        """
        broken, wanted, _ = built
        answer = "%s%s%s" % (left, wanted, right)
        verdict = evaluate.judge(broken, answer, answer, None)
        self.assertTrue(verdict["false_hits"])
        self.assertFalse(verdict["ok"])

    @given(pair=CLEAN)
    @FAST
    def test_a_pair_without_the_defect_comes_back_unchanged(self, pair):
        self.assertEqual(pair, pairs.validate(dict(pair)))

    @given(pair=CLEAN, tail=st.text(alphabet="xyzw", min_size=1, max_size=4))
    @FAST
    def test_an_expect_inside_a_forbid_is_not_a_defect(self, pair, tail):
        """Обратная сторона проходима: ответ может взять одно и не задеть другое."""
        wanted = pair["expect"][0]
        fine = dict(pair, forbid=list(pair["forbid"]) + [wanted + tail])
        self.assertEqual(fine, pairs.validate(fine))


class TestTheSetItselfIsClean(unittest.TestCase):
    """Оба изъяна проверяются на самом наборе, а не только на выдуманных парах."""

    def setUp(self):
        _, self.items = pairs.load(HOUSEHOLD)

    def test_no_pair_forbids_a_piece_of_its_own_expectation(self):
        for item in self.items:
            for banned in item.get("forbid") or []:
                for wanted in item.get("expect") or []:
                    self.assertNotIn(
                        banned.lower(), wanted.lower(),
                        "%s: запрет %r сидит внутри ожидания %r"
                        % (item["id"], banned, wanted))


class TestTaskNeverNamesTheExpectation(unittest.TestCase):
    """Ожидание в тексте задачи — пара меряет чтение вопроса, а не память."""

    def test_no_task_spells_out_what_the_answer_must_contain(self):
        _, items = pairs.load(HOUSEHOLD)
        for item in items:
            said = (item["task"].get("say") or "").lower()
            for wanted in item.get("expect") or []:
                self.assertNotIn(
                    wanted.lower(), said,
                    "%s: ожидание %r названо в самой задаче" % (item["id"], wanted))


if __name__ == "__main__":
    unittest.main()
