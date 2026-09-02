#!/usr/bin/env python3
"""Замер называет момент, на который считает сроки, и держит его между прогонами.

Запуск: python3 -m unittest tests.test_measure_moment -v

Набор носит версию, и загрузчик отказывается читать чужую (ADR 0006). Состояние
базы не носило ничего: заработало забывание, шестьдесят семь фактов уехали в
отложенное, и цифра замера упала на шесть пунктов без единой правки кода. Через
неделю она дала бы третье число, и любая эвристика, померенная так, получила бы
чужую разницу — свою от хода времени не отличить.

Правило одно и всё целиком здесь:

  момент     замер называет момент, на который считаются сроки. Не «сейчас», а
             значение: оно лежит в конверте набора и не меняется само.
  выборка    на этот момент видно ровно то, что было живо тогда, — где бы оно
             физически ни лежало. Переклад двигает строку между таблицами, но
             не трогает срок на ней, и восстановить состав по сроку можно
             всегда.
  вид        вернувшееся из отложенного идёт в выдачу фактом, а не отдельным
             видом: иначе квота вида и разбор выдачи меняются от того, гоняли
             ли переклад, — то есть цифра снова зависит от часов.

Свойства заданы не примерами: важно не «на этой базе совпало», а «совпадает при
любом расписании переклада и любом наборе сроков».

Мутации, на которых проверки обязаны краснеть:
  * не фильтровать выборку моментом                → TestTheMomentPinsTheSelection
  * не поднимать отложенное, живое на момент       → TestSweepingIsInvisibleToTheMeasure
  * отдавать поднятое видом LapsedFact             → TestWhatComesBackIsAFact
  * считать момент от «сейчас», а не из набора     → TestTheSetCarriesTheMoment
  * не закреплять момент на прогон                 → TestThePinReachesTheReadPath
  * обойти моментом хоть одну выборку фактов       → TestEveryFactReadObeysTheMoment
  * вернуть путь к набору внутрь пакета eval       → TestTheStandardCommandFindsItsSet
"""
import contextlib, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import lifespan, models
from eval import evaluate, goldenset, matrix
from infra import config
from pipeline import forget
from storage import db, local, port

HERE = Path(__file__).resolve().parent.parent

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

QUERY = "маркер"

# Сроки фактов, днями от T0. Держим их врозь и в пределах квоты вида: свойства
# здесь про состав выдачи, а не про то, как она обрезается потолком.
OFFSETS = st.lists(st.integers(min_value=-60, max_value=60), min_size=1,
                   max_size=5, unique=True)

# Момент, на который считают сроки, и момент, когда прогнали переклад.
DAY = st.integers(min_value=-70, max_value=70)


@contextlib.contextmanager
def store(tmp, as_of=None):
    """Локальная база и момент замера — оба на время одной проверки."""
    base = Path(tmp) / "memory.db"
    local.close()
    env = {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
           "XMEM_LOCAL_PATH": str(base), "XMEM_AS_OF": as_of or ""}
    with mock.patch.dict(os.environ, env), \
         mock.patch.dict(os.environ, {"XMEM_STATE_DIR": str(Path(tmp) / "state")}):
        try:
            yield base
        finally:
            local.close()


def stamp(days):
    return lifespan.stamp(T0 + timedelta(days=days))


def fact(i, days):
    """Факт со сроком на T0 плюс столько-то дней. Тема у каждого своя."""
    return models.Fact(fact_type="project_state", subject="файл%d.py" % i,
                       scope="project", content="%s про файл %d" % (QUERY, i),
                       project="demo", updated_at=lifespan.stamp(T0),
                       valid_until=stamp(days))


def fill(offsets):
    port.door().write_objects([fact(i, d) for i, d in enumerate(offsets)])


def alive(offsets, at):
    """Кто был жив на момент. Срок, равный моменту, ещё не вышел."""
    return sorted("файл%d.py" % i for i, d in enumerate(offsets) if d >= at)


def sweep(at):
    return forget.sweep(now=stamp(at))["moved"]


def subjects(rows):
    return sorted(r["subject"] for r in rows if r.get("subject"))


def seen(as_of=None):
    """Что видит замер: чтение той же дверью, какой ходит подсказка."""
    import json
    answer = port.door().read(QUERY, mode="raw")
    rows = json.loads(answer) if answer else []
    return rows


# --- 1. Момент задаёт выборку ----------------------------------------------


class TestTheMomentPinsTheSelection(unittest.TestCase):
    """На названный момент видно ровно то, что было живо тогда."""

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_the_selection_is_what_was_alive_then(self, offsets, at):
        """Выборка на момент — это срок на записи, а не таблица, где она лежит.

        Мутация: перестать фильтровать моментом — свойство краснеет на первом
        же наборе, где хоть один срок вышел раньше момента.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            got = local.repository().search(QUERY, limit=10, as_of=stamp(at))
            self.assertEqual(subjects(got), alive(offsets, at))

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_without_a_moment_nothing_changes(self, offsets, at):
        """Без момента выдача прежняя: всё живое из таблицы фактов, и только.

        Момент — добавка к чтению, а не новая его форма. Прежние вызовы,
        которых в конвейере большинство, обязаны ходить как ходили.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            sweep(at)
            got = local.repository().search(QUERY, limit=10)
            self.assertEqual(subjects(got), alive(offsets, at),
                             "без момента выдача должна совпасть с таблицей фактов")

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_the_moment_ignores_the_clock(self, offsets, at):
        """Часы на выборку не влияют: считается срок против момента, не против «сейчас».

        Сроки здесь заданы от 2026 года, а прогон идёт когда угодно. Совпадение
        с `alive` и означает, что «сейчас» в расчёт не входит.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            first = subjects(local.repository().search(QUERY, limit=10,
                                                       as_of=stamp(at)))
            self.assertEqual(first, alive(offsets, at))


# --- 2. Переклад замеру не виден -------------------------------------------


class TestSweepingIsInvisibleToTheMeasure(unittest.TestCase):
    """Цифра не зависит от того, когда прогнали забывание и прогнали ли вовсе."""

    @SLOW
    @given(offsets=OFFSETS, at=DAY, sweeps=st.lists(DAY, max_size=3))
    def test_any_sweep_schedule_gives_the_same_answer(self, offsets, at, sweeps):
        """Главное свойство. Переклад двигает строку, но не срок на ней.

        Мутация: не поднимать отложенное, живое на момент, — свойство краснеет
        на первом же перекладе, случившемся позже момента.
        """
        want = None
        for schedule in ([], sweeps):
            with tempfile.TemporaryDirectory() as tmp, store(tmp):
                fill(offsets)
                for day in schedule:
                    sweep(day)
                got = subjects(local.repository().search(QUERY, limit=10,
                                                         as_of=stamp(at)))
                if want is None:
                    want = got
                self.assertEqual(got, want,
                                 "расписание переклада %s сдвинуло выдачу" % schedule)
        self.assertEqual(want, alive(offsets, at))

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_a_month_of_running_does_not_move_the_number(self, offsets, at):
        """Мутация из задачи: сдвинуть прогон на месяц вперёд, итог тот же.

        Месяц работы — это месяц перекладов на конце хода. Дверью ходим той же,
        какой ходит замер: момент читается из окружения, а не передаётся руками.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp, as_of=stamp(at)):
            fill(offsets)
            before = subjects(seen())
            for day in range(at, at + 31, 10):
                sweep(day)
            self.assertEqual(subjects(seen()), before,
                             "месяц перекладов сдвинул выдачу замера")

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_the_deep_read_still_sees_everything(self, offsets, at):
        """Глубокое чтение осталось глубоким: отложенное достаётся нарочно."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            sweep(max(offsets) + 1)
            got = subjects(local.repository().search(QUERY, limit=10, deep=True))
            self.assertEqual(got, sorted("файл%d.py" % i for i in range(len(offsets))))


class TestEveryFactReadObeysTheMoment(unittest.TestCase):
    """Факты читает не только поиск. Обойди момент хоть одну выборку — цифра поедет.

    Ровно на этом первый заход и попался: поиск чинили, а соседей по графу
    брали прямо из таблицы фактов, и после переклада она пустела. Свойства на
    поиске были зелёные, а итог на живой базе всё равно гулял на единицу.
    """

    @SLOW
    @given(offsets=OFFSETS, at=DAY, after=st.integers(min_value=1, max_value=60))
    def test_a_neighbour_survives_the_sweep(self, offsets, at, after):
        """Сосед по связи, живой на момент, приходит и после переклада.

        Мутация: убрать момент из `neighbours` — свойство краснеет, потому что
        связь переживает строку, а строка уезжает в отложенное.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            keys = [fact(i, d).identity() for i, d in enumerate(offsets)]
            if len(keys) < 2:
                return
            port.door().write_objects([models.Association(
                source_key=keys[0], target_key=keys[1], cue="same_episode", weight=1.0,
                observed_at=lifespan.stamp(T0))])
            want = [r["subject"] for r, _ in
                    local.repository().neighbours([keys[0]], as_of=stamp(at))]
            sweep(at + after)
            got = [r["subject"] for r, _ in
                   local.repository().neighbours([keys[0]], as_of=stamp(at))]
            self.assertEqual(got, want)
            self.assertEqual(bool(got), "файл1.py" in alive(offsets, at))

    @SLOW
    @given(offsets=OFFSETS, at=DAY, after=st.integers(min_value=1, max_value=60))
    def test_the_situation_of_a_fact_survives_the_sweep(self, offsets, at, after):
        """Обстановка считается по строке факта, и строка уезжает вместе с ним.

        Мутация: убрать момент из `contexts` — обстановка пропадает, уместность
        считается не по тому, и порог начинает резать иначе.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            keys = [fact(i, d).identity() for i, d in enumerate(offsets)]
            want = local.repository().contexts(keys, as_of=stamp(at))
            sweep(at + after)
            self.assertEqual(local.repository().contexts(keys, as_of=stamp(at)), want)
            alive_now = alive(offsets, at)
            filled = [k for k, v in want.items() if v]
            self.assertEqual(len(filled), len(alive_now))

    @SLOW
    @given(offsets=OFFSETS, at=DAY, after=st.integers(min_value=1, max_value=60))
    def test_a_slice_survives_the_sweep(self, offsets, at, after):
        """Срез по осям обстановки — та же выборка фактов, тем же правилом."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            want = subjects(local.repository().slice({"project": "demo"},
                                                     as_of=stamp(at)))
            sweep(at + after)
            got = subjects(local.repository().slice({"project": "demo"},
                                                    as_of=stamp(at)))
            self.assertEqual(got, want)
            self.assertEqual(got, alive(offsets, at))


# --- 3. Поднятое выглядит фактом -------------------------------------------


class TestWhatComesBackIsAFact(unittest.TestCase):
    """Вернувшееся из отложенного неотличимо от того, что там не бывало."""

    @SLOW
    @given(offsets=OFFSETS, at=DAY, after=st.integers(min_value=1, max_value=60))
    def test_a_raised_row_is_indistinguishable_from_a_plain_fact(self, offsets, at,
                                                                 after):
        """Не только состав, но и сами записи совпадают до поля.

        Мутация: отдавать поднятое видом LapsedFact или с отметкой переклада —
        свойство краснеет, потому что разбор выдачи и квота вида начинают
        зависеть от того, гоняли ли переклад.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            plain = local.repository().search(QUERY, limit=10, as_of=stamp(at))
            sweep(at + after)
            raised = local.repository().search(QUERY, limit=10, as_of=stamp(at))
            self.assertEqual(raised, plain)
            for row in raised:
                self.assertEqual(row.get("object_type"), "Fact")
                self.assertNotIn("lapsed_at", row)

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_folded_rows_do_not_come_back(self, offsets, at):
        """Свёрнутое моментом не воскресает: его содержание несёт замена.

        Отставное место одно на два повода, и различает их пометка `merged_into`
        (ADR 0013). Момент отменяет только выбытие по сроку.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            repo = local.repository()
            with repo.lock:
                repo.conn.execute(
                    'INSERT OR REPLACE INTO "lapsedfact" '
                    '("fact_type", "subject", "scope", "content", "project", '
                    '"valid_until", "lapsed_at", "merged_into") '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    ("project_state", "свёрнутый.py", "project",
                     "%s про свёрнутый" % QUERY, "demo", stamp(60), stamp(0),
                     "project_state|файл0.py|project"))
                repo.conn.commit()
            got = subjects(repo.search(QUERY, limit=10, as_of=stamp(at)))
            self.assertNotIn("свёрнутый.py", got)


# --- 4. Момент лежит в наборе ----------------------------------------------


class TestTheSetCarriesTheMoment(unittest.TestCase):
    """Момент — свойство набора, а не прогона: иначе он снова «сейчас»."""

    @SLOW
    @given(kind=st.sampled_from(goldenset.KINDS), day=DAY)
    def test_the_envelope_keeps_the_moment(self, kind, day):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            goldenset.dump(path, kind, [], as_of=stamp(day))
            meta, _ = goldenset.load(path, kind)
            self.assertEqual(goldenset.as_of(meta), stamp(day))

    @SLOW
    @given(kind=st.sampled_from(goldenset.KINDS))
    def test_a_set_without_a_moment_falls_back_to_its_build_time(self, kind):
        """Прежние наборы момента не носят. Отказывать им незачем: сборка —
        такой же снимок состояния, и она в конверте уже есть."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "набор.json"
            goldenset.dump(path, kind, [])
            meta, _ = goldenset.load(path, kind)
            del meta["as_of"]
            self.assertEqual(goldenset.as_of(meta), lifespan.stamp(meta["built_at"]))

    def test_the_shipped_set_names_one_moment(self):
        """Тот набор, что лежит в репозитории, момент называет — и один и тот же."""
        meta, _ = goldenset.load(HERE / "eval-cases.json", "cases")
        first = goldenset.as_of(meta)
        self.assertTrue(first, "набор не называет момента")
        self.assertEqual(first, goldenset.as_of(meta))
        self.assertEqual(first, lifespan.stamp(first), "момент не в общем формате")


# --- 5. Штатная команда и отчёт о состоянии --------------------------------


class TestTheStandardCommandFindsItsSet(unittest.TestCase):
    """`python3 -m eval.evaluate` и `python3 -m eval.matrix` — без аргументов."""

    def test_both_entry_points_point_at_the_shipped_set(self):
        """Мутация: вернуть путь внутрь пакета eval — набор перестаёт находиться."""
        for where in (evaluate.CASES, matrix.CASES):
            path = Path(where)
            self.assertTrue(path.is_absolute(), "%s зависит от текущего каталога" % path)
            self.assertTrue(path.exists(), "штатная команда не находит набор: %s" % path)
            meta, cases = goldenset.load(path, "cases")
            self.assertTrue(cases)

    def test_the_set_is_found_from_any_directory(self):
        """Замер зовут из корня, из хука и из планировщика. Каталог не при чём."""
        with tempfile.TemporaryDirectory() as tmp:
            here = os.getcwd()
            try:
                os.chdir(tmp)
                self.assertTrue(Path(evaluate.CASES).exists())
                self.assertTrue(Path(matrix.CASES).exists())
            finally:
                os.chdir(here)


class TestThePinReachesTheReadPath(unittest.TestCase):
    """Момент, названный прогоном, доходит до чтения. Иначе он украшение."""

    @SLOW
    @given(offsets=OFFSETS, at=DAY, after=st.integers(min_value=1, max_value=60))
    def test_pinning_a_moment_changes_what_the_door_returns(self, offsets, at, after):
        """Мутация: перестать закреплять момент — свойство краснеет на первом же
        перекладе, случившемся позже момента: дверь отдаст живое на сейчас."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(offsets)
            evaluate.pin(stamp(at))
            self.assertEqual(lifespan.as_of(), stamp(at),
                             "момент не дошёл до чтения")
            before = subjects(seen())
            sweep(at + after)
            self.assertEqual(subjects(seen()), before)
            self.assertEqual(before, alive(offsets, at))

    @SLOW
    @given(offsets=OFFSETS, at=DAY)
    def test_an_empty_pin_means_now(self, offsets, at):
        """Пусто значит «сейчас»: работа ходит так, и менять ей ничего не надо."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp, as_of=stamp(at)):
            self.assertEqual(evaluate.pin(""), "")
            self.assertIsNone(lifespan.as_of())


class TestTheRunSaysWhatItMeasuredOn(unittest.TestCase):
    """Прогон печатает состояние базы: без него цифра ни с чем не сходится."""

    @SLOW
    @given(offsets=OFFSETS, at=DAY, sweeps=st.lists(DAY, max_size=2))
    def test_the_line_names_facts_lapsed_and_the_moment(self, offsets, at, sweeps):
        with tempfile.TemporaryDirectory() as tmp, store(tmp, as_of=stamp(at)):
            fill(offsets)
            for day in sweeps:
                sweep(day)
            counts = local.repository().counts()
            line = evaluate.state_line(stamp(at))
            self.assertIn(stamp(at), line, "не сказано, на какой момент считались сроки")
            self.assertIn(str(counts["Fact"]), line, "не сказано, сколько фактов")
            self.assertIn(str(counts["LapsedFact"]), line,
                          "не сказано, сколько отложенных")

    @SLOW
    @given(offsets=OFFSETS, at=DAY, sweeps=st.lists(DAY, max_size=2))
    def test_the_line_counts_what_the_measure_will_see(self, offsets, at, sweeps):
        """Живых на момент — то самое число, из которого выйдет цифра замера.

        Оно и обязано стоять в отчёте: «фактов 403, отложенных 67» без него не
        говорит, на чём считали, — а после переклада эти два числа переливаются
        одно в другое, не меняя третьего.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp, as_of=stamp(at)):
            fill(offsets)
            for day in sweeps:
                sweep(day)
            state = local.repository().state(stamp(at))
            self.assertEqual(state["alive"], len(alive(offsets, at)))
            self.assertEqual(state["facts"] + state["lapsed"], len(offsets))
            self.assertIn(str(state["alive"]), evaluate.state_line(stamp(at)))

    @SLOW
    @given(offsets=OFFSETS, at=DAY, sweeps=st.lists(DAY, max_size=3))
    def test_the_number_of_the_living_survives_any_sweep(self, offsets, at, sweeps):
        """То же свойство, что у выдачи, но на отчёте: переклад его не двигает."""
        want = None
        for schedule in ([], sweeps):
            with tempfile.TemporaryDirectory() as tmp, store(tmp):
                fill(offsets)
                for day in schedule:
                    sweep(day)
                got = local.repository().state(stamp(at))["alive"]
                if want is None:
                    want = got
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
