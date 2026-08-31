#!/usr/bin/env python3
"""Забывание: у факта есть срок, просроченное уходит из первой выдачи.

Запуск: python3 -m unittest tests.test_forgetting -v

Память умела запоминать, связывать, находить и отмечать пользу. Забывать она
не умела вовсе: в записи было «когда увидели» и «когда впервые», а «до каких
пор верно» — нигде. Вчерашняя правда и позапрошлогодняя лежали вперемешку и
весили одинаково.

Три правила, и проверяются они свойствами, а не примерами:

1. Срок лежит на самом факте значением, и берётся оно из режима памяти. Из
   типа факта срок не выводится: адрес репозитория и правка файла живут по
   одному режиму, а не по двум разным правилам.
2. Просроченное выбывает из первой выдачи и при этом цело. Оно переложено в
   отдельный объект схемы, а не помечено флагом: флаг оставил бы запись в той
   же выборке, и «первую выдачу» пришлось бы отличать порогом на чтении.
3. Обращение продлевает срок. Факт, попавший в выдачу, отсчитывает срок
   заново — часто спрашиваемое остаётся, невостребованное выбывает.

Свойство, которое здесь главное: на потоке с известной частотой обращений
судьба факта предсказуема числом тактов, а не наблюдается по случаю.

Мутации, на которых проверки обязаны краснеть:
  * вывести срок из типа факта                → TestTheDeadlineIsAValue
  * удалять просроченное вместо переклада     → TestTheOverdueLeavesButStaysWhole
  * перестать продлевать срок при обращении   → TestAskingKeepsItAlive
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import lifespan, models
from infra import config
from pipeline import forget, suggest, understand
from storage import db, local, port

HERE = Path(__file__).resolve().parent.parent

CWD = "/home/person/dev/demo"
BRANCH = "forgetting"

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

MODES = sorted(lifespan.MODES)


@contextlib.contextmanager
def store(tmp, mode=None):
    """Локальная база и режим памяти — оба на время одной проверки."""
    base = Path(tmp) / "memory.db"
    local.close()
    env = {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
           "XMEM_LOCAL_PATH": str(base), "XMEM_MEMORY": mode or ""}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(config, "STATE_DIR", Path(tmp) / "state"), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def fact(name, at, mode=None, kind="project_state", scope="project"):
    """Факт со сроком. Срок ставится тем же кодом, каким его ставит конвейер."""
    return models.Fact(fact_type=kind, subject="%s/%s" % (CWD, name), scope=scope,
                       content="правился файл %s" % name, project="demo",
                       updated_at=lifespan.stamp(at),
                       valid_until=lifespan.until(at, mode))


def put(door, *records):
    door.write_objects(list(records))


def rows_of(base, table):
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM "%s"' % table)]
    finally:
        conn.close()


def subjects(records):
    return sorted(r["subject"] for r in records)


def found(door, query, deep=False):
    """Что отдаёт первая выдача, и что — глубокое чтение."""
    if deep:
        return door.deep(query)
    answer = door.read(query, mode="raw")
    return json.loads(answer) if answer else []


# --- 1. Срок значением, и берётся он из режима -----------------------------


class TestTheDeadlineIsAValue(unittest.TestCase):
    """Срок лежит на записи, а не выводится из типа при чтении."""

    def test_a_written_fact_carries_its_deadline(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            put(port.door(), fact("db.py", T0))
            got = rows_of(base, "fact")
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0]["valid_until"], "срока на записи нет")

    @SLOW
    @given(kind=st.sampled_from(models.FACT_TYPES), mode=st.sampled_from(MODES))
    def test_the_type_of_the_fact_does_not_change_the_deadline(self, kind, mode):
        """Режим один на все факты. Отдельного срока на тип нет.

        Спрашиваем сам конвейер, а не функцию срока: срок, выведенный из типа,
        поселился бы именно там, где запись собирается, и сравнение двух
        вызовов `lifespan.until` этого не заметило бы вовсе — первая версия
        этой проверки так и зеленела на мутации.

        Мутация: вернуть в `understand.fact_of` ветку по `fact_type` — свойство
        краснеет на первом же типе, отличном от взятого за образец.
        """
        scope = "global" if kind in ("user", "preference") else "project"
        ep = {"cwd": CWD, "ended_at": lifespan.stamp(T0)}
        one = understand.fact_of(ep, (kind, "db.py", scope, "текст"), mode)
        same = understand.fact_of(ep, ("project_state", "db.py", "project", "текст"),
                                  mode)
        self.assertTrue(one.valid_until, "тип %s остался без срока" % kind)
        self.assertEqual(one.valid_until, same.valid_until)

    @SLOW
    @given(mode=st.sampled_from(MODES))
    def test_the_mode_is_what_sets_the_default(self, mode):
        """Режим памяти задаёт умолчание, и разные режимы дают разный срок."""
        self.assertEqual(lifespan.until(T0, mode),
                         lifespan.stamp(T0 + timedelta(days=lifespan.MODES[mode])))

    def test_longer_mode_means_longer_life(self):
        got = [lifespan.days(name) for name in
               sorted(lifespan.MODES, key=lambda n: lifespan.MODES[n])]
        self.assertEqual(got, sorted(got))
        self.assertGreater(len(set(got)), 1, "режимы не различаются сроком")

    @SLOW
    @given(mode=st.sampled_from(MODES))
    def test_the_switch_reaches_the_one_who_reads_it(self, mode):
        """Переставленный рубильник долетает до того, кто ставит срок."""
        from pipeline import switch
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XMEM_MEMORY": ""}), \
                 mock.patch.object(config, "STATE_DIR", Path(tmp)):
                switch.SWITCHES["memory"].set(mode)
                self.assertEqual(config.memory(), mode)
                self.assertEqual(lifespan.until(T0), lifespan.until(T0, mode))

    @SLOW
    @given(shift=st.integers(min_value=-500, max_value=500),
           mode=st.sampled_from(MODES))
    def test_deadlines_sort_the_way_the_instants_do(self, shift, mode):
        """Срок сравнивается строкой в SQL. Значит формат обязан быть один.

        Разойдись формат — сравнение соврёт молча, и просроченное останется
        в выдаче либо живое уедет в отложенное.
        """
        later = T0 + timedelta(days=shift)
        a, b = lifespan.until(T0, mode), lifespan.until(later, mode)
        self.assertEqual(a < b, T0 < later)
        self.assertEqual(a == b, T0 == later)

    def test_the_pipeline_puts_a_deadline_on_every_fact_it_writes(self):
        """Конвейер, а не только проверка: разбор архива пишет срок."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            understand.digest(archive(tmp, [["db.py", "port.py"]]),
                              door=port.door(), dry=False)
            got = rows_of(base, "fact")
            self.assertTrue(got, "разбор не записал ни одного факта")
            for row in got:
                self.assertTrue(row["valid_until"], "факт без срока: %s" % row["subject"])


# --- 2. Просроченное выбывает из первой выдачи, но остаётся целым -----------


class TestTheOverdueLeavesButStaysWhole(unittest.TestCase):
    """Вытеснение, а не удаление: запись перекладывается в отложенное."""

    def test_the_overdue_fact_is_gone_from_the_first_answer(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            self.assertTrue(found(door, "db.py"), "факт не находится и до срока")
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            self.assertEqual(found(door, "db.py"), [],
                             "просроченное осталось в первой выдаче")

    def test_the_deep_read_brings_it_back(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            deep = found(door, "db.py", deep=True)
            self.assertEqual(subjects(deep), ["%s/db.py" % CWD])
            self.assertEqual(deep[0]["object_type"], "LapsedFact")
            self.assertEqual(deep[0]["content"], "правился файл db.py")

    @SLOW
    @given(names=st.lists(st.sampled_from(["db.py", "port.py", "run.py", "api.py"]),
                          min_size=1, max_size=4, unique=True),
           overdue=st.lists(st.booleans(), min_size=1, max_size=4),
           mode=st.sampled_from(MODES))
    def test_nothing_is_ever_lost(self, names, overdue, mode):
        """Сколько записали — столько и лежит. Переклад ничего не удаляет.

        Мутация: заменить переклад удалением — сумма перестаёт сходиться на
        первом же просроченном факте.
        """
        overdue = (overdue * len(names))[:len(names)]
        span = lifespan.days(mode)
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            for name, old in zip(names, overdue):
                born = T0 - timedelta(days=span + 10) if old else T0
                put(door, fact(name, born, mode=mode))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(hours=1)))
            live, dead = rows_of(base, "fact"), rows_of(base, "lapsedfact")
            self.assertEqual(len(live) + len(dead), len(names))
            self.assertEqual(subjects(live) + subjects(dead) and
                             sorted(subjects(live) + subjects(dead)),
                             sorted("%s/%s" % (CWD, n) for n in names))
            self.assertEqual(subjects(dead),
                             sorted("%s/%s" % (CWD, n)
                                    for n, old in zip(names, overdue) if old))

    @SLOW
    @given(names=st.lists(st.sampled_from(["db.py", "port.py", "run.py"]),
                          min_size=1, max_size=3, unique=True),
           mode=st.sampled_from(MODES))
    def test_what_is_still_valid_is_not_touched(self, names, mode):
        """До срока не трогаем ничего: забывание не должно быть жадным."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            for name in names:
                put(door, fact(name, T0, mode=mode))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(hours=1)))
            self.assertEqual(rows_of(base, "lapsedfact"), [])
            self.assertEqual(len(rows_of(base, "fact")), len(names))

    def test_the_lapsed_copy_keeps_every_field_of_the_fact(self):
        """Целая — значит со всеми полями, а не одним ключом."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            was = rows_of(base, "fact")[0]
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            now = rows_of(base, "lapsedfact")[0]
            for name, value in was.items():
                self.assertEqual(now[name], value, name)
            self.assertTrue(now["lapsed_at"], "не записано, когда выбыл")

    def test_a_second_sweep_moves_nothing(self):
        """Проход повторяем: второй заход не находит уже переложенного."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            late = lifespan.stamp(T0 + timedelta(days=400))
            self.assertEqual(forget.sweep(door=door, now=late)["moved"], 1)
            self.assertEqual(forget.sweep(door=door, now=late)["moved"], 0)

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            late = lifespan.stamp(T0 + timedelta(days=400))
            self.assertEqual(forget.sweep(door=door, now=late, dry=True)["moved"], 1)
            self.assertEqual(rows_of(base, "lapsedfact"), [])
            self.assertEqual(len(rows_of(base, "fact")), 1)

    def test_a_fact_without_a_deadline_never_lapses(self):
        """Срока нет — забывать не по чему. Молча выкидывать такое нельзя."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, models.Fact(fact_type="external_resource", subject="repo",
                                  scope="global", content="адрес репозитория"))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=4000)))
            self.assertEqual(len(rows_of(base, "fact")), 1)
            self.assertEqual(rows_of(base, "lapsedfact"), [])

    def test_the_lapsed_stay_out_of_the_graph_step(self):
        """Сосед по графу — тоже первая выдача. Отложенному там не место."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            near = fact("port.py", T0, mode="short")
            put(door, fact("db.py", T0), near,
                models.Association(source_key=fact("db.py", T0).identity(),
                                   target_key=near.identity(), cue="same_episode",
                                   weight=5.0))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                         door=door)
            self.assertNotIn("port.py", " ".join(t for _, t, _ in kept))


# --- 3. Обращение продлевает срок ------------------------------------------


class TestAskingKeepsItAlive(unittest.TestCase):
    """Спрошенное живёт дальше, невостребованное выбывает."""

    def test_a_fact_in_the_answer_gets_its_deadline_pushed_forward(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"))
            was = rows_of(base, "fact")[0]["valid_until"]
            later = lifespan.stamp(T0 + timedelta(days=3))
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=door)
            suggest.note_injection("разговор-1", "текст", kept, door=door, at=later)
            now = rows_of(base, "fact")[0]
            self.assertGreater(now["valid_until"], was, "срок не сдвинулся")
            self.assertEqual(now["content"], "правился файл db.py",
                             "продление затёрло содержимое")

    def test_renewal_never_moves_a_deadline_backwards(self):
        """Продление только вперёд: обращение не может укоротить жизнь."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="long"))
            was = rows_of(base, "fact")[0]["valid_until"]
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=door)
            suggest.note_injection("разговор-1", "текст", kept, door=door,
                                   at=lifespan.stamp(T0 + timedelta(days=1)))
            self.assertGreaterEqual(rows_of(base, "fact")[0]["valid_until"], was)

    @SLOW
    @given(born=st.sampled_from(MODES), asked=st.sampled_from(MODES))
    def test_renewal_never_shortens_a_life_already_granted(self, born, asked):
        """Срок движется только вперёд, даже если режим сменился.

        Факт записан при долгом режиме, показан при коротком. Продление — это
        «живёт как минимум ещё столько», а не «живёт ровно столько»: укоротить
        уже назначенную жизнь показ не вправе.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp, mode=asked) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode=born))
            was = rows_of(base, "fact")[0]["valid_until"]
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=door)
            suggest.note_injection("разговор-1", "текст", kept, door=door,
                                   at=lifespan.stamp(T0))
            self.assertGreaterEqual(rows_of(base, "fact")[0]["valid_until"], was)

    def test_renewal_does_not_hand_a_deadline_to_what_had_none(self):
        """Пустой срок значит «не протухает». Показ не вправе назначить конец.

        Продление знает только ключ факта. Поставь оно срок тому, кому его не
        ставили, — и адрес репозитория, который не устаревает по времени,
        выбыл бы через месяц просто потому, что его однажды показали.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, models.Fact(fact_type="external_resource", subject="db.py",
                                  scope="global", content="адрес репозитория"))
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=door)
            suggest.note_injection("разговор-1", "текст", kept, door=door,
                                   at=lifespan.stamp(T0))
            self.assertFalse(rows_of(base, "fact")[0]["valid_until"])

    def test_renewal_touches_only_what_was_shown(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", T0, mode="short"), fact("run.py", T0, mode="short"))
            before = {r["subject"]: r["valid_until"] for r in rows_of(base, "fact")}
            _, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0, door=door)
            suggest.note_injection("разговор-1", "текст", kept, door=door,
                                   at=lifespan.stamp(T0 + timedelta(days=3)))
            after = {r["subject"]: r["valid_until"] for r in rows_of(base, "fact")}
            self.assertGreater(after["%s/db.py" % CWD], before["%s/db.py" % CWD])
            self.assertEqual(after["%s/run.py" % CWD], before["%s/run.py" % CWD])

    @SLOW
    @given(mode=st.sampled_from(MODES), step=st.integers(min_value=1, max_value=40))
    def test_the_asked_survives_and_the_unasked_leaves_on_schedule(self, mode, step):
        """Главное свойство. Поток с известной частотой обращений.

        Такт длиной `step` дней. Один факт спрашивают каждый такт, второй — ни
        разу. Спрошенный обязан остаться навсегда; невостребованный обязан
        выбыть ровно на том такте, где накопленное время перевалило за срок
        режима, — не раньше и не позже.

        Мутация: перестать продлевать срок при обращении — спрошенный факт
        выбывает вместе с невостребованным, и проверка краснеет.
        """
        span = lifespan.days(mode)
        ticks = (span // step) + 2
        asked, quiet = "%s/alpha.py" % CWD, "%s/beta.py" % CWD
        with tempfile.TemporaryDirectory() as tmp, store(tmp, mode=mode) as base:
            door = port.door()
            put(door, fact("alpha.py", T0, mode=mode), fact("beta.py", T0, mode=mode))
            for k in range(1, ticks + 1):
                at = lifespan.stamp(T0 + timedelta(days=k * step))
                _, kept, _ = suggest.suggest("alpha.py", mode="raw",
                                             min_score=0.0, door=door)
                if kept:
                    suggest.note_injection("разговор-1", "текст", kept,
                                           door=door, at=at)
                forget.sweep(door=door, now=at)
                live = subjects(rows_of(base, "fact"))
                self.assertIn(asked, live, "спрошенный выбыл на такте %d" % k)
                self.assertEqual(quiet in live, k * step <= span,
                                 "невостребованный выбыл не на своём такте: %d" % k)
            self.assertEqual(subjects(rows_of(base, "lapsedfact")), [quiet])

    @SLOW
    @given(mode=st.sampled_from(MODES), step=st.integers(min_value=1, max_value=40))
    def test_without_renewal_even_the_asked_one_leaves(self, mode, step):
        """Обратная половина: без продления поток не спасает никого.

        Проверка на пустоту самой проверки выше. Выпотроши продление — и то,
        что спрашивали каждый такт, обязано выбыть вместе с остальным.
        """
        span = lifespan.days(mode)
        ticks = (span // step) + 2
        with tempfile.TemporaryDirectory() as tmp, store(tmp, mode=mode) as base, \
             mock.patch.object(suggest, "renew", lambda *a, **k: []):
            door = port.door()
            put(door, fact("alpha.py", T0, mode=mode), fact("beta.py", T0, mode=mode))
            for k in range(1, ticks + 1):
                at = lifespan.stamp(T0 + timedelta(days=k * step))
                _, kept, _ = suggest.suggest("alpha.py", mode="raw",
                                             min_score=0.0, door=door)
                if kept:
                    suggest.note_injection("разговор-1", "текст", kept,
                                           door=door, at=at)
                forget.sweep(door=door, now=at)
            self.assertEqual(rows_of(base, "fact"), [])
            self.assertEqual(len(rows_of(base, "lapsedfact")), 2)

    def test_renewal_does_not_resurrect_a_fact_that_is_not_there(self):
        """Продление адресует лежащую строку. Нет строки — нечего продлевать.

        Иначе обращение к отложенному заводило бы в первой выдаче призрак:
        ключ есть, содержимого нет.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            ghost = {"object_type": "Fact", "fact_type": "project_state",
                     "subject": "%s/gone.py" % CWD, "scope": "project",
                     "content": "нет такой строки"}
            suggest.renew([(None, "нет такой строки", ghost)], door=door)
            self.assertEqual(rows_of(base, "fact"), [])


# --- 4. Дверь, которая забывать не умеет -----------------------------------


class TestTheDoorThatCannotForget(unittest.TestCase):
    """У сетевого пути ни переклада, ни глубокого чтения нет — и ладно."""

    def test_the_sweep_says_so_instead_of_falling(self):
        got = forget.sweep(door=Deaf(), now=lifespan.stamp(T0))
        self.assertFalse(got["able"])
        self.assertEqual(got["moved"], 0)

    def test_the_suggestion_works_as_before(self):
        """Продление — добавка. Дверь без него отдаёт то же, что отдавала."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", T0))
            text, kept, _ = suggest.suggest("db.py", mode="raw", min_score=0.0,
                                            door=Deaf(door))
            self.assertTrue(kept)
            self.assertEqual(suggest.renew(kept, door=Deaf(door)), [])


class TestTheHookCallsTheSweep(unittest.TestCase):
    """Написанный и не подключённый проход — то же, что ненаписанный.

    Проверяем текст хука, а не поведение: запускать живой конец хода из теста
    значит писать в хранилище пользователя. Проверка грубая, но ловит ровно то,
    ради чего задача, — вызова нет вовсе, и память не забывает никогда.
    """

    def setUp(self):
        self.body = (HERE / "hooks" / "on_stop.sh").read_text(encoding="utf-8")

    def test_the_sweep_is_called(self):
        self.assertIn("pipeline.forget", self.body,
                      "просроченное не выбывает: выдача будет только пухнуть")

    def test_it_writes_and_does_not_idle(self):
        self.assertIn("pipeline.forget --send", self.body,
                      "холостой проход считает и ничего не перекладывает")

    def test_the_sweep_goes_after_everything_that_writes(self):
        """Порядок: сперва в базу ложится новое и продлевается спрошенное.

        Забудь раньше — и факт, только что подтверждённый разбором, выбыл бы
        со старым сроком, не дождавшись новой отметки.
        """
        order = [self.body.index("pipeline.understand"),
                 self.body.index("pipeline.associate"),
                 self.body.index("--settle"),
                 self.body.index("pipeline.forget")]
        self.assertEqual(order, sorted(order), "забывание идёт не последним")


class Deaf:
    """Дверь без переклада и глубокого чтения. Такова сеть, и она в строю."""

    name = "deaf"

    def __init__(self, inner=None):
        self.inner = inner

    def write(self, text, wait=False):
        return "" if self.inner is None else self.inner.write(text, wait=wait)

    def read(self, query, mode="single"):
        return "" if self.inner is None else self.inner.read(query, mode=mode)


# --- общая оснастка --------------------------------------------------------


def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-01-01T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD,
                "gitBranch": BRANCH}
        out.append(dict(head, type="user",
                        message={"content": "Посмотри, что там с базой"}))
        blocks = [{"type": "tool_use", "name": "Edit",
                   "input": {"file_path": "%s/%s" % (CWD, name)}} for name in spec]
        blocks.append({"type": "text", "text": "Готово."})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape, session="разговор-1"):
    path = Path(root) / "разговор.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for line in rows(session, shape):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return [path]


if __name__ == "__main__":
    unittest.main()
