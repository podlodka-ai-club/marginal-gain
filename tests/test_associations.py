#!/usr/bin/env python3
"""Граф связей между фактами: карточка Association и её концы.

Запуск: python3 -m unittest tests.test_associations -v

Схема описывает связь между фактами с версии, где появился объект
`Association`. Карточек в хранилище было ноль: порождать их было нечем, и всё,
что мы говорим про граф — всплытие по зацепке, распространение меток,
транзитивность, — не проверялось ничем.

Правило первой версии принято ресёрчем и здесь не выбирается заново: два повода
из пяти. `same_episode` с потолком в восемь фактов на эпизод (без потолка
дюжина самых длинных эпизодов даёт почти половину всех пар) и `error_then_fix` —
направленный и редкий. `same_project` отвергнут: он давал бы 88% графа и
ранжировал хуже случайного, а как фильтр он и так лежит в `scope`.

Свойствами, а не примерами: правило порождает пары комбинаторно, и руками
краёв не перечислить — потолок, повтор пары в разных эпизодах, направленность
редкого повода, идемпотентность повторного прохода.
"""
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import models
from pipeline import associate, understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "associations"

SLOW = settings(deadline=None, max_examples=20,
                suppress_health_check=[HealthCheck.too_slow])

# Просьбы: первые три попадают в темы предпочтений, последняя — ни в одну.
REQUESTS = st.sampled_from([
    "Отвечай кратко, длинные ответы не читаю",
    "Не выдумывай, перепроверь по документации",
    "Задавай вопросы по одному",
    "Посмотри, что там с базой",
])

NAMES = ["db.py", "port.py", "run.py", "api.py", "cli.py",
         "save.py", "marks.py", "eval.py", "hooks.py", "scrub.py"]

EPISODES = st.builds(
    lambda request, names, reply, error: {
        "request": request, "names": names, "reply": reply, "error": error},
    request=REQUESTS,
    names=st.lists(st.sampled_from(NAMES), max_size=6, unique=True),
    reply=st.sampled_from(["Готово.", "Готово. Адрес https://example.org/db"]),
    error=st.sampled_from(["", "FileNotFoundError: db.py"]))

ARCHIVES = st.lists(st.tuples(st.sampled_from(["разговор-1", "разговор-2"]),
                              st.lists(EPISODES, min_size=1, max_size=4)),
                    min_size=1, max_size=2, unique_by=lambda item: item[0])


def rows(session, specs):
    out = []
    for number, spec in enumerate(specs):
        stamp = "2026-08-28T%02d:00:00Z" % (number % 24)
        head = {"sessionId": session, "timestamp": stamp, "cwd": CWD,
                "gitBranch": BRANCH}
        out.append(dict(head, type="user", message={"content": spec["request"]}))
        blocks = [{"type": "tool_use", "name": "Edit",
                   "input": {"file_path": "%s/%s" % (CWD, name)}}
                  for name in spec["names"]]
        if spec["error"]:
            blocks.append({"type": "tool_result", "is_error": True,
                           "content": spec["error"]})
        blocks.append({"type": "text", "text": spec["reply"]})
        out.append(dict(head, type="assistant", message={"content": blocks}))
    return out


def archive(root, shape):
    out = []
    for number, (session, specs) in enumerate(shape):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        with path.open("a", encoding="utf-8") as fh:
            for line in rows(session, specs):
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.append(path)
    return out


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


def cards_in(base):
    """Карточки связей из базы: ключ -> строка.

    Базы может не быть вовсе: её заводит первая запись, а проход по архиву без
    фактов не пишет ничего. Пустой граф и незаведённая база — одно и то же.
    """
    if not Path(base).exists():
        return {}
    conn = sqlite3.connect(str(base))
    try:
        conn.row_factory = sqlite3.Row
        return {(r["source_key"], r["target_key"], r["cue"]): dict(r)
                for r in conn.execute("SELECT * FROM association")}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def links_of(base, relation):
    conn = sqlite3.connect(str(base))
    try:
        found = {}
        for link_id, role, kind, key in conn.execute(
                "SELECT link_id, role, object_type, object_key FROM links "
                "WHERE relation = ?", (relation,)):
            found.setdefault(link_id, {})[role] = (kind, json.loads(key))
        return found
    finally:
        conn.close()


def facts_in(base):
    if not Path(base).exists():
        return set()
    conn = sqlite3.connect(str(base))
    try:
        return {"%s|%s|%s" % row for row in conn.execute(
            "SELECT fact_type, subject, scope FROM fact")}
    finally:
        conn.close()


def run(files, **kwargs):
    """Понимание, а следом проход по связям: карточка ссылается на факты."""
    understand.digest(files, door=port.door(), dry=False)
    kwargs.setdefault("dry", False)
    return associate.build(files, door=port.door(), **kwargs)


class TestTheGraphIsFilled(unittest.TestCase):
    """Карточек больше нуля, и их число сходится с числом найденных поводов."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_pass_writes_exactly_what_it_counted(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = run(archive(tmp, shape))
            self.assertEqual(len(cards_in(base)), got["cards"])

    def test_a_plain_archive_leaves_cards_behind(self):
        """Обычный разговор из двух эпизодов даёт непустой граф."""
        shape = [("разговор-1", [
            {"request": "Посмотри, что там с базой", "names": ["db.py", "port.py"],
             "reply": "Готово.", "error": ""},
            {"request": "Отвечай кратко, длинные ответы не читаю",
             "names": ["run.py"], "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = run(archive(tmp, shape))
            self.assertGreater(got["cards"], 0)
            self.assertGreater(len(cards_in(base)), 0, "граф остался пустым")

    @SLOW
    @given(shape=ARCHIVES)
    def test_every_cue_belongs_to_the_schema(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            for (_, _, cue) in cards_in(base):
                self.assertIn(cue, models.CUES)


class TestOnePairMakesOneCard(unittest.TestCase):
    """Пара фактов и повод — это одна карточка, сколько бы раз ни встретилась."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_same_pair_never_makes_two_cards(self, shape):
        """Порядок концов канонический: иначе А—Б и Б—А это две карточки."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            seen = set()
            for (source, target, cue) in cards_in(base):
                if cue == "same_episode":
                    self.assertLess(source, target, "концы не упорядочены")
                self.assertNotIn((source, target, cue), seen)
                seen.add((source, target, cue))

    @SLOW
    @given(shape=ARCHIVES)
    def test_a_second_pass_adds_nothing(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = archive(tmp, shape)
            run(files)
            was = cards_in(base)
            run(files)
            now = cards_in(base)
            self.assertEqual(set(now), set(was), "повтор прохода наплодил карточек")
            for key, row in now.items():
                self.assertEqual(row["weight"], was[key]["weight"],
                                 "вес поехал на повторном проходе")

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_weight_counts_how_often_the_pair_was_seen(self, shape):
        """Вес — сколько раз повод наблюдался. Ноль весом быть не может."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            for row in cards_in(base).values():
                self.assertGreaterEqual(row["weight"], 1)


class TestTheCeilingBoundsOneEpisode(unittest.TestCase):
    """Потолок на эпизод. Без него длинные эпизоды заполняют граф собой."""

    @SLOW
    @given(count=st.integers(min_value=0, max_value=len(NAMES)))
    def test_an_episode_gives_exactly_the_pairs_the_ceiling_allows(self, count):
        """Эпизод из N правок даёт C(min(N, 8), 2) пар — не больше и не меньше."""
        shape = [("разговор-1", [{"request": "Посмотри, что там с базой",
                                  "names": NAMES[:count], "reply": "Готово.",
                                  "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            same = [k for k in cards_in(base) if k[2] == "same_episode"]
            # Число названо здесь, а не выведено из константы модуля: выведи мы
            # его из CEILING — проверка ослабла бы вместе с ней, и снятый
            # потолок прошёл бы зелёным. Восемь фактов дают 28 пар.
            taken = min(count, 8)
            self.assertEqual(len(same), taken * (taken - 1) // 2)


class TestBothEndsAreRealFacts(unittest.TestCase):
    """Конец связи — факт, который лежит в хранилище, а не выдуманный ключ."""

    @SLOW
    @given(shape=ARCHIVES)
    def test_every_card_points_at_written_facts(self, shape):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            known = facts_in(base)
            if not known:
                # Эпизод без единого факта — такой же край, как эпизод с ними:
                # связывать нечего, и карточек быть не должно.
                self.assertEqual(cards_in(base), {})
                return
            for (source, target, _) in cards_in(base):
                self.assertIn(source, known)
                self.assertIn(target, known)

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_card_is_tied_to_both_facts(self, shape):
        """Связь association_fact_link: карточка и оба её конца."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            found = links_of(base, "association_fact_link")
            self.assertEqual(len(found), len(cards_in(base)))
            for ends in found.values():
                self.assertEqual(set(ends), {"association", "source_fact", "target_fact"})


class TestTheRareCueKeepsItsDirection(unittest.TestCase):
    """error_then_fix направлен: сперва упёрлись, потом починили."""

    def test_the_obstacle_is_the_source_and_the_edit_is_the_target(self):
        shape = [("разговор-1", [
            {"request": "Посмотри, что там с базой", "names": [],
             "reply": "Пока не вышло.", "error": "FileNotFoundError: db.py"},
            {"request": "Посмотри, что там с базой", "names": ["db.py"],
             "reply": "Готово.", "error": ""}])]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            rare = {k: v for k, v in cards_in(base).items() if k[2] == "error_then_fix"}
            self.assertTrue(rare, "редкий повод не сработал ни разу")
            for (source, target, _) in rare:
                self.assertIn("препятствие", source_content(base, source))
                self.assertIn("правился файл", source_content(base, target))

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_rare_cue_is_about_work_only(self, shape):
        """Оба конца — состояние проекта: предпочтениям и адресам тут не место."""
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            for (source, target, cue) in cards_in(base):
                if cue == "error_then_fix":
                    self.assertTrue(source.startswith("project_state|"), source)
                    self.assertTrue(target.startswith("project_state|"), target)

    @SLOW
    @given(shape=ARCHIVES)
    def test_the_cue_never_ties_a_fact_to_itself(self, shape):
        """Петля бессмысленна: факт не чинит сам себя.

        Обратную карточку, в отличие от петли, запрещать нельзя: сегодня
        упёрлись в А и починили Б, через неделю упёрлись в Б и починили А —
        это два разных наблюдения, и порядок концов у повода как раз и хранит,
        что за чем шло.
        """
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            run(archive(tmp, shape))
            for (source, target, cue) in cards_in(base):
                self.assertNotEqual(source, target)


def source_content(base, identity):
    kind, subject, scope = identity.split("|")
    conn = sqlite3.connect(str(base))
    try:
        row = conn.execute("SELECT content FROM fact WHERE fact_type=? AND subject=? "
                           "AND scope=?", (kind, subject, scope)).fetchone()
    finally:
        conn.close()
    return (row or [""])[0] or ""


class TestASignatureIsParsedBackWhateverСontainsIt(unittest.TestCase):
    """Подпись разбирается обратно, даже если в теме есть разделитель.

    Тема размеченного факта — свободный текст модели, и `|` в нём вполне
    реален. Разбор слева направо уводил тему в охват, схема браковала значение,
    и весь проход падал уже после обхода архива — вся работа терялась.
    Крайние поля подписи это закрытые перечисления, поэтому режем с краёв.
    """

    def test_a_subject_with_a_separator_survives_the_round_trip(self):
        fact = models.Fact(fact_type="project_state", subject="а|б|в",
                           scope="project", content="что-то")
        back = associate.fact_of(fact.identity())
        self.assertEqual((back.fact_type, back.subject, back.scope),
                         ("project_state", "а|б|в", "project"))
        back._validate_key()

    def test_the_pass_does_not_die_on_such_a_fact(self):
        graph = {("project_state|а|б|project", "project_state|в|project",
                  "same_episode"): {"weight": 1, "first": "", "last": ""}}
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            associate.deliver(sorted(graph.items()), port.door())
            self.assertEqual(len(cards_in(base)), 1)


class TestAnEmptyArchiveIsNotAFailure(unittest.TestCase):
    """Архив без фактов — обычное дело, а не повод падать."""

    def test_no_facts_no_cards(self):
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            got = run([])
            self.assertEqual(got["cards"], 0)
            self.assertEqual(cards_in(base), {})


if __name__ == "__main__":
    unittest.main()
