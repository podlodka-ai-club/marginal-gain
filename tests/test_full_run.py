#!/usr/bin/env python3
"""Прогон целиком одной командой. Запуск: python3 -m pytest tests/test_full_run.py

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

Живой стенд стоит десятки секунд на проверку, и держать его в обычном прогоне
нельзя: батарея шла больше десяти минут и потому не гонялась вовсе. Стенд
поднимают ровно три проверки, помеченные `slow`; по умолчанию они не
собираются, включаются ключом:

    XMEM_SLOW=1 python3 -m pytest -q -m slow

Ворота живут в `conftest.py`, а его знает только pytest. Через
`python3 -m unittest` пометка не действует и стенд поднимется весь — команду
запуска этого файла поэтому называем через pytest.

Каждая из трёх собрана из нескольких прежних: одно поднятие стенда несёт все
утверждения, ради которых прежде поднимали его же по разу. Остальное
проверяется без стенда — по разбору кода, по настройкам песочницы и по чистым
функциям разбивки.

Мутации, на которых проверки обязаны краснеть:
  * убрать XMEM_STATE_DIR из окружения песочницы   → TestNothingLivesOutsideTheSandbox
  * брать базу прошлого прогона                    → TestTheBaseStartsEmpty
  * класть записи мимо хуков                       → TestMemoryArrivesThroughTheHooks
  * ставить все задачи одной сессией               → TestMemoryArrivesThroughTheHooks
  * не ждать фоновую половину хода                 → TestMemoryArrivesThroughTheHooks
  * склеить исходы разбивки                        → TestEveryCaseLandsInOneBucket
  * не проверять каталог ходов на служебный путь   → TestThePlayGroundIsNotAServicePath
  * ставить задачу из чужого места                 → TestTheBaseStartsEmpty
  * завязать стенд на форму нашего набора          → TestTheBenchDoesNotKnowTheDomain
  * не гасить порождённые процессы                 → TestTheRunLeavesNothingBehind
  * склеивать повторы по содержанию реплики        → TestTheBaseStartsEmpty
  * сделать цифру зависящей от прошлого прогона    → TestTheBaseStartsEmpty
  * увести ожидание из первого этапа во второй     → TestTheWaitSitsInsideTheLoop
  * запереть срок ожидания без ключа наружу        → TestTheWaitGivesUpOnTime
  * принять пару без исхода или без первой сессии  → TestThePairSetIsData
  * снять сторожа живого состояния                 → TestNothingLivesOutsideTheSandbox
  * ждать фон один раз в конце, а не на каждом ходе → TestTheWaitSitsInsideTheLoop
  * занять конец хода на втором этапе              → TestTheSecondStageDoesNotWrite
  * ждать залипший ход по три минуты               → TestTheWaitGivesUpOnTime

Каждая из них прогнана: код ломается точечно, названная проверка краснеет.
Две последние появились как раз оттого, что мутация не покраснела — сторожа
живого состояния заслонял отсев служебных путей, а ожидание на ходу держало
только одно ожидание в конце.
"""
import ast, fcntl, inspect, json, os, shutil, subprocess, sys, tempfile, time, unittest, uuid
from pathlib import Path

import pytest
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
    предохранитель к этому — утверждение о числе прошедших пар в каждой
    из медленных проверок.
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


@pytest.mark.slow
class TestTheBaseStartsEmpty(Base):
    """База каждого прогона пустая, и потому два прогона дают одно число.

    Стенд поднимается здесь на всё, что про это можно спросить, и ни одно из
    утверждений не выкинуть:

      * набор проходим — иначе всё остальное зеленеет на нулях: прогон, где не
        проходит ничего, «не наследует прошлый» даром;
      * два прогона одного набора подряд дают одну цифру и одну разбивку —
        это и есть наблюдаемое следствие чистой базы, и то, ради чего замер
        вообще существует;
      * щуп с пустой первой сессией получает подсказку, пока база полна, —
        иначе «второй прогон ничего не получил» доказывает только то, что щуп
        молчит всегда, например задаёт задачу из ниоткуда;
      * прогон, запущенный штатной командой, отрабатывает и возвращает ноль:
        README зовёт `python3 -m eval.live`, и разбор ключей, деление корня по
        рукам и печать итога иначе доходят до человека раньше, чем до проверки.

    Ожидание тишины сведено к нулю: цепочка конца хода идёт на глазах прогона,
    и это отдельно проверяет TestMemoryArrivesThroughTheHooks. Тут оно только
    экономит по две секунды на ходе.
    """

    def test_two_runs_in_a_row_give_one_number_and_neither_inherits(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        filling = a_load(cases)
        # Щуп: та же задача и то же место, но своей первой сессии нет.
        # Подсказать ему может только то, что лежит в базе.
        probe = [dict(pair, id=pair["id"] + "-щуп", tell=[], aim="avoid",
                      task=dict(pair["task"], place=pair["tell"][0]["place"]))
                 for pair in filling]
        items = filling + probe

        first = live.run(pairs=items, player="replay", root=self.root, quiet=0.0)
        won = [row for row in first.asked
               if not row["id"].endswith("-щуп")
               and live.bucket(row) == live.APPLIED]
        self.assertEqual(len(filling), len(won),
                         "набор непроходим — остальное зеленеет на нулях: %s"
                         % [(r["id"], r["reason"]) for r in first.asked])
        self.assertTrue([row for row in first.asked
                         if row["id"].endswith("-щуп") and row["injected"]],
                        "щуп молчит и на полной базе — проверять им нечем")
        # Досчитанный прогон, у которого что-то прошло, уносит песочницу с
        # собой. Ноль оставляют нарочно — разбирать обрыв иначе не по чему.
        self.assertFalse(first.root.exists(), "песочница осталась на диске")

        second = live.run(pairs=items, player="replay", root=self.root, quiet=0.0)
        self.assertEqual(first.passed, second.passed,
                         "два прогона одного набора дали разные цифры: %d и %d"
                         % (first.passed, second.passed))
        self.assertEqual(live.tally(first.asked), live.tally(second.asked),
                         "цифра одна, а разбивка разная — значит она случайна")

        # Пустая база: тот же щуп, но наполнять его некому.
        empty = self.sets / "probe.json"
        pairs.dump(empty, probe)
        clean = live.run(pairs=a_load(empty), player="replay",
                         root=self.root, quiet=0.0)
        self.assertEqual([], [row for row in clean.asked if row["injected"]],
                         "третий прогон получил подсказку из базы прошлых")
        self.assertEqual([], self.leaked(), "прогон наследил в живом состоянии")

        # Штатная команда целиком: разбор ключей, набор из файла, деление корня
        # по рукам, печать итога. Набор тот же пустой — ходов он не играет, и
        # проверка стоит секунды.
        self.assertEqual(0, live.main(["--pairs", str(empty), "--player", "replay",
                                       "--root", str(self.root / "команда")]),
                         "штатная команда не отработала")


@pytest.mark.slow
class TestMemoryArrivesThroughTheHooks(Base):
    """Записи кладёт ход, а не проигрыватель, и кладёт до того, как спросят.

    Заглуши хуки — и база останется пустой. Если бы проигрыватель писал в базу
    сам, эта проверка прошла бы с полной базой и ничего бы не значила.

    Оба прогона идут с нулевым ожиданием тишины. Ожидание по тишине — догадка:
    цепочка конца хода стартует питон и читает архив, ничего при этом не
    записывая, и тихое окно наступает раньше первой записи. Сведи ожидание к
    нулю — и видно, что база наполняется самим ходом, а не тем, что прогон
    подождал.

    Здесь же держится порядок этапов и чистота сессий: разводить их по
    отдельным проверкам значило бы поднимать стенд ещё дважды ради утверждений
    о том же самом прогоне.
    """

    def test_the_hooks_are_what_fills_the_base(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        items = a_load(cases)

        hushed = live.run(pairs=items, player="replay", root=self.root / "глухой",
                          keep=True, live_hooks=False, quiet=0.0)
        repo = db.Repository(hushed.root / "memory.db")
        try:
            self.assertEqual([], repo.search("альфа"),
                             "хуки заглушены, а база наполнилась мимо них")
        finally:
            repo.close()

        report = live.run(pairs=items, player="replay", root=self.root / "живой",
                          keep=True, live_hooks=True, quiet=0.0)
        repo = db.Repository(report.root / "memory.db")
        try:
            self.assertTrue(repo.search("альфа"),
                            "ход вернулся, а конец хода ещё ничего не записал")
        finally:
            repo.close()

        self.assertEqual(len(items), report.passed,
                         "набор непроходим — остальное зеленеет на нулях: %s"
                         % [(r["id"], r["reason"]) for r in report.asked])

        # Цепочка доходит до конца, а не гасится уборкой на полудороге.
        # Признак — отметка связей: она предпоследнее звено.
        left = sorted(p.name for p in
                      (report.root / "state").glob("associate-state*"))
        self.assertTrue(left, "связи не считались ни разу: цепочка обрывалась")

        # Порядок этапов: сначала все ходы, потом все задачи.
        kinds = [row["stage"] for row in report.trail]
        self.assertEqual(sorted(kinds, key=lambda k: k != "play"), kinds,
                         "вопросы перемешались с ходами: %s" % kinds)
        self.assertTrue(report.settled_at, "прогон не дождался фоновой половины")
        self.assertLessEqual(report.settled_at, min(row["at"] for row in report.asked),
                             "вопрос задан раньше, чем осела запись хода")

        # Каждая задача идёт своей сессией, и ни одна не пришла с первого этапа.
        played = {row["session_id"] for row in report.played}
        asked = [row["session_id"] for row in report.asked]
        self.assertTrue(played, "первый этап не сыграл ни одного хода")
        self.assertEqual(len(asked), len(set(asked)), "вопросы делят сессию")
        self.assertEqual(set(), played & set(asked),
                         "вопрос задан сессией первого этапа")


class TestTheWaitSitsInsideTheLoop(unittest.TestCase):
    """Ожидание фона стоит внутри цикла первого этапа, а не одно в конце.

    Ждать один раз перед вторым этапом мало: следующая реплика стартует, пока
    предыдущая ещё пишет, а база одна и открыта без журнала упреждающей записи
    — писателей двое. Прежде это ловил живой прогон с обёрнутыми `settled` и
    `play`: он поднимал стенд ради порядка вызовов, который виден в самом коде.

    Смотрим по разбору, а не по тексту: `settled` в докстринге — не вызов
    `settled`, и запрет по подстроке зеленел бы на нём.
    """

    def calls_of(self, node, name):
        out = []
        for one in ast.walk(node):
            if not isinstance(one, ast.Call):
                continue
            what = one.func
            if isinstance(what, ast.Attribute) and what.attr == name:
                out.append(one)
            if isinstance(what, ast.Name) and what.id == name:
                out.append(one)
        return out

    def the_run(self):
        tree = ast.parse((HERE / "eval" / "live.py").read_text(encoding="utf-8"))
        found = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "run"]
        self.assertEqual(1, len(found), "в eval.live не одна функция run")
        return found[0]

    def stages(self):
        """Два цикла прогона, каждый — по тому, каким этапом он помечает строку.

        Отличать их обязательно. Оба зовут `play`, и правило «ожидание есть
        хоть в одном цикле с `play`» проходит на прогоне, где ожидание уехало
        из первого этапа во второй: первый перестал ждать вовсе, а проверка
        зеленеет.
        """
        run = self.the_run()
        found = {}
        for loop in ast.walk(run):
            if not isinstance(loop, ast.For):
                continue
            for call in self.calls_of(loop, "note"):
                if (call.args and isinstance(call.args[0], ast.Constant)
                        and call.args[0].value in ("play", "ask")):
                    found[call.args[0].value] = loop
        self.assertEqual({"play", "ask"}, set(found),
                         "в прогоне не нашлось обоих этапов: %s" % sorted(found))
        return found["play"], found["ask"]

    def test_the_playing_loop_waits_on_every_turn(self):
        play, _ask = self.stages()
        self.assertTrue(self.calls_of(play, "settled"),
                        "ход не ждёт своей фоновой половины: ожидание вынесено "
                        "из цикла, и следующая реплика стартует на живой записи")

    def test_the_asking_loop_does_not_wait(self):
        """Второму этапу ждать нечего: конец хода у него не занят.

        Проверка не про скорость. Ожидание во втором этапе значило бы, что там
        кто-то пишет, — а писать там некому, и появившийся писатель это ровно
        та беда, ради которой конец хода на втором этапе снят.
        """
        _play, ask = self.stages()
        self.assertEqual([], self.calls_of(ask, "settled"),
                         "второй этап чего-то ждёт — значит там кто-то пишет")

    def test_the_wait_is_not_only_inside_the_loop(self):
        """Обратная проверка: перед вторым этапом ждут ещё раз.

        Последний ход осел, а цепочка конца хода могла взять замок только что.
        Без этого ожидания первая задача уходит на недописанную базу.
        """
        run = self.the_run()
        inside = {id(one) for loop in ast.walk(run) if isinstance(loop, ast.For)
                  for one in self.calls_of(loop, "settled")}
        outside = [one for one in self.calls_of(run, "settled")
                   if id(one) not in inside]
        self.assertTrue(outside, "перед вторым этапом прогон не ждёт вовсе")


class TestTheWaitGivesUpOnTime(Base):
    """Срок ожидания взят по замеру, а не с потолка.

    Стоял он 180 секунд. Замер на прогоне из шести ходов: семь ожиданий, все
    ровно 2.0-2.1 секунды, то есть весь срок — это тихое окно, а сама цепочка
    конца хода к моменту вопроса уже отработала. Срок в три минуты означал
    другое: залипший ход держит прогон три минуты вместо того, чтобы честно
    сказать «не дождались» и пойти дальше.

    Проверяем не число, а поведение: ожидание, которому не дают успокоиться,
    сдаётся в названный срок и говорит об этом. Число сверху держит вторая
    проверка — чтобы срок не уполз обратно к трём минутам молча.
    """

    # Замеренный потолок — 2.05 с при тихом окне 2.0 с. Держим умолчание с
    # пятнадцатикратным запасом: больше — это уже не запас, а зависание.
    CEILING = 30.0

    def test_a_state_that_never_calms_is_given_up_on_within_the_timeout(self):
        """Занятый замок не даёт успокоиться никогда — значит сдаёмся по сроку."""
        for timeout in (0.1, 0.2, 0.3):
            with tempfile.TemporaryDirectory() as tmp:
                lock = Path(tmp) / "save.lock"
                with lock.open("w") as held:
                    fcntl.flock(held, fcntl.LOCK_EX)
                    at = time.time()
                    when, stalled = live.settled(tmp, quiet=0.05, timeout=timeout)
                    spent = time.time() - at
                self.assertTrue(stalled,
                                "ожидание сказало «дождались» на занятом замке")
                self.assertLess(spent, timeout + 2.0,
                                "ожидание переждало свой срок: %.2f при %.2f"
                                % (spent, timeout))
                self.assertGreaterEqual(spent, timeout * 0.5,
                                        "ожидание сдалось раньше срока: %.2f при %.2f"
                                        % (spent, timeout))

    def test_the_default_timeout_stays_within_the_measured_ceiling(self):
        got = inspect.signature(live.settled).parameters["timeout"].default
        self.assertLessEqual(got, self.CEILING,
                             "срок ожидания %s с назван мимо замера: залипший ход "
                             "держит прогон дольше, чем идёт весь прогон" % got)

    def test_the_ceiling_is_a_default_and_not_a_wall(self):
        """Замер снят на отладочном проигрывателе — живому агенту может не хватить.

        Умолчание, которое нельзя поднять, — это тот же потолок, только низкий:
        прогон за деньги упрётся в него молча и разойдётся двумя писателями по
        одной базе. Поэтому срок обязан доходить до ожидания и с командной
        строки, и из вызова.
        """
        self.assertIn("wait", inspect.signature(live.run).parameters,
                      "срок ожидания не поднять из вызова прогона")
        self.assertEqual(90.0, live.parser().parse_args(["--wait", "90"]).wait,
                         "срок ожидания не поднять с командной строки")

        seen = []
        real = live.settled

        def watched(state, extra=(), quiet=2.0, timeout=None):
            seen.append(timeout)
            return time.time(), False

        live.settled = watched
        try:
            live.run(pairs=[], player="replay", root=self.root, wait=77.0)
        finally:
            live.settled = real
        self.assertEqual([77.0], seen,
                         "названный срок до ожидания не доехал: %s" % seen)


class TestTheBenchDoesNotKnowTheDomain(Base):
    """Стенд принимает пары как данные и не знает, о чём они.

    Требование прямое: подставили бытовой набор — работает, подставили набор
    про код — работает. Проверяется формой: в самом прогоне нет имён нашего
    набора, а бытовой пример читается тем же загрузчиком и несёт свою
    отрицательную пару. Живой прогон бытового набора отсюда убран — он ничего
    не добавлял к трём медленным проверкам, а стоил столько же, сколько они.
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
            if name.name in ("pairs.py", "goldenset.py", "evaluate.py",
                             "__init__.py"):
                continue
            self.assertNotIn("eval-cases", name.read_text(encoding="utf-8"),
                             "%s знает форму старого набора" % name.name)

    def test_a_household_set_goes_through_the_same_machinery(self):
        """Бытовой набор проходит всю подготовку прогона, а не только загрузку.

        Проверять его загрузчиком нечего: `pairs.load` и так зовёт `validate`
        на каждой записи, и утверждение «валидное валидно» проходит на
        выпотрошенном `validate`. Спрашиваем то, что до стенда действительно
        может сломаться о незнакомый домен: сценарий ходов, каталог каждого
        хода и место, откуда ставится задача. Ни один из них не имеет права
        знать, о чём набор, — а имена в нём кириллические, с пробелами и без
        единого пути к файлу.
        """
        items = pairs.load(self.HOUSEHOLD)[1]
        turns = live.script_of(items)
        self.assertEqual(sum(len(pair["tell"]) for pair in items), len(turns),
                         "сценарий потерял или размножил ходы бытового набора")
        with live.Sandbox(self.root) as box:
            for turn in turns:
                where = live.ground(box, turn)
                self.assertTrue(str(where).startswith(str(box.places)),
                                "ход бытового набора уехал из песочницы: %s" % where)
                self.assertFalse([bad for bad in extract.NOT_CODE
                                  if bad in "%s/" % where],
                                 "каталог хода отсеивается как служебный: %s" % where)
            places = {live.key_of(turn): live.ground(box, turn) for turn in turns}
            for pair in items:
                asked = live.asked_from(box, pair, places)
                self.assertTrue(str(asked).startswith(str(box.places)),
                                "задача ставится вне песочницы: %s" % asked)
                if pair["tell"]:
                    self.assertEqual(places[live.key_of(pair["tell"][0])], asked,
                                     "%s: задачу ставят не из того места, где "
                                     "сказали — уместность срежет выдачу"
                                     % pair["id"])

    def test_a_household_set_carries_its_own_negative_pair(self):
        items = pairs.load(self.HOUSEHOLD)[1]
        self.assertTrue([pair for pair in items if pair["aim"] == "avoid"],
                        "в примере нет отрицательной пары — проверять нечего")


class TestTheRunReadsOnlyItsOwnArchive(Base):
    """Прогон не читает чужие разговоры, даже когда проход обходит архив весь.

    Разговоры прогона пишет харнесс, и пишет он их в архив пользователя: увести
    их оттуда нечем, не отобрав у агента учётные данные. Значит хук конца хода
    стоит посреди настоящего архива, а связи он считает по всему архиву разом —
    так задумано, вес карточки это число наблюдений.

    Для работы это верно, для замера — нет. Прогон, читающий чужие разговоры,
    кладёт к себе в базу карточки, собранные из чужой переписки: цифра начинает
    зависеть от того, чья машина, и меняется сама собой от разговора к
    разговору. Поэтому прогон называет себе границу обхода, и она у него одна на
    все каталоги ходов.
    """

    def test_the_run_names_a_scope_inside_its_own_sandbox(self):
        with live.Sandbox(self.root) as box:
            scope = box.env().get("XMEM_ONLY") or ""
        self.assertTrue(scope, "прогон не сузил обход архива")
        self.assertIn(live.flat(box.places), scope,
                      "граница обхода не про каталоги ходов: %s" % scope)

    def test_the_scope_matches_every_place_of_the_run_and_nothing_else(self):
        with live.Sandbox(self.root) as box:
            scope = box.env()["XMEM_ONLY"]
            ours = [live.flat(live.ground(box, {"place": name}))
                    for name in ("альфа", "бета", "гамма")]
        for one in ours:
            self.assertIn(scope, one, "свой каталог ходов не попал в границу")
        for alien in (live.flat(Path.home() / ".claude" / "projects" / "чужой"),
                      live.flat(Path("/Users/кто-то/dev/проект"))):
            self.assertNotIn(scope, alien, "чужой каталог попал в границу: %s" % alien)

    def test_a_walk_over_the_archive_honours_the_scope(self):
        """Граница названа окружением — значит проход, идущий по всему архиву,
        обязан её увидеть без ключа в командной строке."""
        yard = self.root / "архив"
        (yard / "ours").mkdir(parents=True)
        (yard / "alien").mkdir(parents=True)
        (yard / "ours" / "a.jsonl").write_text("", encoding="utf-8")
        (yard / "alien" / "b.jsonl").write_text("", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(HERE), XMEM_ONLY="ours")
        code = ("import json,sys;"
                "from infra import config;"
                "print(json.dumps(config.only()))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(HERE),
                             env=env, capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr[-300:])
        self.assertEqual("ours", json.loads(out.stdout))


@pytest.mark.slow
class TestTheSecondStageDoesNotWrite(Base):
    """Вопрос второго этапа не должен попадать в базу, которую он меряет.

    Задача ставится из того же места, где сказали, — иначе уместность срежет
    выдачу. Значит и разбор конца хода у неё тот же, и текст задачи вместе с
    ответом лёг бы в память: пара N подсказывала бы паре N+1. Отсюда и цифра,
    которая ползёт от порядка пар, и отрицательные случаи, которые то проходят,
    то нет. На втором этапе точка конца хода не занята вовсе.

    Тем же поднятием стенда проверяется отчёт и уборка архива: держать ради
    них ещё два прогона по полминуты не за что.
    """

    def test_the_task_text_never_lands_in_the_base(self):
        cases = a_set(self.sets, {"альфа": names(2), "бета": names(2)})
        items = a_load(cases)
        report = live.run(pairs=items, player="replay",
                          root=self.root, keep=True)
        self.addCleanup(shutil.rmtree, report.root, ignore_errors=True)
        repo = db.Repository(report.root / "memory.db")
        try:
            asked = {row["session_id"] for row in report.asked}
            rows = repo.search("правились")
            said = [r for r in rows if (r.get("session_id") or "") in asked]
            self.assertEqual([], said,
                             "вопрос второго этапа осел в базе: %s" % said[:2])
        finally:
            repo.close()

        self.assertEqual(len(items), report.passed,
                         "набор непроходим — проверять оседание нечем: %s"
                         % [(r["id"], r["reason"]) for r in report.asked])

        # Отчёт называет проигрывателя и корзины, которые мы не меряем.
        text = report.text()
        self.assertIn("replay", text)
        self.assertEqual(len(items), report.total)
        for name in live.RESERVED:
            self.assertIn(name, text,
                          "исход, который мы не меряем, пропал из отчёта молча")

        # Разговоров прогона в архиве пользователя не остаётся.
        talks = {row["session_id"] for row in report.played + report.asked}
        left = [talk for talk in talks
                if list(live.TRANSCRIPTS.rglob("%s.jsonl" % talk))]
        self.assertEqual([], left, "разговоры прогона остались в архиве: %s" % left)


class TestTheSecondStageWiring(Base):
    """Проводка второго этапа: конец хода снят, чтение оставлено."""

    def test_the_second_stage_wiring_has_no_end_of_turn_point(self):
        with live.Sandbox(self.root) as box:
            self.assertIn("Stop", box.wiring()["hooks"],
                          "на первом этапе конец хода обязан быть занят")
            self.assertNotIn("Stop", box.wiring(asking=True)["hooks"],
                             "на втором этапе конец хода занят — вопрос осядет в базе")
            self.assertIn("UserPromptSubmit", box.wiring(asking=True)["hooks"],
                          "на втором этапе чтение обязано остаться")


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
    """Уборка песочницы гасит то, что прогон породил.

    Оборванный прогон проверялся отдельным подпроцессом: он ждал до полутора
    минут, пока пойдёт фоновая половина хода, рвал прогон сигналом и ещё шесть
    секунд смотрел, не вернулся ли каталог. Одна эта проверка стоила больше,
    чем вся быстрая батарея, и держать её в ней нельзя. Что от неё осталось —
    прямой вопрос процессу: жив он после уборки или нет; уборку удачного
    прогона несёт TestTheBaseStartsEmpty.
    """

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


if __name__ == "__main__":
    unittest.main()
