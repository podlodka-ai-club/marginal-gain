#!/usr/bin/env python3
"""Контракт пары и опорный набор, который никто не трогает.

Запуск: python3 -m pytest tests/test_pairs_contract.py -q

Набор растёт по одной паре за раз (см. `eval/pairs.py`), и у роста два риска:

1. Новая пара собрана не по форме — `validate` должен ловить это до прогона,
   а не после часа ожидания живого стенда. До сих пор эту функцию не проверял
   ни один тест: она вызывается изнутри `pairs.load`, но что именно ловится, а
   что проходит — не зафиксировано.
2. Опорные четыре пары (`завтрак`, `город`, `забор`, `макбук`) правятся заодно
   с новыми — правило задачи прямо это запрещает, а json-файл ничего не
   заметит: он одинаково молча принимает и новую пару, и правку старой.

Свойства:

1. Пара с любым из перечисленных изъянов — не проходит: пустой id, aim не из
   `apply`/`avoid`, `tell` не список, пустой `tell` у `apply`-пары, ход без
   `say`, задача без `say`, ни `expect`, ни `forbid`.
2. Пара без изъянов проходит и возвращается тем же значением, что дано.
3. Пустой `tell` у `avoid`-пары — не изъян: отрицательный случай не обязан
   ничего сообщать в сессии 1.
4. Опорные четыре пары в `eval-pairs-example.json` побайтово равны снимку,
   снятому на прогоне с долей 3 из 4 (коммит `d03cdbd`).

Мутации, на которых проверки обязаны краснеть:
  * `validate` перестаёт проверять id/aim/tell/task/expect-forbid → TestValidPairsPass закрасится не тем (см. TestEachDefectIsCaught)
  * пустой `tell` запрещён и `avoid`-паре тоже                    → TestEmptyTellIsOnlyOkForAvoid
  * кто-то правит опорную пару вместе с новой                     → TestBaselinePairsAreFrozen
"""
import copy
import json
import os
import unittest
from pathlib import Path

from hypothesis import assume, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

FAST = settings(deadline=None, max_examples=100)

# --- строительные блоки валидной пары ---------------------------------------

WORD = st.text(min_size=1, max_size=12).filter(lambda s: s.strip())
ID = WORD
SAY = st.text(min_size=1, max_size=40).filter(lambda s: s.strip())
PLACE = st.text(max_size=15)


def a_turn():
    return st.builds(dict, say=SAY, place=PLACE)


NONEMPTY_TELL = st.lists(a_turn(), min_size=1, max_size=4)
TASK = a_turn()
NAMES = st.lists(WORD, min_size=1, max_size=3)


def a_valid_pair(aim):
    tell = NONEMPTY_TELL if aim == "apply" else st.one_of(st.just([]), NONEMPTY_TELL)
    return st.builds(
        dict,
        id=ID,
        aim=st.just(aim),
        tell=tell,
        task=TASK,
        expect=st.one_of(NAMES, st.just([])),
        forbid=st.one_of(NAMES, st.just([])),
    ).filter(lambda p: p["expect"] or p["forbid"])


VALID_PAIR = st.sampled_from(pairs.AIMS).flatmap(a_valid_pair)


class TestValidPairsPass(unittest.TestCase):
    """Пара без изъянов проходит и не теряется по дороге."""

    @given(pair=VALID_PAIR)
    @FAST
    def test_a_well_formed_pair_comes_back_unchanged(self, pair):
        self.assertEqual(pair, pairs.validate(dict(pair)))


class TestEachDefectIsCaught(unittest.TestCase):
    """Каждый изъян из докстринга `validate` останавливает пару, а не проходит мимо."""

    @given(pair=st.sampled_from(pairs.AIMS).flatmap(a_valid_pair), empty_id=st.sampled_from(("", None)))
    @FAST
    def test_no_id_is_rejected(self, pair, empty_id):
        broken = dict(pair, id=empty_id)
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=st.sampled_from(pairs.AIMS).flatmap(a_valid_pair),
           bad_aim=st.text(max_size=10).filter(lambda s: s not in pairs.AIMS))
    @FAST
    def test_an_aim_outside_apply_avoid_is_rejected(self, pair, bad_aim):
        broken = dict(pair, aim=bad_aim)
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=a_valid_pair("apply"),
           not_a_list=st.one_of(st.text(max_size=10), st.integers(), st.none()))
    @FAST
    def test_tell_that_is_not_a_list_is_rejected(self, pair, not_a_list):
        broken = dict(pair, tell=not_a_list)
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=a_valid_pair("apply"))
    @FAST
    def test_an_empty_tell_is_rejected_for_an_apply_pair(self, pair):
        """apply без сессии 1 — «примени то, чего не говорили», непроходимо."""
        broken = dict(pair, tell=[])
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=st.sampled_from(pairs.AIMS).flatmap(a_valid_pair),
           blank=st.sampled_from(("", "   ", None)))
    @FAST
    def test_a_turn_without_a_line_is_rejected(self, pair, blank):
        assume(pair["tell"])
        broken = copy.deepcopy(pair)
        broken["tell"][0]["say"] = blank
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=st.sampled_from(pairs.AIMS).flatmap(a_valid_pair))
    @FAST
    def test_a_task_without_a_shape_is_rejected(self, pair):
        broken = dict(pair, task={"place": "где-то"})
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(pair=st.sampled_from(pairs.AIMS).flatmap(a_valid_pair))
    @FAST
    def test_neither_expect_nor_forbid_is_rejected(self, pair):
        broken = dict(pair, expect=[], forbid=[])
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)


class TestMattersFieldIsOptional(unittest.TestCase):
    """`matters` — условие релевантности факта. Необязательное, старые пары живы.

    Мутации, на которых обязана покраснеть:
      * `validate` требует `matters` у каждой пары  → test_a_pair_without_matters_still_passes
      * `validate` принимает пустую строку/не-строку → test_a_blank_matters_is_rejected
    """

    @given(pair=VALID_PAIR)
    @FAST
    def test_a_pair_without_matters_still_passes(self, pair):
        self.assertNotIn("matters", pair)
        self.assertEqual(pair, pairs.validate(dict(pair)))

    @given(pair=VALID_PAIR,
           note=st.text(min_size=1, max_size=40).filter(lambda s: s.strip()))
    @FAST
    def test_a_pair_with_matters_passes_and_keeps_it(self, pair, note):
        with_note = dict(pair, matters=note)
        self.assertEqual(with_note, pairs.validate(with_note))

    @given(pair=VALID_PAIR, blank=st.sampled_from(("", "   ", 7, [])))
    @FAST
    def test_a_blank_matters_is_rejected(self, pair, blank):
        broken = dict(pair, matters=blank)
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)


class TestEmptyTellIsOnlyOkForAvoid(unittest.TestCase):
    """Пустая сессия 1 — легальный случай ровно у отрицательной пары."""

    @given(pair=a_valid_pair("avoid"))
    @FAST
    def test_avoid_accepts_an_empty_tell(self, pair):
        empty = dict(pair, tell=[])
        self.assertEqual([], pairs.validate(empty)["tell"])


class TestBaselinePairsAreFrozen(unittest.TestCase):
    """Опорные четыре пары не меняются, пока набор растёт вокруг них.

    Снимок взят на коммите `d03cdbd` (доля руки с памятью 3 из 4). Если этот
    тест покраснел, кто-то поправил формулировку, ожидание или запрет
    опорной пары вместо того, чтобы добавить новую рядом.
    """

    FROZEN = {
        "завтрак": {
            "id": "завтрак",
            "aim": "apply",
            "tell": [
                {
                    "say": "Запомни: на завтрак у нас дома едят только овсянку, других каш не берём.",
                    "place": "кухня",
                    "touched": ["меню.txt"],
                },
                {
                    "say": "Овсянку бери крупную, из хлопьев быстрого приготовления никто не ест.",
                    "place": "кухня",
                    "touched": ["меню.txt"],
                },
            ],
            "task": {"say": "Составь список покупок на неделю: завтраки, перекусы, ужины."},
            "expect": ["овсян"],
            "forbid": [],
        },
        "город": {
            "id": "город",
            "aim": "apply",
            "tell": [
                {"say": "Я живу в Казани, работаю смотрителем в музее.", "place": "письма"},
                {"say": "Из Казани почти не выезжаю, все встречи назначаю здесь же.", "place": "письма"},
            ],
            "task": {"say": "Напиши шапку делового письма от меня незнакомому адресату."},
            "expect": ["Казан"],
            "forbid": [],
        },
        "забор": {
            "id": "забор",
            "aim": "avoid",
            "tell": [
                {
                    "say": "На прошлой неделе красил забор на даче в зелёный, ушло четыре банки.",
                    "place": "дача",
                    "touched": ["смета.txt"],
                },
                {
                    "say": "Краску для забора брал матовую, глянцевая на солнце выгорает.",
                    "place": "дача",
                    "touched": ["смета.txt"],
                },
            ],
            "task": {
                "say": "Посоветуй, что приготовить на ужин из того, что обычно есть дома.",
                "place": "дача",
            },
            "expect": [],
            "forbid": ["забор", "краск"],
        },
        "макбук": {
            "id": "макбук",
            "aim": "apply",
            "tell": [
                {
                    "say": "Вчера купил MacBook Pro M5, 16 гигабайт памяти, 14 дюймов. Это мой рабочий ноутбук.",
                    "place": "работа",
                },
                {
                    "say": "Другого рабочего ноутбука у меня нет, всё веду с этого MacBook.",
                    "place": "работа",
                },
            ],
            "task": {"say": "Дай мне ссылку на документацию для моего Mac.", "place": "работа"},
            "expect": ["M5"],
            "forbid": [],
        },
    }

    def current(self):
        _, items = pairs.load(HOUSEHOLD)
        return {item["id"]: item for item in items}

    def test_every_baseline_pair_is_still_present(self):
        current = self.current()
        missing = [id_ for id_ in self.FROZEN if id_ not in current]
        self.assertEqual([], missing, "опорная пара пропала из набора: %s" % missing)

    def test_every_baseline_pair_is_byte_for_byte_the_same(self):
        current = self.current()
        for id_, frozen in self.FROZEN.items():
            self.assertEqual(frozen, current[id_], "опорная пара %r изменилась" % id_)


if __name__ == "__main__":
    unittest.main()
