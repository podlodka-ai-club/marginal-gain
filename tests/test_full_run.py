#!/usr/bin/env python3
"""Прогон целиком одной командой. Запуск: python3 -m unittest tests.test_full_run -v

Флоу замера был описан и разобран на части, но целиком не выполнялся ни разу:
проигрывателя первой сессии в репозитории не было вовсе, а вторая дёргала
подсказку в своём же процессе. Цифра, снятая так, говорит про выдачу памяти, а
не про то, применил ли её агент.

Здесь проверяется сам прогон, а не качество памяти. Свойства такие:

1. Прогон живёт в своей песочнице. Живая база, живая очередь, живая лента и
   живые отметки не открываются ни на запись, ни на чтение — ни при удачном
   исходе, ни при обрыве.
2. База каждого прогона пустая. Знание прошлого прогона во втором не находится,
   и потому два прогона подряд дают одно число.
3. Память наполняется через хуки. Заглуши хуки — и после первой сессии база
   останется пустой; значит записи в неё кладёт ход, а не проигрыватель.
4. Между сессиями сессия сбрасывается. Задача ставится своей сессией, и ни одна
   из них не совпадает с сессиями первого этапа.
5. Задачи ставятся после того, как первый этап осел, а не вперемешку.
6. Каждая пара попадает ровно в один исход, и исходы в сумме дают итог.
7. Каталог, где проигрываются ходы, не должен попадать под отсев служебных
   путей: иначе правки файлов не станут фактами, и прогон честно покажет ноль,
   не назвав причины.
8. Стенд не знает домена. Бытовой набор про еду и дачу проходит тем же кодом,
   что и набор про код, и в самом стенде нет ни слова про наш архив.

Мутации, на которых проверки обязаны краснеть:
  * убрать XMEM_STATE_DIR из окружения песочницы   → TestNothingLivesOutsideTheSandbox
  * не удалять песочницу на обрыве                 → TestTheRunLeavesNothingBehind
  * брать базу прошлого прогона                    → TestTheBaseStartsEmpty
  * класть записи мимо хуков                       → TestMemoryArrivesThroughTheHooks
  * ставить все задачи одной сессией               → TestTheSecondStageAsksClean
  * не ждать фоновую половину хода                 → TestTheQuestionsWaitForTheFirstStage
  * склеить исходы разбивки                        → TestEveryCaseLandsInOneBucket
  * не проверять каталог ходов на служебный путь   → TestThePlayGroundIsNotAServicePath
  * ставить задачу из чужого места                 → TestTheFlowCanActuallyPass
  * завязать стенд на форму нашего набора          → TestTheBenchDoesNotKnowTheDomain
  * не гасить порождённые процессы                 → TestTheRunLeavesNothingBehind
  * порождать детей не своей группой               → TestTheRunLeavesNothingBehind
  * склеивать повторы по содержанию реплики        → TestTheFlowCanActuallyPass
  * принять пару без исхода или без первой сессии  → TestThePairSetIsData
  * снять сторожа живого состояния                 → TestNothingLivesOutsideTheSandbox
  * ждать фон один раз в конце, а не на каждом ходе → TestTheQuestionsWaitForTheFirstStage

Каждая из них прогнана: код ломается точечно, названная проверка краснеет.
Две последние появились как раз оттого, что мутация не покраснела — сторожа
живого состояния заслонял отсев служебных путей, а ожидание на ходу держало
только одно ожидание в конце.
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time, unittest, uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from archive import extract
from eval import live, pairs
from storage import db

HERE = Path(__file__).resolve().parent.parent

SLOW = settings(deadline=None, max_examples=15,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])

# Живое состояние: его не должен трогать ни один прогон.
LIVE_STATE = Path.home() / ".local" / "state" / "memory-encoder"

# Песочницы проверок живут там же, где песочницы прогонов, и по той же причине:
# `/tmp` и `~/.local/state` попадают под отсев служебных путей (extract.NOT_CODE),
# и правка файла в таком каталоге фактом не станет. Проверка, поставленная в
# tempfile, зеленела бы на пустой базе и ничего не значила.
TESTS_ROOT = live.DEFAULT_ROOT.parent / "memory-encoder-eval-tests"


def sandbox_root():
    return TESTS_ROOT / uuid.uuid4().hex[:12]


def names(count):
    return ["mod%d.py" % i for i in range(count)]


def a_set(root, places, times=3):
    """Крошечный набор пар: в первой сессии правят файлы, во второй спрашивают.

    `places` — словарь «место: список имён файлов». `times` — сколько раз это
    сказано, и трижды здесь не для красоты. Мера считает повторы
    (`domain.measure.score_of`): при трёх вхождениях в один день выходит 0.517,
    при двух — 0.467 при пороге 0.5. Набор на двух вхождениях стоит на лезвии:
    он то проходит, то нет, и любая проверка поверх него мигает. Набор, где ни
    одна пара пройти не может, ещё хуже — он зеленит всё, ничего не проверяя;
    предохранитель к этому — TestTheFlowCanActuallyPass.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for place, files in sorted(places.items()):
        items.append({
            "id": "pair-%s" % place,
            "aim": "apply",
            "tell": [{"say": "почини сборку в проекте %s" % place,
                      "place": place, "mark": "main",
                      "touched": ["/home/person/%s/%s" % (place, name)
                                  for name in files]}
                     for _ in range(times)],
            "task": {"say": "Какие файлы правились в проекте %s?" % place},
            "expect": [files[0]], "forbid": [],
        })
    where = root / "pairs.json"
    pairs.dump(where, items)
    return where


def a_load(where):
    return pairs.load(where)[1]


class Base(unittest.TestCase):
    """Общая уборка: песочница проверки уходит вместе с проверкой."""

    def setUp(self):
        self.root = sandbox_root()
        self.sets = self.root.parent / (self.root.name + "-sets")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.sets, ignore_errors=True)


class TestNothingLivesOutsideTheSandbox(Base):
    """Всё состояние прогона уводится в песочницу одной переменной.

    Каталог состояния считали сами десять модулей, каждый своей строкой. Пока
    так, увести прогон в сторону нечем: подмени один путь — остальные девять
    продолжают писать в живое, и молча.
    """

    def test_no_module_names_the_state_dir_past_the_switch(self):
        """Назвал путь по умолчанию — назови и переменную, которая его отменяет.

        Молчаливое исключение здесь стоит дорого: девять модулей продолжают
        писать в живое, а десятый уехал в песочницу, и половина прогона идёт
        мимо другой половины.
        """
        offenders = []
        for layer in ("infra", "domain", "archive", "storage", "pipeline", "eval",
                      "hooks"):
            for path in sorted((HERE / layer).glob("*.py")):
                body = path.read_text(encoding="utf-8")
                names = '"state"' in body and "memory-encoder" in body
                honours = "XMEM_STATE_DIR" in body or "config.state_dir" in body
                if names and not honours:
                    offenders.append("%s/%s" % (layer, path.name))
        self.assertEqual([], offenders,
                         "каталог состояния назван мимо рубильника: %s" % offenders)

    @given(tail=st.text(alphabet="абвde-_0123456789", min_size=1, max_size=12))
    @SLOW
    def test_every_path_of_the_run_lands_inside_the_sandbox(self, tail):
        with live.Sandbox(self.root / tail) as box:
            got = live.paths_in(box.env())
        self.assertTrue(got, "модули не назвали ни одного пути")
        for name, where in got.items():
            self.assertTrue(str(where).startswith(str(box.root)),
                            "%s ведёт наружу песочницы: %s" % (name, where))
            self.assertFalse(str(where).startswith(str(LIVE_STATE)),
                             "%s ведёт в живое состояние: %s" % (name, where))

    def test_the_hooks_read_the_same_sandbox(self):
        with live.Sandbox(self.root) as box:
            out = subprocess.run(
                ["bash", "-c", 'source "%s"; printf "%%s" "$STATE_DIR"'
                 % (HERE / "hooks" / "common.sh")],
                capture_output=True, text=True, env=box.env())
        self.assertEqual(str(box.state), out.stdout.strip())

    def test_the_live_state_dir_is_refused_as_a_sandbox(self):
        """Отказ обязан быть именно про живое состояние, а не про служебный путь.

        Сторожа два, и один заслоняет другого: `~/.local/state/…` попадает и
        под отсев служебных путей. Проверка «упало» проходит и с вырезанным
        сторожем живого состояния — падает-то второй. Поэтому спрашиваем, чем
        отказано, и берём путь, который ловит только первый сторож.
        """
        for where in (LIVE_STATE, LIVE_STATE.parent):
            with self.assertRaises(live.UnsafeRun) as bad:
                live.Sandbox(where).open()
            self.assertIn("живое состояние", str(bad.exception),
                          "%s отказан не как живое состояние: %s"
                          % (where, bad.exception))

    def test_a_directory_holding_the_live_state_is_refused(self):
        """Каталог выше живого состояния: служебным путём он не выглядит вовсе.

        `~/.local` под отсев не попадает — маркер `/.local/state/` в нём не
        встречается, — а снести его прогон уборкой имеет полное право. Ловит
        такой путь только сторож живого состояния, и это единственный случай,
        где видно, что сторож вообще есть.
        """
        above = LIVE_STATE.parent.parent
        self.assertFalse([bad for bad in extract.NOT_CODE
                          if bad in "%s/places/" % above],
                         "путь и так отсеивается как служебный — проверять нечем")
        with self.assertRaises(live.UnsafeRun) as bad:
            live.Sandbox(above).open()
        self.assertIn("живое состояние", str(bad.exception))


class TestThePlayGroundIsNotAServicePath(Base):
    """Каталог ходов не должен попадать под отсев служебных путей.

    Правка файла в `/tmp`, `~/.cache` или `~/.local/state` фактом не становится
    (`extract.NOT_CODE`). Поставь песочницу туда — и прогон покажет ноль, не
    назвав причины: набор непроходим по построению, а выглядит как слабая
    память.
    """

    def test_the_default_ground_survives_the_sift(self):
        ground = str(live.DEFAULT_ROOT / "runid" / "projects" / "demo" / "db.py")
        self.assertFalse([bad for bad in extract.NOT_CODE if bad in ground],
                         "каталог прогона по умолчанию отсеивается как служебный")

    @given(bad=st.sampled_from(extract.NOT_CODE))
    @SLOW
    def test_a_service_path_is_refused_loudly(self, bad):
        where = Path("/nowhere%sруны" % bad)
        with self.assertRaises(live.UnsafeRun):
            live.Sandbox(where).open()


class TestEveryCaseLandsInOneBucket(unittest.TestCase):
    """Разбивка обязана объяснять итог целиком, а не почти целиком."""

    rows = st.lists(st.fixed_dictionaries({
        "ok": st.booleans(),
        "injected": st.booleans(),
        "intruded": st.booleans(),
        "reason": st.sampled_from([None, "not_found", "incidental", "below_threshold",
                                   "over_budget", "backend_error", "overdue",
                                   "pipeline_error", "disabled"]),
        "error": st.sampled_from([None, "", "упало"]),
    }), min_size=0, max_size=40)

    @given(rows=rows)
    @SLOW
    def test_the_buckets_add_up_to_the_total(self, rows):
        counted = live.tally(rows)
        self.assertEqual(sorted(counted), sorted(live.BUCKETS))
        self.assertEqual(len(rows), sum(counted.values()))

    @given(rows=rows)
    @SLOW
    def test_each_row_gets_exactly_one_bucket(self, rows):
        for row in rows:
            got = [name for name in live.BUCKETS if live.bucket(row) == name]
            self.assertEqual(1, len(got), "строка попала в %d корзин: %s" % (len(got), row))

    @given(rows=rows)
    @SLOW
    def test_a_passed_case_is_never_called_a_loss(self, rows):
        for row in rows:
            if row["ok"] and not row["error"] and not row["intruded"]:
                self.assertIn(live.bucket(row), (live.APPLIED, live.COINCIDED))

    @given(reason=st.sampled_from([None, "not_found", "below_threshold",
                                   "over_budget", "incidental"]))
    @SLOW
    def test_a_pass_on_a_silent_memory_is_not_memorys_win(self, reason):
        """Ответ сошёлся, а память промолчала — это совпадение, а не память.

        Случай не выдуман: на первом же живом прогоне агент написал в список
        покупок овсянку просто потому, что овсянка — обычный завтрак. Память
        при этом не сказала ничего (`not_found`). Засчитай это применением — и
        цифра растёт от того, что вопросы набора угадываются без памяти, а
        корзина «ничего не нашла» пустеет ровно на эти случаи.
        """
        row = {"ok": True, "injected": False, "reason": reason,
               "intruded": False, "error": None}
        self.assertEqual(live.COINCIDED, live.bucket(row))

    @given(reason=st.sampled_from([None, "not_found", "below_threshold"]))
    @SLOW
    def test_a_pass_with_a_hint_is_memorys_win(self, reason):
        row = {"ok": True, "injected": True, "reason": reason,
               "intruded": False, "error": None}
        self.assertEqual(live.APPLIED, live.bucket(row))

    @given(reason=st.sampled_from([None, "not_found", "below_threshold", "over_budget"]))
    @SLOW
    def test_what_memory_gave_is_never_called_a_miss(self, reason):
        """Отдала и не помогло — это не «ничего не нашла» и не «срезали».

        Разница между ними и есть весь смысл разбивки: одна корзина говорит
        «памяти нечего было сказать», другая — «сказала, а не помогло». Слей их
        — и итог снова становится единственным числом.
        """
        row = {"ok": False, "injected": True, "reason": reason,
               "intruded": False, "error": None}
        self.assertEqual(live.UNUSED, live.bucket(row))

    @given(reason=st.sampled_from(live.CUT_REASONS))
    @SLOW
    def test_a_cut_is_never_called_an_empty_memory(self, reason):
        row = {"ok": False, "injected": False, "reason": reason,
               "intruded": False, "error": None}
        self.assertEqual(live.CUT, live.bucket(row))

    @given(reason=st.sampled_from([None, "not_found", "backend_error", "overdue",
                                   "pipeline_error", "disabled"]))
    @SLOW
    def test_silence_without_a_find_is_an_empty_memory(self, reason):
        row = {"ok": False, "injected": False, "reason": reason,
               "intruded": False, "error": None}
        self.assertEqual(live.NOT_FOUND, live.bucket(row))

    @given(ok=st.booleans(), injected=st.booleans(),
           reason=st.sampled_from([None, "not_found", "below_threshold"]))
    @SLOW
    def test_dragged_in_memory_is_never_a_win(self, ok, injected, reason):
        """Приплела лишнее — исход отрицательный, даже если нужное тоже сказано.

        Слей это с удачей — и отрицательные пары перестанут стоить чего-либо:
        память, тянущая в ответ всё подряд, набирала бы очки.
        """
        row = {"ok": ok, "injected": injected, "reason": reason,
               "intruded": True, "error": None}
        self.assertEqual(live.INTRUDED, live.bucket(row))

    @given(ok=st.booleans(), injected=st.booleans())
    @SLOW
    def test_a_broken_turn_is_never_counted_as_memory(self, ok, injected):
        row = {"ok": ok, "injected": injected, "reason": None,
               "intruded": False, "error": "упало"}
        self.assertEqual(live.BROKEN, live.bucket(row))


class TestTheRunGoesEndToEnd(Base):
    """Один вызов проходит оба этапа и печатает цифру."""

    def test_the_command_prints_a_number(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        out = live.main(["--pairs", str(cases),
                         "--player", "replay", "--root", str(self.root)])
        self.assertEqual(0, out)

    def test_the_report_names_the_player(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        self.assertIn("replay", report.text())
        self.assertEqual(1, report.total)
        for name in live.RESERVED:
            self.assertIn(name, report.text(),
                          "исход, который мы не меряем, пропал из отчёта молча")

    def test_the_first_stage_fills_the_base(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root, keep=True)
        self.addCleanup(shutil.rmtree, report.root, ignore_errors=True)
        repo = db.Repository(report.root / "memory.db")
        try:
            self.assertTrue(repo.search("альфа"),
                            "после первого этапа в базе пусто")
        finally:
            repo.close()


class TestTheFlowCanActuallyPass(Base):
    """Прогон, у которого не проходит ничего, зеленит всё остальное впустую.

    Пока набор непроходим по построению — файл правили однажды, вопрос задан
    из чужого каталога, — любая проверка «два прогона дают одно число» проходит
    на двух нулях. Эта проверка стоит здесь как предохранитель ко всем
    остальным в файле.
    """

    def test_a_filled_memory_answers_its_own_question(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        self.assertEqual(1, report.passed,
                         "случай не прошёл даже на наполненной памяти: %s"
                         % [(r["reason"], r["injected"]) for r in report.asked])

    def test_the_question_is_asked_from_the_project_it_is_about(self):
        """Иначе уместность срежет всё, и прогон покажет «нашла, срезали» везде."""
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root, keep=True)
        self.addCleanup(shutil.rmtree, report.root, ignore_errors=True)
        self.assertEqual(2, report.passed,
                         "вопрос задан не из своего проекта: %s"
                         % [(r["id"], r["reason"]) for r in report.asked])


class TestMemoryArrivesThroughTheHooks(Base):
    """Записи кладёт ход, а не проигрыватель.

    Заглуши хуки — и база останется пустой. Если бы проигрыватель писал в базу
    сам, эта проверка прошла бы с полной базой и ничего бы не значила.
    """

    def test_a_silenced_contour_leaves_the_base_empty(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root, keep=True, live_hooks=False)
        self.addCleanup(shutil.rmtree, report.root, ignore_errors=True)
        repo = db.Repository(report.root / "memory.db")
        try:
            self.assertEqual([], repo.search("альфа"),
                             "хуки заглушены, а база наполнилась мимо них")
        finally:
            repo.close()

    def test_the_same_run_with_live_hooks_fills_it(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root, keep=True, live_hooks=True)
        self.addCleanup(shutil.rmtree, report.root, ignore_errors=True)
        repo = db.Repository(report.root / "memory.db")
        try:
            self.assertTrue(repo.search("альфа"))
        finally:
            repo.close()


class TestTheBaseStartsEmpty(Base):
    """Прогон не видит того, что положил прошлый."""

    def test_a_second_run_does_not_inherit_the_first(self):
        first = a_set(self.sets, {"альфа": names(2)})
        # Второй прогон ставит ту же задачу и из того же места, но своей первой
        # сессии у него нет: подсказать ему может только то, что осталось от
        # первого. Место называем явно — иначе задача задавалась бы из ниоткуда
        # и молчала бы независимо от того, чистая база или нет.
        empty = [dict(pair, tell=[], aim="avoid",
                      task=dict(pair["task"], place="альфа"))
                 for pair in a_load(first)]

        live.run(pairs=a_load(first), player="replay", root=self.root)
        report = live.run(pairs=empty, player="replay", root=self.root)
        self.assertEqual([], [row for row in report.asked if row["injected"]],
                         "второй прогон получил подсказку из базы первого")

    def test_the_probe_would_notice_a_dirty_base(self):
        """Обратная проверка к предыдущей: пара-щуп умеет заметить полную базу.

        Без неё «второй прогон ничего не получил» доказывает только то, что щуп
        молчит всегда — например, потому что задаёт задачу из ниоткуда.
        """
        cases = a_set(self.sets, {"альфа": names(2)})
        filling = a_load(cases)
        probe = [dict(pair, id=pair["id"] + "-щуп", tell=[], aim="avoid",
                      task=dict(pair["task"], place="альфа"))
                 for pair in filling]
        report = live.run(pairs=filling + probe, player="replay", root=self.root)
        asked = {row["id"]: row for row in report.asked}
        self.assertTrue([row for name, row in asked.items()
                         if name.endswith("-щуп") and row["injected"]],
                        "щуп молчит и на полной базе — проверять им нечего")

    def test_two_runs_in_a_row_give_one_number(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        one = live.run(pairs=a_load(cases), player="replay", root=self.root)
        two = live.run(pairs=a_load(cases), player="replay", root=self.root)
        self.assertEqual(one.passed, two.passed)
        self.assertEqual(one.text(), two.text())


class TestTheSecondStageAsksClean(Base):
    """Каждый вопрос идёт своей сессией, и ни одна не пришла с первого этапа."""

    def test_no_question_reuses_a_session(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        asked = [row["session_id"] for row in report.asked]
        self.assertEqual(len(asked), len(set(asked)), "вопросы делят сессию")

    def test_the_questions_do_not_reuse_the_played_sessions(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        played = {row["session_id"] for row in report.played}
        asked = {row["session_id"] for row in report.asked}
        self.assertTrue(played, "первый этап не сыграл ни одного хода")
        self.assertEqual(set(), played & asked,
                         "вопрос задан сессией первого этапа")


class TestTheQuestionsWaitForTheFirstStage(Base):
    """Порядок этапов держится, а не складывается случайно."""

    def test_every_played_turn_comes_before_every_question(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        kinds = [row["stage"] for row in report.trail]
        self.assertEqual(sorted(kinds, key=lambda k: k != "play"), kinds,
                         "вопросы перемешались с ходами: %s" % kinds)

    def test_the_run_waits_for_the_background_half(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        self.assertTrue(report.settled_at, "прогон не дождался фоновой половины")
        first = min(row["at"] for row in report.asked)
        self.assertLessEqual(report.settled_at, first,
                             "вопрос задан раньше, чем осела запись хода")

    def test_every_played_turn_waits_for_its_own_background_half(self):
        """Ждать один раз в конце первого этапа мало.

        Проверка выше держит только последнее ожидание: вырежи ожидание внутри
        цикла — она всё равно зеленеет, потому что перед вторым этапом прогон
        ждёт ещё раз. А без ожидания на ходу следующая реплика стартует, пока
        предыдущая ещё пишет: база одна и открыта без журнала упреждающей
        записи, писателей двое.

        Смотрим не на счётчик вызовов, а на их чередование: за каждым ходом
        первого этапа обязано идти своё ожидание, и только потом следующий ход.
        Настоящие ход и ожидание при этом выполняются — обёртка их не подменяет.
        """
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2),
                                  "гамма": names(2)})
        items = a_load(cases)
        turns = len(live.script_of(items))
        self.assertGreater(turns, 1, "на одном ходе разницы не видно")

        order = []
        real_wait, real_play = live.settled, live.Replay.play

        def watched_wait(*a, **kw):
            order.append("ждём")
            return real_wait(*a, **kw)

        def watched_play(self_, *a, **kw):
            order.append("ход")
            return real_play(self_, *a, **kw)

        live.settled = watched_wait
        live.Replay.play = watched_play
        try:
            report = live.run(pairs=items, player="replay", root=self.root)
        finally:
            live.settled = real_wait
            live.Replay.play = real_play

        self.assertEqual(turns, len(report.played))
        # Первый этап: ход, ожидание, ход, ожидание… и одно ожидание перед
        # вторым этапом. Дальше идут только задачи, им ждать нечего.
        head = order[:turns * 2 + 1]
        self.assertEqual(["ход", "ждём"] * turns + ["ждём"], head,
                         "ход и ожидание не чередуются: %s" % head)
        self.assertNotIn("ждём", order[turns * 2 + 1:],
                         "ожидание затесалось во второй этап: %s" % order)


class TestTheBenchDoesNotKnowTheDomain(Base):
    """Стенд принимает пары как данные и не знает, о чём они.

    Требование прямое: подставили бытовой набор — работает, подставили набор
    про код — работает. Проверяется обоими способами: формой (в самом прогоне
    нет имён нашего набора) и делом (бытовой набор проходит тем же кодом).
    """

    HOUSEHOLD = HERE / "eval-pairs-example.json"

    def test_the_run_does_not_name_our_set(self):
        body = (HERE / "eval" / "live.py").read_text(encoding="utf-8")
        for word in ("eval-cases", "eval-script", "goldenset"):
            self.assertNotIn(word, body,
                             "прогон знает про наш набор по имени: %s" % word)

    def test_only_the_bridge_knows_the_old_shape(self):
        """Форму старого набора знает мост и только он."""
        body = (HERE / "eval" / "pairs.py").read_text(encoding="utf-8")
        self.assertIn("eval-cases", body)
        for name in sorted((HERE / "eval").glob("*.py")):
            if name.name in ("pairs.py", "goldenset.py", "evaluate.py", "matrix.py",
                             "holdout.py", "__init__.py"):
                continue
            self.assertNotIn("eval-cases", name.read_text(encoding="utf-8"),
                             "%s знает форму старого набора" % name.name)

    def test_a_household_set_runs_through_the_same_code(self):
        items = pairs.load(self.HOUSEHOLD)[1]
        report = live.run(pairs=items, player="replay", root=self.root)
        self.assertEqual(len(items), report.total)
        for row in report.asked:
            self.assertIn(live.bucket(row), live.BUCKETS)
        self.assertTrue(any(row["injected"] for row in report.asked),
                        "бытовому набору память не сказала вообще ничего: %s"
                        % [(r["id"], r["reason"]) for r in report.asked])

    def test_a_household_set_carries_its_own_negative_pair(self):
        items = pairs.load(self.HOUSEHOLD)[1]
        self.assertTrue([pair for pair in items if pair["aim"] == "avoid"],
                        "в примере нет отрицательной пары — проверять нечего")


class TestThePairSetIsData(unittest.TestCase):
    """Набор пар — вход, а не часть кода. Форма проверяется, домен не знается."""

    said = st.text(alphabet="абвгде ", min_size=1, max_size=20).filter(lambda s: s.strip())

    pair = st.builds(
        lambda ident, aim, tell, task, expect, forbid: {
            "id": ident, "aim": aim,
            "tell": [{"say": one} for one in tell],
            "task": {"say": task},
            "expect": expect, "forbid": forbid},
        ident=st.text(alphabet="abc-0123456789", min_size=1, max_size=8),
        aim=st.sampled_from(pairs.AIMS),
        tell=st.lists(said, min_size=1, max_size=3),
        task=said,
        expect=st.lists(said, min_size=1, max_size=2),
        forbid=st.lists(said, max_size=2))

    @given(item=pair)
    @SLOW
    def test_a_well_formed_pair_survives_a_round_trip(self, item):
        where = Path(tempfile.mkdtemp(prefix="xmem-pairs-")) / "pairs.json"
        try:
            pairs.dump(where, [item])
            meta, got = pairs.load(where)
            self.assertEqual([item], got)
            self.assertEqual(pairs.VERSION, meta["version"])
        finally:
            shutil.rmtree(where.parent, ignore_errors=True)

    @given(item=pair, drop=st.sampled_from(["id", "aim", "task"]))
    @SLOW
    def test_a_pair_without_its_parts_is_refused(self, item, drop):
        broken = {k: v for k, v in item.items() if k != drop}
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(broken)

    @given(item=pair)
    @SLOW
    def test_a_pair_without_a_verdict_is_refused(self, item):
        """Ни expect, ни forbid — исход не определён, и такую пару читать нельзя."""
        with self.assertRaises(pairs.PairSetError):
            pairs.validate(dict(item, expect=[], forbid=[]))

    @given(item=pair)
    @SLOW
    def test_only_a_negative_pair_may_have_no_first_session(self, item):
        """Применить то, чего никто не говорил, нельзя — и такая пара непроходима."""
        if item["aim"] == "avoid":
            pairs.validate(dict(item, tell=[]))
        else:
            with self.assertRaises(pairs.PairSetError):
                pairs.validate(dict(item, tell=[]))


class TestTheRunLeavesNothingBehind(Base):
    """Ни удачный прогон, ни оборванный не оставляют за собой ничего."""

    def leaked(self):
        """Следы прогона в живом состоянии. Ищем по пути его песочницы.

        Снимок «файл — размер» тут не годится: живой контур пользователя пишет
        в тот же каталог всё время, пока идёт проверка, и снимок расходится сам
        собой. Путь песочницы уникален для прогона, поэтому его появление в
        живой базе, ленте или журнале — это утечка, а чужие записи мимо.
        """
        needle = str(self.root).encode("utf-8")
        found = []
        if not LIVE_STATE.exists():
            return found
        for path in sorted(LIVE_STATE.rglob("*")):
            if not path.is_file():
                continue
            tail = b""
            try:
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        if needle in tail + chunk:
                            found.append(path.name)
                            break
                        tail = chunk[-len(needle):]
            except OSError:
                continue
        return found

    def test_closing_the_sandbox_kills_what_it_spawned(self):
        """Ни одного живого процесса за собой. Прямо, а не по следам на диске.

        Конец хода уходит в фон и переживает и хук, и агента. По каталогу это
        видно через раз — цепочка успевает дописать раньше, чем мы смотрим, —
        поэтому спрашиваем сам процесс: жив он после уборки или нет.
        """
        box = live.Sandbox(self.root).open()
        proc = box.spawn(["sleep", "120"])
        self.assertIsNone(proc.poll(), "процесс не запустился")
        box.close()
        deadline = time.time() + 15
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(proc.poll(),
                             "прогон убрался, а порождённый им процесс жив")

    def test_a_finished_run_removes_its_sandbox(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        self.assertFalse(report.root.exists(), "песочница осталась на диске")
        self.assertEqual([], self.leaked(), "прогон наследил в живом состоянии")

    def test_an_interrupted_run_removes_its_sandbox(self):
        cases = a_set(self.sets, {"альфа": names(3), "бета": names(3),
                                          "гамма": names(3)})
        env = dict(os.environ, PYTHONPATH=str(HERE))
        proc = subprocess.Popen(
            [sys.executable, "-m", "eval.live", "--pairs", str(cases),
             "--player", "replay", "--root", str(self.root)],
            cwd=str(HERE), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        # Рвём не раньше, чем пошла фоновая половина хода: именно она
        # переживает обрыв и заводит песочницу заново. Порви прогон до неё — и
        # убирать будет нечего, а проверка пройдёт на пустом месте. Признак
        # того, что цепочка стартовала, — журнал конца хода.
        started = self.root / "state" / "save.log"
        deadline = time.time() + 90
        while time.time() < deadline and not started.exists():
            time.sleep(0.05)
        self.assertTrue(started.exists(), "фоновая половина хода так и не пошла")
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=60)
        # Ждём после выхода, а не проверяем сразу. Каталог, снесённый в момент,
        # когда фоновая цепочка ещё жива, возвращается через секунду-другую: её
        # первая же запись заводит его заново. Проверка сразу после выхода этого
        # не видит и проходит на прогоне, который ничего не погасил.
        time.sleep(6)
        self.assertFalse(self.root.exists(), "обрыв оставил песочницу на диске")
        self.assertEqual([], self.leaked(), "обрыв наследил в живом состоянии")

    def test_no_transcript_of_the_run_stays_in_the_archive(self):
        cases = a_set(self.sets, {"альфа": names(2)})
        report = live.run(pairs=a_load(cases), player="replay",
                          root=self.root)
        talks = {row["session_id"] for row in report.played + report.asked}
        left = [talk for talk in talks
                if list(live.TRANSCRIPTS.rglob("%s.jsonl" % talk))]
        self.assertEqual([], left, "разговоры прогона остались в архиве: %s" % left)


if __name__ == "__main__":
    unittest.main()
