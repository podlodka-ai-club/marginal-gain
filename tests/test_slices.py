#!/usr/bin/env python3
"""Срезы по обстановке: все факты проекта, все факты вторника, вторника и проекта.

Запуск: python3 -m unittest tests.test_slices -v

До сих пор так спросить было нельзя вовсе: у факта в полях один `project`, а
ветка, каталог и время лежали у эпизода и связью не читались. Срез собирается
из осей той же формы, какой считается уместность, — иначе «факты вторника» и
«уместно во вторник» разошлись бы молча.

Срезы **комбинируются**, а не выбираются из списка готовых: ось это ключ формы,
их набор произволен, и AND между ними считается в одной обстановке, а не по
разным. Отсюда главное свойство: срез по двум осям вложен в пересечение срезов
по каждой из них поодиночке, и добавление оси никогда не расширяет ответ.

Свойствами, а не примерами: важно не «вот здесь пришли два факта», а
«ни одна ось не выдумывается», «порядок осей не значит ничего», «в срезе нет
записей, которых нет в базе».
"""
import contextlib, itertools, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import context, models
from pipeline import understand
from storage import local, port

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow])

CWD_ROOT = "/home/person/dev"
BRANCH = "slices"

# Даты выписаны вместе с днём недели нарочно: справочник дней у проверки свой,
# иначе «срез по вторнику» сверялся бы с тем же кодом, который его и считает.
DAYS = {"2026-08-24": "monday", "2026-08-25": "tuesday",
        "2026-08-26": "wednesday", "2026-08-27": "thursday"}

PROJECTS = ["marginal-gain", "job-hunt", "ru-side"]
NAMES = ["db.py", "port.py", "run.py"]

SHAPES = st.lists(
    st.tuples(st.sampled_from(PROJECTS),
              st.sampled_from(sorted(DAYS)),
              st.sampled_from([9, 14, 21]),
              st.sampled_from(NAMES)),
    min_size=2, max_size=5)


def rows(session, project, name, day, hour):
    cwd = "%s/%s" % (CWD_ROOT, project)
    stamp = "%sT%02d:00:00Z" % (day, hour)
    head = {"sessionId": session, "timestamp": stamp, "cwd": cwd,
            "gitBranch": BRANCH}
    return [dict(head, type="user", message={"content": "Посмотри, что там с базой"}),
            dict(head, type="assistant", message={"content": [
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "%s/%s" % (cwd, name)}},
                {"type": "text", "text": "Готово."}]})]


def archive(root, shape):
    paths = []
    for number, (project, day, hour, name) in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows("разговор-%d" % number, project, name, day, hour):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        paths.append(path)
    return paths


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()


def fill(files):
    understand.digest(files, door=port.door(), dry=False)


def keys(found):
    return {models.Fact(fact_type=r["fact_type"], subject=r["subject"],
                        scope=r["scope"]).identity() for r in found}


def axes_of(shape_item):
    project, day, hour, _name = shape_item
    return {"project": project, "day_of_week": DAYS[day],
            "git_branch": BRANCH, "hour_of_day": hour,
            "working_directory": "%s/%s" % (CWD_ROOT, project)}


class TestASliceIsBuiltFromAxes(unittest.TestCase):
    """Ось — ключ общей формы. Набор осей произволен, а не выбирается из списка."""

    @SLOW
    @given(shape=SHAPES, axis=st.sampled_from(context.AXES))
    def test_every_fact_of_a_slice_really_has_that_axis(self, shape, axis):
        """Срез не выдумывает: у каждого факта есть обстановка с этим значением."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            want = axes_of(shape[0])[axis]
            found = door.slice({axis: want})
            self.assertTrue(found, "срез пуст — проверять нечего")
            got = door.contexts(sorted(keys(found)))
            for key in keys(found):
                values = [one[axis] for one in got.get(key, [])]
                self.assertIn(context.of({axis: want})[axis], values,
                              "факт %s попал в срез без такой обстановки" % key)

    @SLOW
    @given(shape=SHAPES)
    def test_a_slice_without_axes_is_the_whole_base(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            fill(archive(tmp, shape))
            door = port.door()
            everything = door.slice({})
            self.assertTrue(everything)
            for one in ({"project": "нет-такого"}, {"day_of_week": "sunday"}):
                self.assertLessEqual(keys(door.slice(one)), keys(everything))

    @SLOW
    @given(shape=SHAPES)
    def test_two_axes_never_reach_past_each_of_them(self, shape):
        """Срез по двум осям вложен в пересечение срезов по каждой.

        Вложен, а не равен: AND считается в одной обстановке. Факт, который во
        вторник видели в одном проекте, а в среду в другом, попадёт в оба
        одиночных среза и не попадёт в совместный — и это верно.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            axes = axes_of(shape[0])
            for first, second in itertools.combinations(sorted(axes), 2):
                both = keys(door.slice({first: axes[first], second: axes[second]}))
                alone = keys(door.slice({first: axes[first]})) \
                    & keys(door.slice({second: axes[second]}))
                self.assertLessEqual(both, alone,
                                     "срез по %s и %s шире пересечения"
                                     % (first, second))

    @SLOW
    @given(shape=SHAPES)
    def test_adding_an_axis_never_widens_the_slice(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            axes = axes_of(shape[0])
            for name in sorted(axes):
                wide = keys(door.slice({"project": axes["project"]}))
                narrow = keys(door.slice({"project": axes["project"],
                                          name: axes[name]}))
                self.assertLessEqual(narrow, wide)

    @SLOW
    @given(shape=SHAPES)
    def test_the_order_of_axes_means_nothing(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            axes = axes_of(shape[0])
            straight = {"project": axes["project"], "day_of_week": axes["day_of_week"]}
            reversed_ = {"day_of_week": axes["day_of_week"], "project": axes["project"]}
            self.assertEqual(keys(door.slice(straight)), keys(door.slice(reversed_)))

    @SLOW
    @given(shape=SHAPES)
    def test_a_slice_invents_no_facts(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            known = keys(door.slice({}))
            axes = axes_of(shape[0])
            for name in sorted(axes):
                self.assertLessEqual(keys(door.slice({name: axes[name]})), known)

    def test_an_unknown_axis_is_an_error_and_not_the_whole_base(self):
        """Опечатка в имени оси не должна молча возвращать всё подряд."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("demo", "2026-08-24", 9, "db.py")]))
            with self.assertRaises(ValueError):
                port.door().slice({"проект": "demo"})


class TestTheSliceAndTheFitReadOneContext(unittest.TestCase):
    """Срез и уместность считаются по одной и той же обстановке факта.

    Разойдись они — «факты вторника» и «уместно во вторник» отвечали бы про
    разное, и оба остались бы правы поодиночке.
    """

    @SLOW
    @given(shape=SHAPES, axis=st.sampled_from(context.AXES))
    def test_being_in_a_slice_equals_having_that_context(self, shape, axis):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            want = context.of(axes_of(shape[0]))[axis]
            inside = keys(door.slice({axis: want}))
            everything = sorted(keys(door.slice({})))
            got = door.contexts(everything)
            for key in everything:
                has = any(one[axis] == want for one in got.get(key, []))
                self.assertEqual(key in inside, has,
                                 "срез и обстановка расходятся на %s" % key)

    @SLOW
    @given(shape=SHAPES)
    def test_a_full_match_of_a_context_is_a_full_fit(self, shape):
        """Обстановка факта, поданная как обстановка хода, даёт уместность 1."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            everything = sorted(keys(door.slice({})))
            got = door.contexts(everything)
            self.assertTrue(any(got.values()), "связей нет, проверять нечего")
            for situations in got.values():
                for one in situations:
                    self.assertEqual(context.fit(one, one), 1.0)
                    self.assertEqual(context.best(situations, one), 1.0)

    @SLOW
    @given(shape=SHAPES)
    def test_contexts_are_asked_by_key_and_answered_by_key(self, shape):
        """Про ключ, которого нет, отвечают пустотой, а не чужой обстановкой."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, shape))
            door = port.door()
            self.assertEqual(door.contexts([]), {})
            got = door.contexts(["project_state|нет-такого|project"])
            self.assertEqual(got.get("project_state|нет-такого|project", []), [])


class TestTheDoorWithoutSlicesStaysQuiet(unittest.TestCase):
    """Срез умеет не всякий путь наружу: у сети такой выборки нет."""

    def test_a_door_that_cannot_slice_says_so_plainly(self):
        class Blind:
            def read(self, query, mode="single"):
                return ""

        deaf = port.StructuredDoor(Blind(), "blind")
        with self.assertRaises(AttributeError):
            deaf.slice({"project": "demo"})
        with self.assertRaises(AttributeError):
            deaf.contexts(["a|b|project"])


if __name__ == "__main__":
    unittest.main()
