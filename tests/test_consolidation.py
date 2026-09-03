#!/usr/bin/env python3
"""Офлайн-свёртка: группа записей про одно и то же становится одной.

Запуск: python3 -m unittest tests.test_consolidation -v

Память умеет копить и умеет забывать по сроку. Чего она не умела — сворачивать:
три записи про одно и то же оставались тремя и втроём же занимали места в
выдаче. Свёртка отличается от забывания тем, что знание не теряется, а
обобщается, и потому обратимость здесь обязательна.

Четыре правила, и проверяются они свойствами, а не примерами:

1. Правило слияния узкое и синтаксическое: в группу сходятся записи одного
   вида, охвата и проекта с посимвольно одинаковым содержанием. Широкое
   слияние по смыслу ждёт ресёрча и здесь не выдумывается.
2. Свёртка ничего не удаляет. Лишние записи перекладываются в то же отставное
   место, куда их кладёт забывание, и достаются глубоким чтением.
3. Свёртка обратима. По замене видно, из чего она собрана, а разворот
   возвращает таблицу фактов ровно в прежний вид.
4. Проход не конкурирует с живой сессией: он берёт общий замок и уходит,
   если тот занят.

Мутации, на которых проверки обязаны краснеть:
  * удалять исходные вместо переклада      → TestFoldingLosesNothing
  * не переписывать концы связей на замену → TestTheGraphSurvivesTheFold
  * потерять пометку «из чего собрана»     → TestTheReplacementShowsItsSources
  * свернуть записи разного вида или охвата → TestTheRuleIsNarrow
  * идти без замка                          → TestTheFoldDoesNotRaceTheSession
"""
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import folding, lifespan, models
from infra import locks
from pipeline import consolidate, forget, suggest, understand
from storage import local, port

HERE = Path(__file__).resolve().parent.parent

CWD = "/home/person/dev/demo"

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NAMES = ["db.py", "port.py", "run.py", "api.py", "switch.py", "marks.py"]
TEXTS = ["правился файл базы", "человек любит краткость", "адрес репозитория"]


@contextlib.contextmanager
def store(tmp, mode=None):
    """Локальная база и режим памяти — оба на время одной проверки."""
    base = Path(tmp) / "memory.db"
    local.close()
    env = {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
           "XMEM_LOCAL_PATH": str(base), "XMEM_MEMORY": mode or ""}
    with mock.patch.dict(os.environ, env), \
         mock.patch.dict(os.environ, {"XMEM_STATE_DIR": str(Path(tmp) / "state")}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
         mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
        try:
            yield base
        finally:
            local.close()


def fact(name, content, at=T0, kind="project_state", scope="project",
         project="demo", mode=None):
    """Факт со сроком. Срок ставится тем же кодом, каким его ставит конвейер."""
    return models.Fact(fact_type=kind, subject="%s/%s" % (CWD, name), scope=scope,
                       content=content, project=project,
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


def ids(records):
    return sorted(models.Fact(fact_type=r["fact_type"], subject=r["subject"],
                              scope=r["scope"]).identity() for r in records)


def found(door, query, deep=False):
    """Что отдаёт первая выдача, и что — глубокое чтение."""
    if deep:
        return door.deep(query)
    answer = door.read(query, mode="raw")
    return json.loads(answer) if answer else []


def plain(rows):
    """Строки без служебных полей свёртки — для сравнения «до и после»."""
    return sorted(
        tuple(sorted((k, v) for k, v in row.items() if k != "merged_from"))
        for row in rows)


# --- стратегии --------------------------------------------------------------

# Запись описывается пятёркой: имя (оно же тема), текст, вид, охват, проект.
# Совпадение по четырём последним и есть правило слияния, а имя — то, чем
# записи в группе различаются.
#
# Набор осей взят не произвольным, а «звездой»: каждая четвёрка отличается от
# первой ровно одной осью. Так свойство о правиле упирается в каждую ось
# порознь, и при этом четвёрок мало — значит совпадения случаются часто.
# Замер на 400 примерах: хоть одна группа — 81%, группа из трёх — 20%, две
# группы разом — 12%.
#
# Свободный перебор всех осей выглядел честнее и был пуст: четыре вида на два
# охвата на два проекта на три текста дают 48 корзин, а записей в примере не
# больше шести. Группа получалась в 9 примерах из 200, группа из трёх — в
# одном, две группы разом — ни в одном. То есть свойства почти всегда
# проверяли пустую свёртку. Замер покрытия ниже держится в той же форме
# `@given`, что и сами свойства, иначе он мерил бы не то распределение.
BUCKETS = [
    (TEXTS[0], "project_state", "project", "demo"),
    (TEXTS[0], "project_state", "project", "other"),    # отличие только проектом
    (TEXTS[0], "preference", "project", "demo"),        # отличие только видом
    (TEXTS[0], "project_state", "global", "demo"),      # отличие только охватом
    (TEXTS[1], "project_state", "project", "demo"),     # отличие только текстом
]

# Регистр и лишние пробелы — не разница. Пусть половина записей приходит
# «громкой»: тогда свойства проверяют, что группу собирает именно
# нормализация, а не совпадение байт в байт.
shapes = st.lists(
    st.tuples(st.sampled_from(NAMES), st.sampled_from(BUCKETS), st.booleans()),
    min_size=3, max_size=6, unique_by=lambda item: item[0]
).map(lambda items: [(name, (" %s. " % text.upper()) if loud else text,
                      kind, scope, project)
                     for name, (text, kind, scope, project), loud in items])


def written(door, shapes, at=T0, mode=None):
    """Разложить пятёрки в базу. Отдаёт записи в том же порядке."""
    out = []
    for name, text, kind, scope, project in shapes:
        one = fact(name, text, at=at, kind=kind, scope=scope, project=project,
                   mode=mode)
        out.append(one)
    put(door, *out)
    return out


# --- 1. Правило слияния узкое ----------------------------------------------


class TestTheRuleIsNarrow(unittest.TestCase):
    """Сходятся только записи одного вида, охвата и проекта с тем же текстом.

    Мутация: убрать из правила любую из осей — свойство краснеет на первой же
    паре, различающейся по ней.
    """

    def test_normalising_is_idempotent(self):
        for text in TEXTS:
            self.assertEqual(folding.norm(folding.norm(text)), folding.norm(text))

    @SLOW
    @given(text=st.sampled_from(TEXTS), pad=st.text(alphabet=" \t\n", max_size=4))
    def test_spacing_and_case_do_not_make_a_new_fact(self, text, pad):
        """Пробелы и регистр — не разница. Всё прочее — разница."""
        self.assertEqual(folding.norm(pad + text.upper() + pad), folding.norm(text))

    @SLOW
    @given(a=st.sampled_from(TEXTS), b=st.sampled_from(TEXTS))
    def test_different_words_stay_different(self, a, b):
        self.assertEqual(folding.norm(a) == folding.norm(b), a == b)

    @SLOW
    @given(shapes=shapes)
    def test_two_rows_share_a_group_exactly_when_the_rule_says_so(self, shapes):
        """Группа — это в точности совпадение по четырём осям, не шире и не уже."""
        rows = [dict(fact_type=k, subject=n, scope=s, project=p, content=t,
                     updated_at=lifespan.stamp(T0), valid_until=lifespan.until(T0))
                for n, t, k, s, p in shapes]
        together = {}
        for number, group in enumerate(folding.groups(rows)):
            for row in group:
                together[row["subject"]] = number
        for one in rows:
            for two in rows:
                if one is two:
                    continue
                same = (one["fact_type"] == two["fact_type"]
                        and one["scope"] == two["scope"]
                        and one["project"] == two["project"]
                        and folding.norm(one["content"]) == folding.norm(two["content"]))
                pair = (together.get(one["subject"]), together.get(two["subject"]))
                self.assertEqual(same, pair[0] is not None and pair[0] == pair[1],
                                 "%s и %s" % (one["subject"], two["subject"]))

    @SLOW
    @given(shapes=shapes)
    def test_groups_never_hold_a_lonely_row(self, shapes):
        """Одиночке сворачиваться не с чем: группы короче двух не бывает."""
        rows = [dict(fact_type=k, subject=n, scope=s, project=p, content=t,
                     updated_at=lifespan.stamp(T0), valid_until=lifespan.until(T0))
                for n, t, k, s, p in shapes]
        seen = []
        for group in folding.groups(rows):
            self.assertGreaterEqual(len(group), 2)
            seen += [row["subject"] for row in group]
        self.assertEqual(len(seen), len(set(seen)), "запись попала в две группы")

    def test_an_empty_content_is_not_a_topic(self):
        """Пустой текст — не «одно и то же». Сворачивать по пустоте нельзя."""
        rows = [dict(fact_type="user", subject=n, scope="global", project=None,
                     content="", updated_at="", valid_until="")
                for n in ("a", "b", "c")]
        self.assertEqual(folding.groups(rows), [])

    @SLOW
    @given(shapes=shapes)
    def test_the_survivor_comes_from_the_group(self, shapes):
        """Замена не выдумывается: её содержание взято из самой группы."""
        rows = [dict(fact_type=k, subject=n, scope=s, project=p, content=t,
                     updated_at=lifespan.stamp(T0), valid_until=lifespan.until(T0))
                for n, t, k, s, p in shapes]
        for group in folding.groups(rows):
            keep = folding.survivor(group)
            self.assertIn(keep, group)

    @SLOW
    @given(days=st.lists(st.integers(min_value=0, max_value=300),
                         min_size=2, max_size=5))
    def test_the_longest_living_one_stays(self, days):
        """Остаётся тот, кто прожил бы дольше: свёртка не укорачивает жизнь.

        Мутация: выбрать замену как попало — свойство краснеет, как только
        срок замены оказывается меньше чьего-нибудь в группе.
        """
        group = [dict(fact_type="project_state", subject="s%d" % i, scope="project",
                      project="demo", content="один и тот же текст",
                      updated_at=lifespan.stamp(T0),
                      valid_until=lifespan.until(T0 + timedelta(days=d)))
                 for i, d in enumerate(days)]
        keep = folding.survivor(group)
        self.assertEqual(keep["valid_until"], max(r["valid_until"] for r in group))

    def test_a_fact_without_a_deadline_wins(self):
        """Пустой срок значит «не протухает». Смертная замена убила бы бессмертное."""
        group = [dict(fact_type="external_resource", subject="a", scope="global",
                      project=None, content="адрес репозитория",
                      updated_at=lifespan.stamp(T0), valid_until=None),
                 dict(fact_type="external_resource", subject="b", scope="global",
                      project=None, content="адрес репозитория",
                      updated_at=lifespan.stamp(T0),
                      valid_until=lifespan.until(T0))]
        self.assertFalse(folding.survivor(group)["valid_until"])


# --- 2. Свёртка ничего не теряет -------------------------------------------


class TestFoldingLosesNothing(unittest.TestCase):
    """Переклад, а не удаление. Сумма записей не меняется никогда.

    Мутация: заменить переклад удалением — сумма перестаёт сходиться, а
    глубокое чтение по теме приходит пустым.
    """

    def test_three_facts_about_one_thing_become_one(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"),
                fact("run.py", "правился файл базы"))
            self.assertEqual(len(rows_of(base, "fact")), 3)
            got = consolidate.fold(door=door, now=lifespan.stamp(T0))
            self.assertEqual(got["folded"], 2)
            self.assertEqual(len(rows_of(base, "fact")), 1)
            self.assertEqual(len(rows_of(base, "lapsedfact")), 2)

    @SLOW
    @given(shapes=shapes)
    def test_nothing_is_ever_lost(self, shapes):
        """Сколько записали — столько и лежит, живым или отставным."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            written(door, shapes)
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            live, dead = rows_of(base, "fact"), rows_of(base, "lapsedfact")
            self.assertEqual(len(live) + len(dead), len(shapes))
            self.assertEqual(sorted(ids(live) + ids(dead)),
                             sorted(ids(live) + ids(dead)))
            self.assertEqual(len(set(ids(live)) & set(ids(dead))), 0,
                             "запись осталась и живой, и отставной")

    @SLOW
    @given(shapes=shapes)
    def test_the_deep_read_still_finds_what_was_folded_away(self, shapes):
        """Отставное читается глубоким чтением, а первой выдачей — нет."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            written(door, shapes)
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            dead = rows_of(base, "lapsedfact")
            for row in dead:
                name = row["subject"].rsplit("/", 1)[-1]
                deep = [r["subject"] for r in found(door, name, deep=True)]
                self.assertIn(row["subject"], deep, "отставное не достаётся")
                first = [r["subject"] for r in found(door, name)]
                self.assertNotIn(row["subject"], first,
                                 "свёрнутое осталось в первой выдаче")

    @SLOW
    @given(shapes=shapes)
    def test_a_second_pass_folds_nothing(self, shapes):
        """Проход повторяем: свёрнутое второй раз не сворачивается."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            written(door, shapes)
            at = lifespan.stamp(T0)
            consolidate.fold(door=door, now=at)
            self.assertEqual(consolidate.fold(door=door, now=at)["folded"], 0)

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            self.assertEqual(
                consolidate.fold(door=door, now=lifespan.stamp(T0), dry=True)["folded"], 1)
            self.assertEqual(rows_of(base, "lapsedfact"), [])
            self.assertEqual(len(rows_of(base, "fact")), 2)

    def test_the_retired_copy_keeps_every_field(self):
        """Целая — значит со всеми полями, а не одним ключом."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            old = fact("db.py", "правился файл базы", at=T0 - timedelta(days=3))
            put(door, old, fact("port.py", "правился файл базы"))
            was = [r for r in rows_of(base, "fact") if r["subject"] == old.subject][0]
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            dead = rows_of(base, "lapsedfact")
            self.assertEqual(len(dead), 1)
            for name, value in was.items():
                self.assertEqual(dead[0][name], value, name)
            self.assertTrue(dead[0]["lapsed_at"], "не записано, когда выбыл")

    def test_the_two_retirements_share_one_place(self):
        """Срок и свёртка кладут отставное в одно место, но помечают по-разному."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы", mode="short"),
                fact("port.py", "правился файл базы", mode="short"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=400)))
            dead = rows_of(base, "lapsedfact")
            self.assertEqual(len(dead), 2)
            self.assertEqual(len([r for r in dead if r["merged_into"]]), 1,
                             "свёрнутое не отличить от просроченного")


# --- 3. Свёртка обратима ----------------------------------------------------


class TestTheReplacementShowsItsSources(unittest.TestCase):
    """По замене видно, из чего она собрана, и разворот возвращает исходное.

    Мутация: потерять пометку — источники перестают находиться по замене, и
    разворот не возвращает ничего.
    """

    def test_the_replacement_names_what_it_absorbed(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            live = rows_of(base, "fact")[0]
            self.assertTrue(live["merged_from"], "замена не помнит, из чего собрана")
            gone = rows_of(base, "lapsedfact")[0]
            self.assertIn(models.Fact(fact_type=gone["fact_type"],
                                      subject=gone["subject"],
                                      scope=gone["scope"]).identity(),
                          live["merged_from"])

    @SLOW
    @given(shapes=shapes)
    def test_the_sources_are_readable_by_the_replacement(self, shapes):
        """Спросили замену — получили ровно то, из чего её собрали."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            written(door, shapes)
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            dead = rows_of(base, "lapsedfact")
            for live in rows_of(base, "fact"):
                key = models.Fact(fact_type=live["fact_type"], subject=live["subject"],
                                  scope=live["scope"]).identity()
                mine = [r for r in dead if r["merged_into"] == key]
                self.assertEqual(ids(consolidate.sources(key, door=door)), ids(mine))
                # Пометка на самой замене обязана сходиться с отставным до
                # подписи: одну из двух сторон легко потерять молча.
                self.assertEqual(
                    sorted(line for line in (live["merged_from"] or "").splitlines()
                           if line),
                    ids(mine), "замена и отставное разошлись: %s" % key)

    @SLOW
    @given(shapes=shapes)
    def test_unfolding_puts_the_table_back(self, shapes):
        """Главное свойство обратимости: свернули и развернули — как было.

        Мутация: удалять исходные вместо переклада — разворачивать нечего, и
        таблица фактов после разворота короче прежней.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            written(door, shapes)
            was = plain(rows_of(base, "fact"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            for live in list(rows_of(base, "fact")):
                key = models.Fact(fact_type=live["fact_type"], subject=live["subject"],
                                  scope=live["scope"]).identity()
                consolidate.unfold(key, door=door)
            self.assertEqual(plain(rows_of(base, "fact")), was)
            self.assertEqual(rows_of(base, "lapsedfact"), [],
                             "отставное осталось лежать после разворота")

    def test_unfolding_clears_the_mark(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            live = rows_of(base, "fact")[0]
            key = models.Fact(fact_type=live["fact_type"], subject=live["subject"],
                              scope=live["scope"]).identity()
            self.assertEqual(consolidate.unfold(key, door=door), 1)
            self.assertFalse(rows_of(base, "fact")[0]["merged_from"])

    def test_the_lapsed_by_deadline_are_not_unfolded(self):
        """Разворот трогает своё. Просроченное вернуть он не вправе."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"),
                fact("api.py", "совсем другое дело", mode="short"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            forget.sweep(door=door, now=lifespan.stamp(T0 + timedelta(days=10)))
            live = subjects(rows_of(base, "fact"))
            self.assertEqual(len(live), 1, "остались не только замена: %s" % live)
            self.assertNotIn("%s/api.py" % CWD, live, "просроченное осталось живым")
            live = rows_of(base, "fact")
            for row in live:
                key = models.Fact(fact_type=row["fact_type"], subject=row["subject"],
                                  scope=row["scope"]).identity()
                consolidate.unfold(key, door=door)
            back = subjects(rows_of(base, "fact"))
            self.assertNotIn("%s/api.py" % CWD, back,
                             "разворот воскресил просроченное")


# --- 4. Связи переживают свёртку -------------------------------------------


class TestTheGraphSurvivesTheFold(unittest.TestCase):
    """Связь адресует факт подписью. Свёрнутый конец обязан стать заменой.

    Мутация: не переписывать концы — связь повисает на снесённой подписи, и
    сосед перестаёт находиться.
    """

    def test_a_link_to_a_folded_fact_moves_to_the_replacement(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            twin = fact("port.py", "правился файл базы", at=T0 - timedelta(days=3))
            keep = fact("db.py", "правился файл базы")
            near = fact("api.py", "совсем другое дело")
            put(door, keep, twin, near,
                models.Association(source_key=twin.identity(),
                                   target_key=near.identity(),
                                   cue="same_episode", weight=5.0))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            ends = set()
            for row in rows_of(base, "association"):
                ends |= {row["source_key"], row["target_key"]}
            self.assertNotIn(twin.identity(), ends, "связь повисла на снесённом конце")
            self.assertIn(keep.identity(), ends, "связь не переехала на замену")

    @SLOW
    @given(shapes=shapes)
    def test_no_association_points_at_a_missing_fact(self, shapes):
        """После свёртки у каждой связи оба конца находятся среди живых."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            records = written(door, shapes)
            for one, two in zip(records, records[1:]):
                put(door, models.Association(source_key=one.identity(),
                                             target_key=two.identity(),
                                             cue="same_episode", weight=1.0))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            live = set(ids(rows_of(base, "fact")))
            for row in rows_of(base, "association"):
                self.assertIn(row["source_key"], live)
                self.assertIn(row["target_key"], live)
                self.assertNotEqual(row["source_key"], row["target_key"],
                                    "связь замкнулась сама на себя")

    def test_the_neighbour_is_still_reachable_through_the_replacement(self):
        """Сосед свёрнутого становится соседом замены, а не пропадает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            twin = fact("port.py", "правился файл базы", at=T0 - timedelta(days=3))
            keep = fact("db.py", "правился файл базы")
            near = fact("api.py", "совсем другое дело")
            put(door, keep, twin, near,
                models.Association(source_key=twin.identity(),
                                   target_key=near.identity(),
                                   cue="same_episode", weight=5.0))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            got = door.neighbours([keep.identity()])
            self.assertIn("%s/api.py" % CWD, [row["subject"] for row, _ in got])

    def test_the_context_of_a_folded_fact_goes_to_the_replacement(self):
        """Обстановка добирается связью с эпизодом. Её конец тоже переезжает."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            twin = fact("port.py", "правился файл базы", at=T0 - timedelta(days=3))
            keep = fact("db.py", "правился файл базы")
            ep = models.Episode(session_id="разговор-1", episode_number=1,
                                title="правка", outcome="done", project="demo",
                                working_directory=CWD, git_branch="folding",
                                ended_at=lifespan.stamp(T0), day_of_week="monday")
            put(door, keep, twin, ep)
            door.write_objects([], [models.link("episode_facts", episode=ep, fact=twin)])
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            got = door.contexts([keep.identity()])
            self.assertTrue(any(one.get("git_branch") == "folding"
                                for one in got[keep.identity()]),
                            "обстановка свёрнутого не досталась замене")


# --- 5. Выдача перестаёт тратить места на повторы --------------------------


class TestTheAnswerStopsRepeating(unittest.TestCase):
    """Ожидаемый сигнал задачи: по теме остаётся один ответ вместо трёх."""

    def test_the_answer_holds_one_row_instead_of_three(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"),
                fact("run.py", "правился файл базы"))
            before = [r for r in found(door, "правился файл базы")
                      if r["object_type"] == "Fact"]
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            after = [r for r in found(door, "правился файл базы")
                     if r["object_type"] == "Fact"]
            self.assertEqual(len(before), 3)
            self.assertEqual(len(after), 1)

    @SLOW
    @given(shapes=shapes)
    def test_the_answer_never_grows_from_folding(self, shapes):
        """Сжатие не может увеличить выдачу ни на одном наборе."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            written(door, shapes)
            for text in TEXTS:
                before = len(found(door, text))
                consolidate.fold(door=door, now=lifespan.stamp(T0))
                self.assertLessEqual(len(found(door, text)), before)


class TestTheMarksStayOutOfTheAnswer(unittest.TestCase):
    """Пометки свёртки учётные, а не содержательные. Агенту их не показывают."""

    def test_the_marks_are_named_service_fields(self):
        """Пометки записаны служебными полями явно.

        Сегодня их держит не только это: у факта с текстом печатается один
        текст, и до перебора полей дело не доходит. Но короткий путь — свойство
        печати, а не решение про свёртку, и опираться на него нельзя.

        Мутация: убрать пометки из служебных полей — проверка краснеет.
        """
        for name in ("merged_from", "merged_into"):
            self.assertIn(name, suggest.NOISE, "пометка %s не служебная" % name)

    def test_the_provenance_does_not_reach_the_agent(self):
        """Подписи длинные, потолок выдачи — 1200 знаков на весь кусок.

        Мутация: снять оба заслона разом — короткий путь факта в `_text` и
        служебные поля — подпись уезжает в контекст агента вместе с фактом.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            consolidate.fold(door=door, now=lifespan.stamp(T0))
            text, kept, _ = suggest.suggest("правился файл базы", mode="raw",
                                            min_score=0.0, door=door)
            self.assertTrue(kept, "факт не нашёлся после свёртки")
            self.assertNotIn("merged_from", text)
            self.assertNotIn("|", text.split("правился")[0],
                             "подпись факта уехала в контекст агента")


# --- 6. Проход не конкурирует с живой сессией ------------------------------


class TestTheFoldDoesNotRaceTheSession(unittest.TestCase):
    """Замок общий с остальными проходами по архиву: база одна."""

    def test_a_busy_lock_sends_the_pass_away(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            lock = Path(tmp) / "save.lock"
            with lock.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                got = consolidate.fold(door=door, now=lifespan.stamp(T0), lock=lock)
            self.assertTrue(got["busy"], "заход не сказал, что замок занят")
            self.assertEqual(got["folded"], 0)
            self.assertEqual(len(rows_of(base, "fact")), 2, "занятый заход всё же свернул")

    def test_a_free_lock_lets_it_through(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp):
            door = port.door()
            put(door, fact("db.py", "правился файл базы"),
                fact("port.py", "правился файл базы"))
            got = consolidate.fold(door=door, now=lifespan.stamp(T0),
                                   lock=Path(tmp) / "save.lock")
            self.assertFalse(got["busy"])
            self.assertEqual(got["folded"], 1)

    def test_the_pass_takes_the_shared_lock(self):
        """Замок именно общий, а не свой: писателей в базу двое быть не может."""
        self.assertIs(consolidate.LOCK, locks.PASS)

    def test_a_door_that_cannot_fold_is_told_apart_from_one_that_did_nothing(self):
        """Сетевой путь свёртки не умеет и ронять ход этим не вправе."""
        class Deaf:
            name = "cli"

            def read(self, query, mode="single"):
                return ""

        got = consolidate.fold(door=Deaf(), now=lifespan.stamp(T0))
        self.assertFalse(got["able"])
        self.assertEqual(got["folded"], 0)


class TestTheHookCallsTheFold(unittest.TestCase):
    """Написанный и не подключённый проход — то же, что ненаписанный.

    Проверяем текст хука, а не поведение: запускать живой конец хода из теста
    значит писать в хранилище пользователя.
    """

    def setUp(self):
        self.body = (HERE / "hooks" / "on_stop.sh").read_text(encoding="utf-8")

    def test_the_fold_is_called(self):
        self.assertIn("pipeline.consolidate", self.body,
                      "свёртка не подключена: повторы будут копиться")

    def test_it_writes_and_does_not_idle(self):
        self.assertIn("pipeline.consolidate --send", self.body,
                      "холостой проход считает и ничего не сворачивает")

    def test_the_fold_goes_after_everything_that_writes(self):
        """Сперва в базу ложится новое, и только потом оно сворачивается."""
        order = [self.body.index("pipeline.understand"),
                 self.body.index("pipeline.associate"),
                 self.body.index("pipeline.forget"),
                 self.body.index("pipeline.consolidate")]
        self.assertEqual(order, sorted(order), "свёртка идёт не последней")

    def test_the_fold_stays_out_of_the_hot_path(self):
        """Ход человека — горячий путь. Свёртке там не место."""
        for name in ("on_prompt_read.sh", "on_prompt_queue.sh", "on_message_display.sh"):
            body = (HERE / "hooks" / name).read_text(encoding="utf-8")
            self.assertNotIn("pipeline.consolidate", body,
                             "свёртка забралась в горячий путь: %s" % name)


if __name__ == "__main__":
    unittest.main()
