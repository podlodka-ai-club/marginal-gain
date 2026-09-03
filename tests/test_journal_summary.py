#!/usr/bin/env python3
"""Сводка по журналу: сколько раз пара прошла каждую ступень, скриптом.

Запуск: python3 -m pytest tests/test_journal_summary.py -q

Задача — замер, не починка: «плавает ли запись» отвечает цифрой, снятой с
журнала скриптом, а не пересказанной глазами из логов. Свойства ниже
защищают саму механику счёта, а не какой-то один прогон.

Свойства:

1. В сводку пары идут только строки, урезанные ровно до неё (`only ==
   pair_id`) и сыгранные нужной рукой. Полный прогон, где эта пара тоже
   встречается, в счёт не идёт — иначе знаменатель «сколько из N» плывёт
   между прогонами разной природы, то самое смешение, ради которого в
   журнал вообще добавили поле `only`.
2. Ступень пройдена, если `break` пуст (пара дошла до конца цепочки) или
   назван обрыв ПОЗЖЕ этой ступени по порядку `STEPS`. Раз `break_of` кладёт
   первый провал по порядку, счёт ступеней в сумме по многим прогонам не
   может расти по ходу цепочки: `разметка >= факт в БД >= кандидат >= вброс`.
3. Пустой `break` засчитывает все четыре ступени; названный обрыв засчитывает
   строго ступени до него и ни одной после.
4. Все счётчики — от 0 до общего числа прогонов пары, `passed` — честное
   число `outcome == APPLIED`, тот же критерий, что у `Report.passed`.

Мутации, на которых проверки обязаны краснеть:
  * не фильтровать по `only`, взять все строки, где есть пара нужного id
                                                        → TestOnlyCurtailedRunsCount
  * не фильтровать по руке (`arm`)                      → TestOnlyCurtailedRunsCount
  * засчитывать ступень после обрыва как пройденную      → TestStepsReached
  * взять любой другой `outcome` вместо точного `APPLIED` для `passed`
                                                        → TestPassedCount
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live
from eval import journal_summary as js

FAST = settings(deadline=None, max_examples=100)

BREAKS = st.sampled_from(("",) + live.STEPS)
OUTCOMES = st.sampled_from(live.BUCKETS)


def a_pair_entry(id="макбук", aim="apply", outcome=live.APPLIED, break_=""):
    return {"id": id, "aim": aim, "outcome": outcome, "break": break_}


def a_journal_row(only, arm_pairs, arms=("memory",)):
    """Строка журнала: только то, что читает `journal_summary`."""
    return {
        "ts": "2026-01-01T00:00:00Z", "model": "haiku", "player": "claude",
        "voice": "plain", "pairs_file": "n.json", "pairs_count": 5,
        "only": only, "limit": None,
        "arms": {arm: {"passed": 0, "total": 1, "share": None, "cost_usd": 0.0,
                       "pairs": arm_pairs} for arm in arms},
    }


# --- 1. только урезанные строки нужной пары и руки ---------------------------

class TestOnlyCurtailedRunsCount(unittest.TestCase):
    """Свойство 1. Полный прогон и чужая пара в счёт не идут."""

    def test_a_full_run_with_the_same_pair_inside_is_not_counted(self):
        """Прогон без `--only` несёт ту же пару, но в сводку не входит.

        Ровно случай из задачи: базовая строка журнала играла и «макбук», и
        «город» одним полным прогоном (`only: null`). Если бы сводка её
        считала, урезанные и полные прогоны смешались бы в одном
        знаменателе — то, ради чего поле `only` вообще завели.
        """
        full = a_journal_row(only=None,
                             arm_pairs=[a_pair_entry(id="макбук", break_="факт в БД",
                                                     outcome=live.UNUSED)])
        curtailed = a_journal_row(only="макбук",
                                  arm_pairs=[a_pair_entry(id="макбук")])
        summary = js.summarize([full, curtailed], "макбук")
        self.assertEqual(summary["total"], 1)

    def test_a_run_curtailed_to_another_pair_is_not_counted(self):
        other = a_journal_row(only="город",
                              arm_pairs=[a_pair_entry(id="город")])
        summary = js.summarize([other], "макбук")
        self.assertEqual(summary["total"], 0)

    def test_a_run_played_with_a_different_arm_is_not_counted(self):
        row = a_journal_row(only="макбук",
                            arm_pairs=[a_pair_entry(id="макбук")], arms=("bare",))
        summary = js.summarize([row], "макбук", arm="memory")
        self.assertEqual(summary["total"], 0)

    @given(n_matching=st.integers(min_value=0, max_value=8),
          n_noise=st.integers(min_value=0, max_value=8))
    @FAST
    def test_the_total_is_exactly_the_number_of_curtailed_matching_runs(
            self, n_matching, n_noise):
        matching = [a_journal_row(only="макбук",
                                  arm_pairs=[a_pair_entry(id="макбук")])
                   for _ in range(n_matching)]
        noise = [a_journal_row(only="город", arm_pairs=[a_pair_entry(id="город")])
                for _ in range(n_noise)]
        summary = js.summarize(matching + noise, "макбук")
        self.assertEqual(summary["total"], n_matching)


# --- 2 и 3. какие ступени пройдены по полю break ------------------------------

class TestStepsReached(unittest.TestCase):
    """Свойства 2 и 3. Ступени до обрыва — пройдены, обрыв и дальше — нет."""

    def test_an_empty_break_reaches_every_step(self):
        reached = js.steps_reached("")
        self.assertEqual(reached, {step: True for step in live.STEPS})

    @given(idx=st.integers(min_value=0, max_value=len(live.STEPS) - 1))
    @FAST
    def test_a_named_break_reaches_only_the_steps_before_it(self, idx):
        step = live.STEPS[idx]
        reached = js.steps_reached(step)
        for i, s in enumerate(live.STEPS):
            self.assertEqual(reached[s], i < idx, (s, i, idx))

    @given(breaks=st.lists(BREAKS, min_size=1, max_size=12))
    @FAST
    def test_the_aggregate_step_counts_never_increase_along_the_chain(self, breaks):
        """Сумма монотонных по ступени векторов сама монотонна по ступени.

        У каждой пары одного прогона вектор пройденных ступеней — это
        единицы до обрыва и нули после, невозрастающий по построению
        `break_of` (первый провал по порядку). Сумма таких векторов по
        многим прогонам обязана остаться невозрастающей: пройти дальше по
        цепочке нельзя чаще, чем дойти до предыдущей её точки.
        """
        entries = [a_pair_entry(break_=b) for b in breaks]
        rows = [a_journal_row(only="макбук", arm_pairs=[e]) for e in entries]
        summary = js.summarize(rows, "макбук")
        counts = list(summary["steps"].values())
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "счёт по ступеням вырос дальше по цепочке: %s" % counts)

    @given(breaks=st.lists(BREAKS, min_size=0, max_size=12))
    @FAST
    def test_every_step_count_is_within_bounds(self, breaks):
        entries = [a_pair_entry(break_=b) for b in breaks]
        rows = [a_journal_row(only="макбук", arm_pairs=[e]) for e in entries]
        summary = js.summarize(rows, "макбук")
        for count in summary["steps"].values():
            self.assertGreaterEqual(count, 0)
            self.assertLessEqual(count, summary["total"])


# --- 4. passed — честный APPLIED, счётчики в границах -------------------------

class TestPassedCount(unittest.TestCase):
    """Свойство 4. `passed` — точно `outcome == APPLIED`, не любая удача."""

    @given(outcomes=st.lists(OUTCOMES, min_size=0, max_size=12))
    @FAST
    def test_passed_counts_exactly_the_applied_outcomes(self, outcomes):
        entries = [a_pair_entry(outcome=o) for o in outcomes]
        rows = [a_journal_row(only="макбук", arm_pairs=[e]) for e in entries]
        summary = js.summarize(rows, "макбук")
        want = sum(1 for o in outcomes if o == live.APPLIED)
        self.assertEqual(summary["passed"], want)
        self.assertGreaterEqual(summary["passed"], 0)
        self.assertLessEqual(summary["passed"], summary["total"])


# --- пример, взятый из настоящей строки журнала --------------------------------

class TestAKnownExample(unittest.TestCase):
    """Ровно строка из задачи: «макбук» оборвался на «факт в БД»."""

    def test_the_macbook_break_reported_by_the_task(self):
        entry = a_pair_entry(id="макбук", outcome=live.UNUSED, break_="факт в БД")
        row = a_journal_row(only="макбук", arm_pairs=[entry])
        summary = js.summarize([row], "макбук")
        self.assertEqual(summary["steps"]["разметка"], 1)
        self.assertEqual(summary["steps"]["факт в БД"], 0)
        self.assertEqual(summary["steps"]["кандидат"], 0)
        self.assertEqual(summary["steps"]["вброс"], 0)
        self.assertEqual(summary["passed"], 0)


# --- отрицательная пара не выдаёт цепочку задним числом -----------------------

class TestNegativePairsDoNotFakeDelivery(unittest.TestCase):
    """`break_of` не спрашивает обрыв у отрицательной пары вовсе (`aim=avoid`):

    ей нечего ждать в базе, и пустой `break` там значит «вопрос не задавался»,
    не «доставила». Считать это как «прошла все четыре ступени» — придумать
    цифру, которой прогон не давал.
    """

    def test_an_avoid_pair_does_not_report_a_full_chain(self):
        entry = a_pair_entry(id="забор", aim="avoid", outcome=live.APPLIED,
                             break_="")
        row = a_journal_row(only="забор", arm_pairs=[entry])
        summary = js.summarize([row], "забор")
        for step, count in summary["steps"].items():
            self.assertEqual(count, 0, "ступень %r придумана отрицательной паре" % step)

    @given(outcome=OUTCOMES)
    @FAST
    def test_avoid_pairs_never_move_a_step_counter(self, outcome):
        entry = a_pair_entry(id="забор", aim="avoid", outcome=outcome, break_="")
        row = a_journal_row(only="забор", arm_pairs=[entry])
        summary = js.summarize([row], "забор")
        self.assertEqual(sum(summary["steps"].values()), 0)


# --- журнал переживает битую строку --------------------------------------------

class TestTheJournalReadSurvivesABrokenLine(unittest.TestCase):
    """`eval-runs.jsonl` дописывается на ходу — последняя строка может быть
    половиной. Чтение роняет только её, не весь журнал (тот же приём, что и
    `live.replies_of`).
    """

    def test_a_truncated_last_line_does_not_crash_the_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            good = a_journal_row(only="макбук", arm_pairs=[a_pair_entry(id="макбук")])
            path.write_text(json.dumps(good, ensure_ascii=False) + "\n"
                            + '{"only": "макбук", "arms": {"mem', encoding="utf-8")
            rows = js.rows_of(path)
            self.assertEqual(rows, [good])

    def test_a_missing_journal_file_reads_as_no_rows(self):
        self.assertEqual(js.rows_of("/nonexistent/nowhere.jsonl"), [])


# --- ступень цепочки, которой в текущем коде уже нет ---------------------------

class TestAnUnknownBreakDoesNotCrashTheSummary(unittest.TestCase):
    """Журнал коммитится в репозиторий и переживает код: старая строка может
    называть ступень, которую `live.STEPS` больше не знает (переименовали,
    расширили). Такая строка не роняет сводку по остальным девятнадцати.
    """

    def test_an_unrecognized_break_name_is_not_counted_as_reached(self):
        reached = js.steps_reached("ступень-которой-больше-нет")
        self.assertEqual(reached, {step: False for step in live.STEPS})

    def test_it_does_not_raise(self):
        entry = a_pair_entry(break_="ступень-которой-больше-нет")
        row = a_journal_row(only="макбук", arm_pairs=[entry])
        summary = js.summarize([row], "макбук")   # не должно бросить ValueError
        self.assertEqual(summary["total"], 1)


# --- `only` матчится тем же правилом, что резало набор в run() ----------------

class TestOnlyMatchesTheSameRuleRunUsedToCut(unittest.TestCase):
    """`run()` режет набор по `only in pair["id"]` (подстрока), не равенству.

    Прогон `--only макбук` мог задеть и другую пару, чей id содержит
    «макбук» подстрокой, — и её строку в сводке этой пары терять molча
    неправильно: `runs_of` обязан узнавать урезанный прогон тем же правилом,
    каким его резал `run()`, а не своим более строгим.
    """

    def test_a_substring_only_still_counts_for_the_longer_id(self):
        entry = a_pair_entry(id="старый-макбук")
        row = a_journal_row(only="макбук", arm_pairs=[entry])
        runs = js.runs_of([row], "старый-макбук")
        self.assertEqual(runs, [row])

    def test_a_full_run_is_still_excluded(self):
        entry = a_pair_entry(id="макбук")
        row = a_journal_row(only=None, arm_pairs=[entry])
        runs = js.runs_of([row], "макбук")
        self.assertEqual(runs, [])

    def test_an_unrelated_only_value_is_not_counted(self):
        entry = a_pair_entry(id="макбук")
        row = a_journal_row(only="город", arm_pairs=[entry])
        runs = js.runs_of([row], "макбук")
        self.assertEqual(runs, [])


# --- один плохой прогон не роняет сводку по остальным парам ключа -------------

class TestOneBadPairDoesNotAbortTheWholeBatch(unittest.TestCase):
    """`main --pair a --pair b`: битые данные пары `a` не должны стоить
    сводки по `b` — иначе один грязный прогон убивает весь батч ради пары,
    которую даже не спрашивали.
    """

    def test_main_reports_the_good_pair_when_another_pair_is_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            broken = a_journal_row(
                only="макбук",
                arm_pairs=[a_pair_entry(id="макбук"), a_pair_entry(id="макбук")])
            good = a_journal_row(only="город", arm_pairs=[a_pair_entry(id="город")])
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(broken, ensure_ascii=False) + "\n")
                fh.write(json.dumps(good, ensure_ascii=False) + "\n")

            import io
            from contextlib import redirect_stdout, redirect_stderr
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = js.main(["--journal", str(path),
                               "--pair", "макбук", "--pair", "город"])
            self.assertNotEqual(code, 0)
            self.assertIn("город", out.getvalue())
            self.assertIn("1 из 1", out.getvalue())


# --- подстрочный only не должен клеветать на чужой чистый прогон --------------

class TestASubstringCollisionDoesNotFalselyAccuseAnUnrelatedRun(unittest.TestCase):
    """`only` матчится подстрокой (см. `run()`), но сам прогон мог не тронуть
    запрошенную пару вовсе: «макбук» — подстрока и «старый-макбук», и
    «новый-макбук», а сыгран был только один из них. Прогон, который эту
    пару и не спрашивал, не «грязный» — его просто не о чем спросить.
    """

    def test_an_unrelated_run_with_a_colliding_only_is_silently_skipped(self):
        row = a_journal_row(only="макбук",
                            arm_pairs=[a_pair_entry(id="новый-макбук")])
        summary = js.summarize([row], "старый-макбук")
        self.assertEqual(summary["total"], 0)

    def test_a_true_duplicate_inside_one_run_still_raises(self):
        """Два одинаковых id пары в одном прогоне — уже настоящая порча
        данных, не совпадение подстроки, и от неё сводка обязана отказаться
        отдельным падением на эту пару, не молчанием.
        """
        row = a_journal_row(only="макбук",
                            arm_pairs=[a_pair_entry(id="макбук"),
                                      a_pair_entry(id="макбук")])
        with self.assertRaises(ValueError):
            js.summarize([row], "макбук")


# --- журнал переживает битую последнюю строку целиком, не только json.loads --

class TestTheJournalReadSurvivesATruncatedMultibyteTail(unittest.TestCase):
    """Файл дописывается на ходу: конец может оборваться и посреди байтов
    UTF-8 символа, не только посреди валидного JSON. `errors="replace"`,
    как в `live.replies_of`, а не строгий разбор кодировки.
    """

    def test_a_line_cut_mid_character_does_not_crash_the_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            good = a_journal_row(only="макбук", arm_pairs=[a_pair_entry(id="макбук")])
            body = json.dumps(good, ensure_ascii=False) + "\n" + '{"only": "мак'
            raw = body.encode("utf-8")[:-1]   # режем середину последнего символа
            path.write_bytes(raw)
            rows = js.rows_of(path)            # не должно бросить UnicodeDecodeError
            self.assertEqual(rows, [good])

    def test_a_non_dict_json_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            good = a_journal_row(only="макбук", arm_pairs=[a_pair_entry(id="макбук")])
            path.write_text(json.dumps([1, 2, 3]) + "\n"
                            + json.dumps(good, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            self.assertEqual(js.rows_of(path), [good])


# --- отсутствующее поле пары не роняет соседей ---------------------------------

class TestAMissingFieldOnOnePairDoesNotAbortItsNeighbour(unittest.TestCase):
    """`break`/`outcome` читаются мягко: их нет — считаем, что ступень не
    подтверждена и удача не засчитана, а не падаем `KeyError`-ом посреди
    батча ради пары, которую даже не спрашивали.
    """

    def test_a_missing_break_key_counts_as_no_step_confirmed(self):
        entry = {"id": "макбук", "aim": "apply", "outcome": live.APPLIED}
        row = a_journal_row(only="макбук", arm_pairs=[entry])
        summary = js.summarize([row], "макбук")
        self.assertEqual(sum(summary["steps"].values()), 0)
        self.assertEqual(summary["passed"], 1)

    def test_a_missing_outcome_key_does_not_count_as_passed(self):
        entry = {"id": "макбук", "aim": "apply", "break": ""}
        row = a_journal_row(only="макбук", arm_pairs=[entry])
        summary = js.summarize([row], "макбук")
        self.assertEqual(summary["passed"], 0)

    def test_main_still_reports_the_clean_sibling_pair(self):
        broken = a_journal_row(
            only="макбук", arm_pairs=[{"id": "макбук", "aim": "apply"}])
        good = a_journal_row(only="город", arm_pairs=[a_pair_entry(id="город")])
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(broken, ensure_ascii=False) + "\n")
                fh.write(json.dumps(good, ensure_ascii=False) + "\n")
            out = io.StringIO()
            with redirect_stdout(out):
                js.main(["--journal", str(path), "--pair", "макбук", "--pair", "город"])
        self.assertIn("город", out.getvalue())
        self.assertIn("1 из 1", out.getvalue())


# --- рука по умолчанию не расходится с eval.live --------------------------------

class TestTheDefaultArmMatchesLive(unittest.TestCase):
    def test_default_arm_is_the_same_constant_live_uses(self):
        self.assertEqual(js.DEFAULT_ARM, live.DEFAULT_ARMS)


if __name__ == "__main__":
    unittest.main()
