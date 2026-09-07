#!/usr/bin/env python3
"""`порт-draco`: верный ответ обязан назвать 5432 и не должен на этом падать.

Запуск: python3 -m pytest tests/test_port_draco_forbid.py -q

Задача пары спрашивает не только порт, но и «почему именно его». Объяснить
нестандартный порт draco (5433), не назвав стандартный (5432, который держит
ещё не погашенный старый инстанс), нельзя. Значит любой полный верный ответ
несёт подстроку `5432` — и запрет на голую подстроку `5432` валит именно
верный ответ. Тот же класс, что «макбук», где верные ответы считались
провалами.

База аудита прогона по этой формулировке снесена вместе с песочницей, но в
ней сохранились дословные ответы руки с памятью на сессии 1 (шаг `reply`,
run `db757cc57db0`): применяя факт, рука пишет «5433 вместо стандартного
5432, потому что старый инстанс на 5432 ещё жив». Этот ответ verbatim — в
`test_the_verbatim_answer_from_the_audit_passes`.

Свойства:

1. Ответ, который рекомендует 5433 и называет 5432 только как занятый старый
   инстанс, проходит судью: ни одной ложной находки, `ok` истинно.
2. Пара по-прежнему кусается: ответ без 5433 (голая рука назовёт индустриальный
   дефолт 5432) судью не проходит — применение факта всё ещё решает исход.

Мутации, на которых проверки обязаны краснеть:
  * в `forbid` пары вернули `"5432"`            → пункт 1 краснеет
  * из пары убрали ожидание `"5433"`            → пункт 2 краснеет (ответ без
                                                  факта начинает проходить)
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from hypothesis import given, settings
from hypothesis import strategies as st

from eval import evaluate, pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

PAIR = {item["id"]: item for item in pairs.load(HOUSEHOLD)[1]}["порт-draco"]

# Рекомендация: в ответе есть 5433 как порт, который надо указать.
RECOMMEND = (
    "В строке подключения к постгресу draco укажи порт 5433",
    "Порт — 5433",
    "Используй 5433",
    "postgres на draco слушает 5433, его и указывай в строке подключения",
)

# Причина: 5432 назван только как занятый/старый, а не как рабочий порт.
BECAUSE_5432_IS_OLD = (
    "потому что 5432 занят ещё не погашенным старым инстансом",
    "стандартный 5432 сейчас держит старая база",
    "5433 стоит вместо стандартного 5432, который заняли под старый инстанс",
    "на 5432 всё ещё жив старый инстанс, его не погасили",
)

# Ответ без факта: голая рука назовёт индустриальный дефолт и не вспомнит 5433.
DEFAULT_ONLY = (
    "Укажи стандартный порт 5432",
    "Порт 5432 — дефолт PostgreSQL",
    "В строке подключения к postgres обычно 5432",
    "5432, стандартный порт постгреса",
)


class TestCorrectAnswerIsNotRejected(unittest.TestCase):
    """Верный ответ называет 5432 как вытесненный инстанс и не падает на этом."""

    @settings(deadline=None)
    @given(rec=st.sampled_from(RECOMMEND), why=st.sampled_from(BECAUSE_5432_IS_OLD))
    def test_naming_5432_as_the_displaced_instance_passes(self, rec, why):
        answer = "%s, %s." % (rec, why)
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertEqual([], verdict["false_hits"],
                         "верный ответ завален запретом: %r" % answer)
        self.assertTrue(verdict["ok"], "верный ответ не прошёл: %r" % answer)

    def test_the_verbatim_answer_from_the_audit_passes(self):
        # Дословно из базы аудита: run db757cc57db0, шаг reply, рука с памятью.
        answer = ("на draco PostgreSQL на 5433 вместо стандартного 5432, "
                  "потому что старый инстанс на 5432 ещё жив.")
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertEqual([], verdict["false_hits"])
        self.assertTrue(verdict["ok"])


class TestThePairStillNeedsTheFact(unittest.TestCase):
    """Без 5433 в ответе пара не проходит — факт всё ещё решает исход."""

    @settings(deadline=None)
    @given(rec=st.sampled_from(DEFAULT_ONLY))
    def test_an_answer_without_5433_does_not_pass(self, rec):
        answer = rec + "."
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertFalse(verdict["ok"],
                         "ответ без факта (нет 5433) прошёл: %r" % answer)


class TestTheForbidDoesNotCatchAPieceOfEveryCorrectAnswer(unittest.TestCase):
    """Быстрый якорь: пара не запрещает подстроку, обязательную в верном ответе.

    `5432` обязан прозвучать в любом полном ответе — как объяснение, почему
    берётся нестандартный порт. Держать его в `forbid` значит валить пару её
    же верным ответом.
    """

    def test_5432_is_not_forbidden_as_a_bare_substring(self):
        self.assertNotIn("5432", PAIR.get("forbid") or [],
                         "запрет на подстроку 5432 валит верный ответ")


if __name__ == "__main__":
    unittest.main()
