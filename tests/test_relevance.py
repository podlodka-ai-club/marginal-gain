#!/usr/bin/env python3
"""Уместность: обстановка факта против обстановки хода.

Запуск: python3 -m unittest tests.test_relevance -v

Вес отвечает на вопрос «знание это или мусор», уместность — на вопрос «к месту
ли оно сейчас». Без второго множителя память отдаёт частое вместо подходящего:
факт про правку файла в одном проекте всплывает, когда работаешь в другом.

Ключевое ограничение формы: обстановка факта и обстановка хода приводятся
**одной** функцией. Разойдись две — сравнивать было бы нечего, и совпадение
считалось бы молча неверно. Поэтому первый класс проверок берёт одну и ту же
минуту, описанную с двух сторон (payload хука и запись эпизода), и требует
дословного совпадения формы.

Свойствами, а не примерами: важно не «в этом случае факт не всплыл», а
«ухудшение совпадения никогда не поднимает уместность», «уместность лежит в
0…1», «порог применяется к произведению», «факт без обстановки уместен везде».

Мутация, которой проверяется непустота: сделать `context.fit` константой.
Обязан покраснеть класс TestTheSameQuestionInAnotherProject.
"""
import contextlib, json, math, os, tempfile, unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, assume, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import context, models
from domain.context import signals
from pipeline import associate, suggest, understand
from storage import local, port

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])
FAST = settings(deadline=None, max_examples=100)

# Названия дней выписаны здесь заново нарочно. Возьми их из того же модуля,
# каким пользуется проверяемый код, — и проверка «день посчитан верно»
# выродится в сравнение значения с самим собой.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")

PROJECTS = st.sampled_from(["marginal-gain", "job-hunt", "demo", "ru-side"])
BRANCHES = st.sampled_from(["main", "fact-relevance", "graph-on-the-network"])
MOMENTS = st.datetimes(min_value=datetime(2020, 1, 1),
                       max_value=datetime(2030, 1, 1))


def stamp_of(moment):
    return moment.isoformat() + "Z"


def turn_source(project, branch, moment):
    """Одна минута глазами хука: ровно те поля, что приходят в payload."""
    return {"cwd": "/home/person/dev/%s" % project,
            "git_branch": branch,
            "occurred_at": stamp_of(moment),
            "session_id": "разговор-1",
            "permission_mode": "acceptEdits",
            "prompt": "что правили в этом проекте"}


def episode_source(project, branch, moment):
    """Та же минута глазами хранилища: запись эпизода, как её отдаёт чтение."""
    return {"object_type": "Episode", "session_id": "разговор-1",
            "episode_number": 1,
            "project": project,
            "working_directory": "/home/person/dev/%s" % project,
            "git_branch": branch,
            "started_at": stamp_of(moment),
            "hour_of_day": moment.hour,
            "day_of_week": WEEKDAYS[moment.weekday()],
            "outcome": "done", "title": "правка", "summary": "правка"}


class TestOneShapeForBothSides(unittest.TestCase):
    """Обстановку факта и обстановку хода приводит одна функция.

    Проверяется с обеих сторон: если бы форм было две, эти проверки прошли бы
    каждая на своей половине и разъехались бы молча.
    """

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_the_turn_and_the_episode_give_the_very_same_form(
            self, project, branch, moment):
        """Одна минута, описанная с двух сторон, даёт один и тот же словарь."""
        self.assertEqual(context.of(turn_source(project, branch, moment)),
                         context.of(episode_source(project, branch, moment)))

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_both_sides_carry_every_axis_of_the_form(
            self, project, branch, moment):
        """Набор осей один и тот же, чем бы обстановку ни описали."""
        for source in (turn_source(project, branch, moment),
                       episode_source(project, branch, moment)):
            self.assertEqual(set(context.of(source)), set(context.AXES))

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_the_project_is_read_from_the_directory_when_not_named(
            self, project, branch, moment):
        """Хук проекта не присылает — он выводится из каталога, как при записи."""
        source = turn_source(project, branch, moment)
        self.assertNotIn("project", source)
        self.assertEqual(context.of(source)["project"], project)

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_the_day_matches_an_independent_calendar(
            self, project, branch, moment):
        """День недели считается по календарю, а не по нашему же справочнику."""
        got = context.of(turn_source(project, branch, moment))
        self.assertEqual(got["day_of_week"], WEEKDAYS[moment.weekday()])

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_the_form_of_a_form_is_the_same_form(self, project, branch, moment):
        """Приведение идемпотентно: вызывающему не надо помнить, приводил ли он."""
        once = context.of(turn_source(project, branch, moment))
        self.assertEqual(context.of(once), once)

    @FAST
    @given(moment=MOMENTS)
    def test_a_source_without_anything_gives_an_empty_form(self, moment):
        """Пустой источник — пустая обстановка, а не выдуманная."""
        self.assertEqual(set(context.of({})), set(context.AXES))
        self.assertEqual([v for v in context.of({}).values() if v is not None], [])
        self.assertEqual([v for v in context.of(None).values() if v is not None], [])


class TestTheRegistryIsTheSourceOfAxes(unittest.TestCase):
    """Полный список признаков заведён сразу, сделанные помечены.

    Реестр — не украшение рядом с кодом, а сам код: оси формы и веса берутся
    оттуда. Разойдись список с механикой — несделанный признак стало бы не
    отличить от забытого, ради чего список и заводился.
    """

    def test_the_axes_are_exactly_the_ready_signals(self):
        self.assertEqual(context.AXES, signals.READY)
        self.assertEqual(set(context.WEIGHTS), set(signals.READY))

    def test_the_weights_add_up_to_one(self):
        """Сумма не единица — уместность вылезет за 0…1 и порог соврёт."""
        self.assertAlmostEqual(sum(context.WEIGHTS.values()), 1.0, places=6)

    def test_an_unfinished_signal_has_no_weight_and_a_finished_one_has(self):
        for signal in signals.SIGNALS:
            if signal.ready:
                self.assertIsNotNone(signal.weight, signal.name)
            else:
                self.assertIsNone(signal.weight, signal.name)

    def test_every_signal_says_where_to_take_it_from(self):
        """Признак без источника — это пожелание, а не запись в списке."""
        for signal in signals.SIGNALS:
            self.assertTrue(signal.what.strip(), signal.name)
            self.assertTrue(signal.source.strip(), signal.name)
            self.assertIn(signal.group, signals.GROUPS, signal.name)

    def test_the_list_is_longer_than_what_is_done(self):
        """Несделанное видно в списке. Пустой остаток значил бы, что мы всё знаем."""
        self.assertTrue(signals.PLANNED)
        self.assertEqual(len(signals.SIGNALS),
                         len(signals.READY) + len(signals.PLANNED))
        self.assertEqual(len(signals.BY_NAME), len(signals.SIGNALS),
                         "имена признаков повторяются")

    def test_the_numbers_say_the_rule_out_loud(self):
        """«Проект решает, остальное уточняет» — свойство самих чисел.

        Проверяем арифметику весов, а не выдачу: перенастрой кто-нибудь веса,
        и правило сломается молча, а выдача на наших примерах останется той же.
        """
        others = sum(w for name, w in context.WEIGHTS.items() if name != "project")
        self.assertLess(others, suggest.MIN_FIT,
                        "промах по проекту выкупается остальными осями")
        self.assertGreaterEqual(context.WEIGHTS["project"], suggest.MIN_FIT,
                                "совпадения по проекту одного мало для порога")

    def test_a_planned_axis_is_refused_by_its_own_name(self):
        """«Заведён, но не сделан» и «нет такого» — разные ответы.

        Один общий ответ прятал бы весь список: спросивший про `embedding`
        решил бы, что признака нет и в замысле.
        """
        with self.assertRaises(ValueError) as planned:
            context.norm({signals.PLANNED[0]: "что-нибудь"})
        self.assertIn("не сделан", str(planned.exception))
        with self.assertRaises(ValueError) as unknown:
            context.norm({"такой-оси-нет": "что-нибудь"})
        self.assertIn("нет такой оси", str(unknown.exception))


class TestTheFitIsANumberBetweenZeroAndOne(unittest.TestCase):
    """Уместность — число от совпадения. Свойства числа, а не примеры."""

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_the_fit_never_leaves_its_range(self, a, b):
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        got = context.fit(there, here)
        self.assertGreaterEqual(got, 0.0)
        self.assertLessEqual(got, 1.0)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_the_same_situation_fits_itself_completely(self, a):
        one = context.of(turn_source(*a))
        self.assertEqual(context.fit(one, one), 1.0)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_the_fit_reads_the_same_from_either_end(self, a, b):
        """Совпадение симметрично: у него нет своей и чужой стороны."""
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        self.assertEqual(context.fit(there, here), context.fit(here, there))

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           axis=st.sampled_from(context.AXES))
    def test_spoiling_one_axis_never_raises_the_fit(self, a, b, axis):
        """Ухудшил совпадение по оси — уместность не выросла. Монотонность."""
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        assume(there[axis] is not None and here[axis] is not None)
        before = context.fit(there, here)
        spoiled = dict(there, **{axis: _other(here[axis])})
        self.assertLessEqual(context.fit(spoiled, here), before)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           axis=st.sampled_from(context.AXES))
    def test_matching_one_more_axis_never_lowers_the_fit(self, a, b, axis):
        """Совпало на ось больше — уместность не упала. Та же монотонность."""
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        assume(there[axis] is not None and here[axis] is not None)
        before = context.fit(there, here)
        self.assertGreaterEqual(context.fit(dict(there, **{axis: here[axis]}), here),
                                before)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_a_missed_project_is_never_bought_back(self, a, b):
        """Промах по проекту не выкупается остальными осями.

        Числа весов подобраны под порог именно так: сумма четырёх остальных
        меньше порога. Иначе факт чужого проекта, случайно совпавший веткой и
        днём, доезжал бы до агента — ровно то, ради чего всё затевалось.
        """
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        assume(there["project"] != here["project"])
        self.assertLess(context.fit(there, here), suggest.MIN_FIT)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_a_matched_project_alone_is_enough(self, a, b):
        """Совпадение по проекту проходит порог само, без остальных осей."""
        there, here = context.of(turn_source(*a)), context.of(turn_source(*b))
        assume(there["project"] == here["project"])
        self.assertGreaterEqual(context.fit(there, here), suggest.MIN_FIT)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_nothing_in_common_means_nobody_is_judged(self, a, b):
        """Общих осей нет — судить нечем. Такой факт уместен везде.

        Иначе глобальное знание про человека выбывало бы из выдачи всюду:
        проекта у него нет, и «не совпало» читалось бы как «не к месту».
        """
        here = context.of(turn_source(*b))
        self.assertEqual(context.fit(context.of({}), here), context.NEUTRAL)
        self.assertEqual(context.fit(context.of(turn_source(*a)), context.of({})),
                         context.NEUTRAL)

    @FAST
    @given(project=PROJECTS, branch=BRANCHES, moment=MOMENTS)
    def test_a_global_fact_is_at_home_anywhere(self, project, branch, moment):
        """Охват `global` — знание про человека, а не про место. Не судим."""
        fact = {"object_type": "Fact", "fact_type": "preference",
                "subject": "формат ответа", "scope": "global",
                "content": "отвечать коротко",
                "project": project, "updated_at": stamp_of(moment)}
        self.assertEqual(context.fit(context.of(fact),
                                     context.of(turn_source(project, branch, moment))),
                         context.NEUTRAL)

    @FAST
    @given(a=st.tuples(PROJECTS, BRANCHES, MOMENTS),
           b=st.tuples(PROJECTS, BRANCHES, MOMENTS))
    def test_the_best_of_several_situations_is_the_best_of_them(self, a, b):
        """У факта обстановок несколько: он встречался не один раз.

        Берём лучшую: факт, однажды записанный здесь, к месту здесь — даже
        если в другой раз его видели в другом проекте.
        """
        here = context.of(turn_source(*b))
        pair = [context.of(turn_source(*a)), here]
        self.assertEqual(context.best(pair, here), 1.0)
        self.assertEqual(context.best([], here), context.NEUTRAL)
        self.assertEqual(
            context.best(pair, here),
            max(context.fit(one, here) for one in pair))


def _other(value):
    """Значение той же оси, заведомо не равное данному.

    Считается от той стороны, с которой сравнивают, а не от той, которую
    портим: «ухудшить совпадение» — это разойтись с обстановкой хода. Первая
    версия двигала своё значение и иногда попадала в чужое, то есть
    совпадение улучшала.
    """
    if isinstance(value, int):
        return (value + context.HOURS_IN_PART) % 24
    return str(value) + "-иное"


class TestTheThresholdJudgesTheProduct(unittest.TestCase):
    """Порог применяется к произведению веса на уместность, а не к весу."""

    @FAST
    @given(score=st.floats(min_value=0.0, max_value=1.0),
           fit=st.floats(min_value=0.0, max_value=1.0),
           bar=st.floats(min_value=0.0, max_value=1.0))
    def test_nothing_below_the_bar_gets_through(self, score, fit, bar):
        record = {"object_type": "Fact", "content": "факт", "fit": fit}
        kept = suggest.gate([(score, "факт", record)], min_score=bar)
        self.assertEqual(bool(kept), score * fit >= bar)

    @FAST
    @given(score=st.floats(min_value=0.0, max_value=1.0),
           bar=st.floats(min_value=0.0, max_value=1.0))
    def test_without_a_situation_the_weight_alone_decides(self, score, bar):
        """Уместность не посчитана — порог судит вес, как судил всегда."""
        record = {"object_type": "Fact", "content": "факт"}
        kept = suggest.gate([(score, "факт", record)], min_score=bar)
        self.assertEqual(bool(kept), score >= bar)

    @FAST
    @given(fit=st.floats(min_value=0.0, max_value=1.0))
    def test_an_unweighed_piece_is_judged_by_its_fit_alone(self, fit):
        """У записи из базы веса нет вовсе: его дописывает только текстовый путь.

        Множителя, на который умножать, тут не существует, и порог на
        произведении вырождается в порог на уместности. Это и есть тот случай,
        ради которого вся работа: неуместное режется без всякой оценки.
        """
        record = {"object_type": "Fact", "content": "факт", "fit": fit}
        kept = suggest.gate([(None, "факт", record)], min_score=0.5)
        self.assertEqual(bool(kept), fit >= suggest.MIN_FIT)


CWD_ROOT = "/home/person/dev"
DAY = "2026-08-24"          # понедельник


def rows(session, project, names, day=DAY, hour=10):
    cwd = "%s/%s" % (CWD_ROOT, project)
    stamp = "%sT%02d:00:00Z" % (day, hour)
    head = {"sessionId": session, "timestamp": stamp, "cwd": cwd,
            "gitBranch": "main"}
    blocks = [{"type": "tool_use", "name": "Edit",
               "input": {"file_path": "%s/%s" % (cwd, name)}} for name in names]
    blocks.append({"type": "text", "text": "Готово."})
    return [dict(head, type="user", message={"content": "Посмотри, что там с базой"}),
            dict(head, type="assistant", message={"content": blocks})]


def archive(root, shape):
    """shape: список (разговор, проект, файлы, день, час)."""
    paths = []
    for number, spec in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(*spec):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        paths.append(path)
    return paths


@contextlib.contextmanager
def store(tmp):
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def fill(files):
    understand.digest(files, door=port.door(), dry=False)
    associate.build(files, door=port.door(), dry=False)


def texts(kept):
    return " ".join(text for _, text, _ in kept)


def here_of(project, day=DAY, hour=10):
    return {"cwd": "%s/%s" % (CWD_ROOT, project), "git_branch": "main",
            "occurred_at": "%sT%02d:00:00Z" % (day, hour)}


class TestTheSameQuestionInAnotherProject(unittest.TestCase):
    """Ожидаемый сигнал целиком. Мутация уместности обязана красить этот класс."""

    @SLOW
    @given(pair=st.lists(st.sampled_from(["marginal-gain", "job-hunt", "ru-side"]),
                         min_size=2, max_size=2, unique=True))
    def test_the_fact_shows_up_at_home_and_stays_away_elsewhere(self, pair):
        mine, other = pair
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", mine, ["db.py"], DAY, 10)]))
            door = port.door()
            _, at_home, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                            door=door, here=here_of(mine))
            self.assertIn("db.py", texts(at_home),
                          "дома факт не всплыл — проверять отсутствие не на чем")
            _, away, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door, here=here_of(other))
            self.assertNotIn("db.py", texts(away),
                             "факт чужого проекта всплыл на тот же вопрос")

    def test_without_a_situation_everything_works_as_before(self):
        """Обстановки нет — выдача ровно та же, что была до уместности.

        Так ходит замер и все прежние вызовы: молчание об обстановке не должно
        читаться как «ничего не подходит».
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", "marginal-gain", ["db.py"], DAY, 10)]))
            door = port.door()
            _, blind, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                          door=door)
            _, at_home, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                            door=door,
                                            here=here_of("marginal-gain"))
            self.assertTrue(blind)
            self.assertEqual([t for _, t, _ in blind], [t for _, t, _ in at_home])

    @SLOW
    @given(pair=st.lists(st.sampled_from(["marginal-gain", "job-hunt", "ru-side"]),
                         min_size=2, max_size=2, unique=True))
    def test_a_global_fact_travels_with_the_person(self, pair):
        """Предпочтение человека всплывает в любом проекте: оно не про место."""
        mine, other = pair
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", mine, ["db.py"], DAY, 10)]))
            door = port.door()
            door.write_objects([models.Fact(
                fact_type="preference", subject="формат ответа", scope="global",
                content="Человек просит отвечать коротко, без предисловий.")])
            _, away, _ = suggest.suggest("формат ответа", mode="raw",
                                         min_score=0.0, door=door,
                                         here=here_of(other))
            self.assertIn("коротко", texts(away),
                          "глобальный факт срезан чужим проектом")


class TestGlobalKnowledgeStaysUnbound(unittest.TestCase):
    """Глобальный факт связан с эпизодом, но обстановку у него не берёт.

    Связь `episode_facts` разбор хода ставит всем фактам эпизода без разбора,
    в том числе предпочтениям человека. Возьми её обстановку — и привычка
    окажется заперта в том проекте, где её впервые заметили.
    """

    @SLOW
    @given(pair=st.lists(st.sampled_from(["marginal-gain", "job-hunt", "ru-side"]),
                         min_size=2, max_size=2, unique=True))
    def test_a_linked_global_fact_has_no_situation(self, pair):
        mine, other = pair
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", mine, ["db.py"], DAY, 10)]))
            door = port.door()
            fact = models.Fact(fact_type="preference", subject="формат ответа",
                               scope="global",
                               content="Человек просит отвечать коротко.")
            episode = models.Episode(session_id="разговор-1", episode_number=1)
            door.write_objects([fact],
                               [models.link("episode_facts", episode=episode,
                                            fact=fact)])
            got = door.contexts([fact.identity()])
            self.assertEqual(got.get(fact.identity()), [],
                             "у глобального факта завелась обстановка эпизода")
            _, away, _ = suggest.suggest("формат ответа", mode="raw",
                                         min_score=0.0, door=door,
                                         here=here_of(other))
            self.assertIn("коротко", texts(away),
                          "связанный с эпизодом глобальный факт срезан проектом")

    @SLOW
    @given(project=st.sampled_from(["marginal-gain", "job-hunt"]))
    def test_a_global_fact_is_in_no_slice_of_a_place(self, project):
        """В срез «факты этого проекта» знание про человека не попадает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", project, ["db.py"], DAY, 10)]))
            door = port.door()
            fact = models.Fact(fact_type="preference", subject="формат ответа",
                               scope="global", content="Отвечать коротко.")
            episode = models.Episode(session_id="разговор-1", episode_number=1)
            door.write_objects([fact],
                               [models.link("episode_facts", episode=episode,
                                            fact=fact)])
            inside = [r["subject"] for r in door.slice({"project": project})]
            self.assertNotIn("формат ответа", inside)
            self.assertIn("формат ответа",
                          [r["subject"] for r in door.slice({})])


class TestTheFactTravelsWithItsContext(unittest.TestCase):
    """Факт уходит в сессию вместе с обстановкой, а не голой строкой."""

    @SLOW
    @given(project=st.sampled_from(["marginal-gain", "job-hunt"]))
    def test_the_piece_carries_its_situation_as_fields(self, project):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", project, ["db.py"], DAY, 10)]))
            door = port.door()
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door, here=here_of(project))
            facts = [r for _, _, r in kept
                     if isinstance(r, dict) and r.get("object_type") == "Fact"]
            self.assertTrue(facts, "фактов в выдаче нет, проверять нечего")
            for record in facts:
                self.assertIn("situation", record, "факт ушёл без обстановки")
                self.assertEqual(set(record["situation"]), set(context.AXES))
                self.assertIsInstance(record.get("fit"), float)

    @SLOW
    @given(project=st.sampled_from(["marginal-gain", "job-hunt"]))
    def test_the_text_for_the_model_names_the_situation(self, project):
        """Модель должна видеть, откуда факт, и решать сама."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            fill(archive(tmp, [("разговор-1", project, ["db.py"], DAY, 10)]))
            door = port.door()
            text, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                            door=door, here=here_of(project))
            self.assertTrue(kept)
            # Спрашиваем именно строку обстановки, а не «есть ли где-то в
            # тексте слово». Пересказ эпизода содержит и проект, и день сам по
            # себе, и проверка «assertIn(project, text)» проходит на коде,
            # который обстановку не приписывает вовсе.
            said = [line.strip() for line in text.splitlines()
                    if line.strip().startswith("обстановка:")]
            self.assertTrue(said, "строки обстановки в куске нет вовсе")
            self.assertTrue(any(project in line for line in said),
                            "в обстановке не назван проект: %s" % said)
            self.assertTrue(any("monday" in line for line in said),
                            "в обстановке не назван день: %s" % said)
            self.assertTrue(any("уместность" in line for line in said),
                            "уместность не названа: %s" % said)

    def test_a_piece_without_a_situation_gets_no_invented_one(self):
        """Обстановки нет — и не приписываем: выдуманное выглядит измеренным."""
        self.assertNotIn("обстановка", suggest.render([(None, "факт", None)]))
        self.assertNotIn("уместность", suggest.render([(0.9, "факт", None)]))


class TestTheSituationCostsNothingToTake(unittest.TestCase):
    """Обстановка снимается без сети и без модели: хук стоит в горячем пути."""

    def test_the_hook_payload_gives_the_whole_situation(self):
        """Из payload берётся всё, что там есть, а не один только вопрос.

        Проверяем аргумент, с которым позвали подсказку, а не «тест не упал»:
        подмена имени в модуле прошла бы и на коде, который payload
        по-прежнему выбрасывает.
        """
        payload = {"prompt": "что правили", "session_id": "разговор-1",
                   "cwd": "/home/person/dev/marginal-gain",
                   "permission_mode": "acceptEdits"}
        seen = {}

        def spy(query, mode="single", min_score=0.5, door=None, here=None):
            seen["query"], seen["here"] = query, here
            return "", [], ""

        with tempfile.TemporaryDirectory() as tmp, store(tmp), \
             mock.patch.object(suggest, "suggest", spy), \
             mock.patch("sys.stdin", _Stdin(json.dumps(payload))), \
             mock.patch("sys.argv", ["suggest", "--hook"]):
            suggest.main()

        self.assertEqual(seen.get("query"), "что правили")
        self.assertEqual(seen.get("here", {}).get("project"), "marginal-gain")
        self.assertEqual(seen["here"]["working_directory"],
                         "/home/person/dev/marginal-gain")

    @FAST
    @given(project=PROJECTS, moment=MOMENTS)
    def test_the_turn_gets_a_time_because_the_payload_has_none(self, project, moment):
        """Времени в payload нет — «сейчас» подставляет снятие обстановки.

        Иначе день и часть суток у хода всегда пусты, а пустая ось не судится
        ни с одной из сторон: две временные оси из пяти молча не работали бы
        никогда, и заметить это по выдаче было бы нечем.
        """
        bare = suggest.situation_of({"cwd": "/home/person/dev/%s" % project})
        self.assertIsNotNone(bare["day_of_week"])
        self.assertIsNotNone(bare["hour_of_day"])
        # Сказанное в payload сильнее подставленного: замер и проверки должны
        # уметь назвать своё время.
        told = suggest.situation_of({"cwd": "/home/person/dev/%s" % project,
                                     "occurred_at": stamp_of(moment)})
        self.assertEqual(told["day_of_week"], WEEKDAYS[moment.weekday()])

    def test_an_empty_payload_is_no_situation_at_all(self):
        """Ни каталога, ни времени — молчание, а не словарь из одного часа.

        Словарь с одним лишь днём недели судил бы факты по дню и только по
        нему: обстановки нет, а фильтр есть.
        """
        self.assertIsNone(suggest.situation_of({}))
        self.assertIsNone(suggest.situation_of(None))

    def test_the_branch_is_read_from_the_checkout_without_a_subprocess(self):
        """Ветка берётся чтением .git/HEAD: запускать git в горячем пути дорого."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".git" / "HEAD").write_text("ref: refs/heads/fact-relevance\n")
            self.assertEqual(context.branch_of(tmp), "fact-relevance")
            self.assertIsNone(context.branch_of(Path(tmp) / "нет-такого"))

    def test_taking_the_situation_touches_neither_net_nor_model(self):
        """Ни одного обращения наружу: снятие обстановки — чистая функция."""
        import subprocess
        with mock.patch.object(subprocess, "run",
                               side_effect=AssertionError("обстановка полезла наружу")):
            got = context.of({"cwd": "/home/person/dev/demo",
                              "occurred_at": "2026-08-25T10:00:00Z"})
        self.assertEqual(got["project"], "demo")


class _Stdin:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text


if __name__ == "__main__":
    unittest.main()
