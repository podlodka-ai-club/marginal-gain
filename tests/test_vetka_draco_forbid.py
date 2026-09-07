#!/usr/bin/env python3
"""`ветка-draco`: объяснение переименования вправе назвать старое имя.

Запуск: python3 -m pytest tests/test_vetka_draco_forbid.py -q

Факт пары: основную ветку draco переименовали `main` → `trunk`. Верная
шпаргалка новичку объясняет это переименование, и назвать в объяснении старое
имя (`master` у части команд, `main` у draco) — законно: «раньше называлась
main, теперь trunk, заводи от trunk». Запрет на голую подстроку `master` валил
бы такой ответ — тот же класс, что `порт-draco` с запретом `5432`.

`origin/main` в запрете остаётся: назвать `origin/main` **рабочей** базовой
веткой — это и есть невыполненное применение факта, а короткая форма `main`
внутри объяснения под запрет не подводится (`origin/main` длиннее и как
подстрока в «main переименовали» не входит).

Свойства:

1. `master` не значится в `forbid` пары.
2. Ответ, который советует ветку от `trunk` и упоминает прежнее имя лишь как
   контекст переименования, проходит судью: ни одной ложной находки, `ok`.
3. Пара по-прежнему кусается: ответ без `trunk` (голая рука заведёт от
   `main`/`master`) судью не проходит.
4. `origin/main` как рабочая база — ложная находка: запрет на него жив.

Мутации, на которых проверки обязаны краснеть:
  * в `forbid` вернули `"master"`            → пункт 2 краснеет
  * из пары убрали ожидание `"trunk"`       → пункт 3 краснеет
  * из `forbid` убрали `"origin/main"`      → пункт 4 краснеет
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

PAIR = {item["id"]: item for item in pairs.load(HOUSEHOLD)[1]}["ветка-draco"]

# Совет завести ветку от trunk.
FROM_TRUNK = (
    "Заводи ветку от trunk: git switch -c fix/… trunk",
    "Базовая ветка — trunk, от неё и ответвляйся",
    "git checkout trunk && git pull, потом git checkout -b fix/… от trunk",
)

# Объяснение переименования, где старое имя названо только как контекст.
RENAME_NOTE = (
    "раньше основная ветка звалась main, её переименовали в trunk весной",
    "если помнишь master из старых проектов — здесь его роль играет trunk",
    "main больше нет, вместо неё trunk",
)

# Ответ без факта: голая рука заводит ветку от общепринятого дефолта.
DEFAULT_BASE = (
    "Заводи ветку от main: git checkout -b fix/… main, потом влей обратно",
    "git checkout master && git checkout -b fix/…",
    "Ответвляйся от master, это основная ветка почти везде",
)


class TestMasterIsNotForbidden(unittest.TestCase):

    def test_master_is_not_a_bare_substring_ban(self):
        self.assertNotIn("master", PAIR.get("forbid") or [],
                         "запрет на подстроку master валит объяснение "
                         "переименования")

    def test_origin_main_is_still_forbidden(self):
        self.assertIn("origin/main", PAIR.get("forbid") or [],
                      "запрет на origin/main как рабочую базу снят зря")


class TestTheCorrectAnswerIsNotRejected(unittest.TestCase):

    @settings(deadline=None)
    @given(rec=st.sampled_from(FROM_TRUNK), note=st.sampled_from(RENAME_NOTE))
    def test_naming_the_old_branch_as_context_passes(self, rec, note):
        answer = "%s. Контекст: %s." % (rec, note)
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertEqual([], verdict["false_hits"],
                         "верный ответ завален запретом: %r" % answer)
        self.assertTrue(verdict["ok"], "верный ответ не прошёл: %r" % answer)


class TestThePairStillNeedsTheFact(unittest.TestCase):

    @settings(deadline=None)
    @given(base=st.sampled_from(DEFAULT_BASE))
    def test_an_answer_without_trunk_does_not_pass(self, base):
        verdict = evaluate.judge(PAIR, base + ".", base + ".", None)
        self.assertFalse(verdict["ok"],
                         "ответ без факта (нет trunk) прошёл: %r" % base)

    def test_calling_origin_main_the_working_base_is_a_false_hit(self):
        answer = "Заводи ветку от origin/main и вливай обратно в неё же."
        verdict = evaluate.judge(PAIR, answer, answer, None)
        self.assertIn("origin/main", verdict["false_hits"])
        self.assertFalse(verdict["ok"])


if __name__ == "__main__":
    unittest.main()
