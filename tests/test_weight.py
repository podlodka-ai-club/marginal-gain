#!/usr/bin/env python3
"""Вес факта растёт от обращений, а не от объёма архива.

Запуск: python3 -m unittest tests.test_weight -v

До сих пор вес считался одним вопросом: сколько раз факт встретился в архиве.
Это частота записи, а не частота пользы. Лента обращений (ADR 0010) даёт второй
источник — что показали и чем это кончилось, — и вес считается по обоим.

**Правила, записанные до кода.**

1. Показ и польза — два счётчика, не один. Подсказка сама решает, что показать;
   считай показ пользой — и она начнёт поднимать вес тому, что сама и выбрала.
2. Показ двигает вес слабее пользы настолько, что показами его не накрутить:
   факт, который показывали десять раз впустую, весит меньше факта, который
   показывали дважды и оба раза с толком.
3. Ответа нет — вес не двигается. `unknown` это молчание агента, а не отказ.
4. Пустая лента не обнуляет факт. Нет обращений — остаётся мера архива
   (ADR 0002) в точности, до знака.
5. Прямое указание человека проходит порог с первого раза, не дожидаясь
   повторов: указание — утверждение, а не гипотеза. Это пол, а не потолок —
   повтор и польза поднимают выше.
6. Веса меры ADR 0002 этой работой не двигаются. Подгонка исчерпана дважды
   (research-4, research-7); здесь добавляется источник сигнала, а не
   перебираются старые коэффициенты.
"""
import os
import unittest
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, marks, measure
from pipeline import suggest, understand

SLOW = settings(deadline=None, max_examples=50,
                suppress_health_check=[HealthCheck.too_slow])

TIMES = st.sampled_from(["", "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z",
                         "2026-08-20T00:00:00Z", "2026-09-01T00:00:00Z"])

@st.composite
def nodes(draw):
    """Узел факта и время последнего эпизода архива: (rec, newest).

    Пара строится вместе, а не порознь: `newest` — это максимум по всему
    архиву, и узла свежее его не бывает. Разведи их по отдельным стратегиям —
    и свойства проверялись бы на факте из будущего, какого в архиве нет.
    """
    last, newest = sorted([draw(TIMES), draw(TIMES)])
    return ({"n": draw(st.integers(min_value=0, max_value=50)),
             "projects": set(draw(st.lists(st.sampled_from(["a", "b", "c", "d"]),
                                           max_size=4))),
             "last": last}, newest)


NODES = nodes()


@st.composite
def tallies(draw, shown=st.integers(min_value=0, max_value=40),
            helped=None, not_helped=None):
    """Счётчики ленты по одной записи, сходящиеся между собой.

    Ответов не бывает больше показов: ответ про пользу снимается с вставки, а
    вставка — это и есть показ. Строим от показов вниз, чтобы свойства не
    проверялись на арифметически невозможных строках.
    """
    n = draw(shown)
    yes = draw(helped if helped is not None else st.integers(0, n))
    yes = min(yes, n)
    no = draw(not_helped if not_helped is not None else st.integers(0, n - yes))
    no = min(no, n - yes)
    return {"shown": n, "helped": yes, "not_helped": no,
            "unknown": n - yes - no}


# Показывали, и ни разу не помогло: пользы ноль, отказов и молчания сколько угодно.
IDLE = tallies(helped=st.just(0))
# Помогло хотя бы раз и не навредило ни разу.
USED = tallies(shown=st.integers(1, 40), helped=st.integers(1, 40),
               not_helped=st.just(0))
# Та же пара в форме задачи: показывали часто и впустую против «показали дважды,
# помогло оба раза». Уже здесь, потолок показов пройден, — иначе мутация упрётся
# в него и обе стороны сравняются, не сказав ничего.
OFTEN_IDLE = tallies(shown=st.integers(measure.SHOWN_CAP, 40), helped=st.just(0))
RARELY_USED = tallies(shown=st.integers(1, 2), helped=st.integers(1, 2),
                      not_helped=st.just(0))


# Настоящая функция, снятая до подмены: мутация зовёт её, а не себя.
USE_OF = measure.use_of


def as_one_counter(got):
    """Мутация: показ и польза считаются одним счётчиком.

    Каждый показ засчитывается пользой — ровно та петля, ради разрыва которой
    счётчика два. Свойства, которые от этого не краснеют, ничего не охраняют.
    """
    got = dict(got or {})
    got["helped"] = got.get("shown", 0)
    got["not_helped"] = 0
    return USE_OF(got)


class Signal(unittest.TestCase):
    """Надбавка от ленты: что она считает и в каких границах живёт."""

    @SLOW
    @given(got=tallies())
    def test_bounds(self, got):
        """Надбавка не выходит за сумму весов: ни один счётчик не без потолка."""
        low = sum(w for w in measure.USE_WEIGHTS.values() if w < 0)
        high = sum(w for w in measure.USE_WEIGHTS.values() if w > 0)
        self.assertGreaterEqual(measure.use_of(got), low)
        self.assertLessEqual(measure.use_of(got), high)

    def test_empty_history_is_zero(self):
        """Обращений не было — надбавки нет. Ни отсутствия ленты, ни нулей."""
        for empty in (None, {}, {"shown": 0, "helped": 0, "not_helped": 0,
                                 "unknown": 0}):
            self.assertEqual(measure.use_of(empty), 0.0)

    @SLOW
    @given(got=tallies(), extra=st.integers(min_value=1, max_value=20))
    def test_silence_of_the_agent_moves_nothing(self, got, extra):
        """Ответа нет — вес не двигается. `unknown` не отказ и не польза."""
        louder = dict(got, unknown=got["unknown"] + extra,
                      shown=got["shown"] + extra)
        self.assertEqual(measure.use_of(dict(got, unknown=got["unknown"] + extra)),
                         measure.use_of(got))
        self.assertGreaterEqual(measure.use_of(louder), measure.use_of(got))

    @SLOW
    @given(got=tallies(), extra=st.integers(min_value=1, max_value=20))
    def test_use_never_lowers(self, got, extra):
        """Ещё один «помог» вес не роняет."""
        self.assertGreaterEqual(measure.use_of(dict(got, helped=got["helped"] + extra)),
                                measure.use_of(got))

    @SLOW
    @given(got=tallies(), extra=st.integers(min_value=1, max_value=20))
    def test_refusal_never_raises(self, got, extra):
        """Ещё один «не помог» вес не поднимает."""
        self.assertLessEqual(
            measure.use_of(dict(got, not_helped=got["not_helped"] + extra)),
            measure.use_of(got))

    @SLOW
    @given(idle=IDLE, used=USED)
    def test_show_alone_loses_to_use(self, idle, used):
        """Главное свойство: показами вес не накрутить.

        Сколько бы раз факт ни показали, если он ни разу не помог — он ниже
        факта, который помог хотя бы однажды. Верно при любых числах, а не на
        подобранной паре.
        """
        self.assertLess(measure.use_of(idle), measure.use_of(used))

    @SLOW
    @given(idle=OFTEN_IDLE, used=RARELY_USED)
    def test_one_counter_breaks_it(self, idle, used):
        """Та же пара при одном счётчике переворачивается.

        Мутация приложена к боевой функции, а не к копии: свойство выше держит
        именно раздельный счёт, а не удачно подобранные веса.
        """
        self.assertLess(measure.use_of(idle), measure.use_of(used))
        self.assertGreater(as_one_counter(idle), as_one_counter(used))


class Weight(unittest.TestCase):
    """Итоговый вес: мера архива плюс сдвиг от ленты."""

    BASE = {"n": 1, "projects": {"a"}, "last": ""}

    @SLOW
    @given(node=NODES, got=tallies())
    def test_bounds(self, node, got):
        rec, newest = node
        weight = measure.weight_of(dict(rec, use=got), newest)
        self.assertGreaterEqual(weight, 0.0)
        self.assertLessEqual(weight, 1.0)

    @SLOW
    @given(node=NODES)
    def test_no_history_keeps_the_measure(self, node):
        """Факт без единого обращения не обнуляется: остаётся мера ADR 0002."""
        rec, newest = node
        self.assertEqual(measure.weight_of(rec, newest),
                         measure.score_of(rec, newest))
        self.assertEqual(measure.weight_of(dict(rec, use=None), newest),
                         measure.score_of(rec, newest))

    def test_ten_empty_shows_lose_to_two_useful_ones(self):
        """Ожидаемый сигнал задачи, в числах.

        Показан десять раз и ни разу не помог — ниже показанного дважды и
        помогшего оба раза. Оба варианта отказа: агент ответил «нет» и агент
        не ответил вовсе.
        """
        useful = measure.weight_of(
            dict(self.BASE, use={"shown": 2, "helped": 2, "not_helped": 0,
                                 "unknown": 0}), "")
        for idle in ({"shown": 10, "helped": 0, "not_helped": 10, "unknown": 0},
                     {"shown": 10, "helped": 0, "not_helped": 0, "unknown": 10}):
            self.assertLess(measure.weight_of(dict(self.BASE, use=idle), ""),
                            useful)

    def test_one_counter_flips_the_expected_signal(self):
        """Мутация задачи: тот же сигнал при одном счётчике краснеет."""
        idle = {"shown": 10, "helped": 0, "not_helped": 10, "unknown": 0}
        useful = {"shown": 2, "helped": 2, "not_helped": 0, "unknown": 0}
        with mock.patch.object(measure, "use_of", as_one_counter):
            self.assertGreater(measure.weight_of(dict(self.BASE, use=idle), ""),
                               measure.weight_of(dict(self.BASE, use=useful), ""))


class DirectWord(unittest.TestCase):
    """Прямое указание человека: высокий вес сразу, без повторов."""

    ONCE = {"n": 1, "projects": set(), "last": ""}

    def test_said_once_passes_the_threshold(self):
        """Сказано один раз, в одном проекте, давно — и всё равно проходит."""
        self.assertGreaterEqual(measure.weight_of(dict(self.ONCE, told=True), ""),
                                suggest.MIN_SCORE)
        self.assertLess(measure.weight_of(self.ONCE, ""), suggest.MIN_SCORE)

    @SLOW
    @given(node=NODES, got=tallies())
    def test_direct_word_is_a_floor_not_a_ceiling(self, node, got):
        """Указание не опускается ниже пола и не мешает подняться выше."""
        rec, newest = node
        told = measure.weight_of(dict(rec, use=got, told=True), newest)
        plain = measure.weight_of(dict(rec, use=got), newest)
        self.assertGreaterEqual(told, measure.TOLD_FLOOR)
        self.assertGreaterEqual(told, plain)

    @SLOW
    @given(node=NODES, got=IDLE)
    def test_refusals_do_not_sink_a_direct_word(self, node, got):
        """Агент отвечал «не помогло» — указание человека всё равно стоит."""
        rec, newest = node
        self.assertGreaterEqual(
            measure.weight_of(dict(rec, use=got, told=True), newest),
            measure.TOLD_FLOOR)


class Marked(unittest.TestCase):
    """Откуда берётся признак прямого указания."""

    UNIT = {"type": "preference", "subject": "длина ответа",
            "predicate": "человек просит", "value": "отвечать коротко",
            "source": "stated", "confidence": 0.9}
    # Ключ — subject и predicate вместе (см. domain/marks.py:xmd1_unit):
    # разные атрибуты одной темы не должны делить один ключ факта.
    KEY = ("mark", "preference", "длина ответа: человек просит", "global")

    def episode(self, *replies):
        return {"session_id": "s1", "number": 1, "request": "сделай коротко",
                "started_at": "2026-08-28T10:00:00Z",
                "ended_at": "2026-08-28T10:05:00Z",
                "cwd": "/Users/person/dev/marginal-gain", "branch": "b",
                "files": [], "commands": [], "replies": list(replies),
                "errors": []}

    def block(self, *units):
        import json
        return "\n".join([marks.XMD1_BEGIN]
                         + [json.dumps(u, ensure_ascii=False) for u in units]
                         + [marks.XMD1_END])

    def test_stated_is_a_direct_word(self):
        self.assertEqual(marks.told_of(self.episode(self.block(self.UNIT))),
                         [self.KEY])

    def test_observed_is_not(self):
        """Наблюдение в работе — не указание. Иначе указанием станет всё."""
        seen = dict(self.UNIT, source="observed")
        facts, _ = marks.facts_of(self.episode(self.block(seen)))
        self.assertEqual(len(facts), 1, "наблюдение пишется как факт")
        self.assertEqual(marks.told_of(self.episode(self.block(seen))), [])

    def test_templates_tell_nothing(self):
        """Факт, вырезанный шаблоном, указанием не бывает: источника у него нет."""
        ep = self.episode("просто ответ")
        ep["files"] = ["/Users/person/dev/marginal-gain/a.py"]
        self.assertEqual(marks.told_of(ep), [])


class Wiring(unittest.TestCase):
    """Лента доезжает до веса на боевом пути, а не только в проверке."""

    def episodes(self, reply, ended="2026-08-28T10:05:00Z"):
        return [{"session_id": "s1", "number": 1, "request": "?",
                 "started_at": "2026-08-28T10:00:00Z", "ended_at": ended,
                 "cwd": "/Users/person/dev/marginal-gain", "branch": "b",
                 "files": [], "commands": [], "replies": [reply], "errors": []}]

    def marked(self, source="observed"):
        import json
        unit = {"type": "preference", "subject": "длина ответа",
                "predicate": "человек просит", "value": "отвечать коротко",
                "source": source, "confidence": 0.9}
        return "\n".join([marks.XMD1_BEGIN, json.dumps(unit, ensure_ascii=False),
                          marks.XMD1_END])

    # Ключ — subject и predicate вместе (см. domain/marks.py:xmd1_unit):
    # разные атрибуты одной темы не должны делить один ключ факта.
    KEY = ("mark", "preference", "длина ответа: человек просит", "global")
    IDENTITY = "preference|длина ответа: человек просит|global"

    def weigh(self, reply, use=None):
        with mock.patch.object(understand, "episodes_from_file",
                               lambda path: self.episodes(reply)):
            return understand.weigh(["один.jsonl"], use=use or {})

    def test_identity_matches_the_ledger_key(self):
        """Ключ узла и ключ ленты — один и тот же факт. Иначе счёт не сойдётся."""
        fact = ("preference", "длина ответа: человек просит", "global",
                "человек просит коротко")
        self.assertEqual(understand.identity_of(fact), self.IDENTITY)

    def test_weigh_joins_the_ledger(self):
        got = {"shown": 4, "helped": 3, "not_helped": 1, "unknown": 0}
        seen = self.weigh(self.marked(), use={self.IDENTITY: got})
        self.assertEqual(seen[self.KEY]["use"], got)

    def test_weigh_counts_a_node_once(self):
        """Узел встретился дважды — счётчики ленты не удваиваются.

        Лента считает показы сама. Сложи её счётчики ещё раз на каждое вхождение
        в архив — и вес поехал бы от частоты записи, ровно от которой уходим.
        """
        both = self.episodes(self.marked()) + self.episodes(self.marked())
        got = {"shown": 4, "helped": 3, "not_helped": 1, "unknown": 0}
        with mock.patch.object(understand, "episodes_from_file",
                               lambda path: both):
            seen = understand.weigh(["один.jsonl"], use={self.IDENTITY: got})
        self.assertEqual(seen[self.KEY]["n"], 2)
        self.assertEqual(seen[self.KEY]["use"], got)

    def test_weigh_marks_the_direct_word(self):
        self.assertTrue(self.weigh(self.marked("stated"))[self.KEY]["told"])
        self.assertFalse(self.weigh(self.marked("observed"))[self.KEY]["told"])

    def test_weigh_reads_the_live_ledger_by_default(self):
        """Счётчики не передали — берём ленту, а не пустоту."""
        got = {self.IDENTITY: {"shown": 1, "helped": 1, "not_helped": 0,
                               "unknown": 0}}
        with mock.patch.object(ledger, "tally", lambda rows_, **kw: got), \
             mock.patch.object(understand, "episodes_from_file",
                               lambda path: self.episodes(self.marked())):
            seen = understand.weigh(["один.jsonl"])
        self.assertEqual(seen[self.KEY]["use"], got[self.IDENTITY])

    def test_the_written_score_is_the_weight(self):
        """Запись эпизода берёт вес, а не голую меру ADR 0002.

        Проверка не «оценка есть», а «оценка приехала из этой функции»: подменяем
        её и смотрим, поехало ли число в записанном факте.
        """
        rec = {"n": 1, "projects": {"a"}, "last": "", "told": False,
               "use": {"shown": 2, "helped": 2, "not_helped": 0, "unknown": 0}}
        written = []
        door = mock.Mock(spec=[])
        with mock.patch.object(understand, "deliver",
                               lambda ep, episode, chosen, door: written.extend(chosen)), \
             mock.patch.object(understand, "weight_of", lambda r, n: 0.77):
            understand.one_episode(self.episodes(self.marked())[0], 0,
                                   {"episodes": 0, "facts": 0, "lost": 0,
                                    "skipped": 0},
                                   {self.KEY: rec}, "2026-08-28T10:05:00Z",
                                   False, 0.0, False, door)
        self.assertTrue(written, "факт не дошёл до записи")
        self.assertIn("Оценка уверенности: 0.77.", written[0][1])


class Untouched(unittest.TestCase):
    """Мера ADR 0002 этой работой не двигается."""

    def test_adr_weights_are_the_same(self):
        self.assertEqual(measure.WEIGHTS,
                         {"repeat": 0.5, "spread": 0.2, "fresh": 0.3})
        self.assertEqual((measure.REPEAT_CAP, measure.SPREAD_CAP,
                          measure.FRESH_DAYS), (10, 3, 30))

    def test_score_of_knows_nothing_about_the_ledger(self):
        """Лента в меру не подмешана: с ней и без неё мера одна."""
        rec = {"n": 3, "projects": {"a"}, "last": ""}
        loud = dict(rec, told=True,
                    use={"shown": 9, "helped": 9, "not_helped": 0, "unknown": 0})
        self.assertEqual(measure.score_of(loud, ""), measure.score_of(rec, ""))
        self.assertEqual(measure.score_of(rec, ""),
                         round(0.5 * 0.3 + 0.2 * (1 / 3.0), 3))


if __name__ == "__main__":
    unittest.main()
