#!/usr/bin/env python3
"""Прогон показывает, где обрыв. Запуск: python3 -m unittest tests.test_break_point -v

Первый живой прогон дал ноль из одной пары, а отчёт сказал ровно «память ничего
не нашла». По такому ответу нельзя понять ни одного из пяти разных случаев:
просьбы о разметке не было, модель блок не поставила, блок отбросил маппер,
факт не доехал до базы, поиск его не нашёл. Все пять выглядят одинаково пусто,
и чинить по такому отчёту нечего.

Здесь проверяется отчёт, а не память. Свойства такие:

1. Разметка читается из разговора, а не угадывается по ответу агента. Блок есть
   хоть в одном ответе — «да», нет ни в одном — «нет», блок есть, а маппер всё
   отбросил — «отброшено» с причинами маппера, а не «нет».
2. Цепочка ступеней одна и та же у всех пар и всегда в одном порядке:
   разметка → факт в БД → кандидат → вброс. Строка отчёта называет все четыре.
3. Обрыв спрашивается только у проигравшей пары, и там это первый «нет».
   У прошедшей пары обрыва нет, какая бы ступень ни молчала: до базы знание
   доезжает двумя дорогами, и молчание одной при работающей второй не поломка.
4. Счёт кандидатов не выдумывается снаружи по пустой выдаче, а приезжает с той
   ступени, где поиск ответил: сколько кусков он отдал, столько и в ленте.
5. Руки считаются порознь и не складываются. Рука без нашей памяти — это
   отрицательный контроль, и её цифра обязана оставаться своей.
6. Нулевой итог в руке с памятью песочницу не сносит: разбирать обрыв было бы
   не по чему.

Мутации, на которых проверки обязаны краснеть:
  * убрать поиск блока разметки в разговоре   → TestTheMarkingIsReadFromTheTranscript
  * считать отброшенный блок за «нет»         → TestTheMarkingIsReadFromTheTranscript
  * назвать обрыв у пары, которая прошла      → TestTheBreakIsTheFirstNo
  * промолчать про обрыв у проигравшей пары   → TestTheBreakIsTheFirstNo
  * выкинуть ступень из строки отчёта         → TestTheLineNamesEveryStep
  * не проложить счёт кандидатов из consult   → TestTheCandidateCountComesFromTheSearch
  * потерять счёт кандидатов в ленте          → TestTheCandidateCountComesFromTheSearch
  * сложить руки в одну цифру                 → TestTheArmsAreCountedApart
  * сносить песочницу при нулевом итоге       → TestAZeroKeepsTheSandbox
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, marks
from eval import live
from pipeline import suggest
from storage import local, port

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow,
                                       HealthCheck.function_scoped_fixture])

TALK = "разговор-1"


# --- сырьё -------------------------------------------------------------------

def a_unit(**over):
    """Единица разметки, которую маппер принимает. Поля можно испортить."""
    body = {"type": "preference", "subject": "завтрак", "predicate": "это",
            "value": "овсянка", "time": "", "source": "stated",
            "confidence": 0.9}
    body.update(over)
    return body


def a_block(units):
    """Ответ агента с блоком разметки в конце, ровно как просит схема."""
    lines = [json.dumps(one, ensure_ascii=False) for one in units]
    return "\n".join(["Готово.", marks.XMD1_BEGIN] + lines + [marks.XMD1_END])


# Единицы, которые маппер отбрасывает, и имя причины у каждой. Имена берём у
# самого маппера, а не своим списком: разъедься они — проверка обязана упасть.
BAD = {"source": a_unit(source="inferred"),
       "confidence": a_unit(confidence=0.1),
       "type": a_unit(type="чепуха"),
       "empty": a_unit(subject="", predicate="", value="")}


def a_talk(where, talk, replies):
    """Разговор в том виде, в каком его кладёт харнесс: строка на сообщение."""
    target = Path(where) / ("%s.jsonl" % talk)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for text in replies:
            fh.write(json.dumps(
                {"type": "user", "sessionId": talk,
                 "message": {"role": "user", "content": [
                     {"type": "text", "text": "скажи"}]}},
                ensure_ascii=False) + "\n")
            fh.write(json.dumps(
                {"type": "assistant", "sessionId": talk,
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": text}]}},
                ensure_ascii=False) + "\n")
    return target


@contextlib.contextmanager
def tape():
    """Своя лента на время проверки. В домашнюю пользователя не пишем."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        with mock.patch.object(ledger, "LOG", path):
            yield path


class Answering:
    """Дверь, отвечающая заданным. Подменяем ответ хранилища, а не его самого."""

    name = "local"

    def __init__(self, answer=""):
        self.answer = answer

    def read(self, query, mode="single"):
        return self.answer

    def write(self, text, wait=False):
        return port.door().write(text, wait)

    def write_objects(self, records, relations=(), op="create"):
        return port.door().write_objects(records, relations, op)


def facts_answer(count, word="овсянка", score=None):
    """Выдача хранилища из `count` записей: столько кусков и найдёт поиск.

    Оценка ставится строкой в содержимое — так её и отдаёт хранилище, и так её
    читает разбор выдачи. Без оценки порогу нечего резать.
    """
    tail = "" if score is None else " Оценка уверенности: %.2f" % score
    return json.dumps([{"object_type": "Fact", "fact_type": "preference",
                        "subject": "тема-%d" % i, "scope": "global",
                        "content": "%s номер %d%s" % (word, i, tail)}
                       for i in range(count)], ensure_ascii=False)


# --- 1. разметка -------------------------------------------------------------

class TestTheMarkingIsReadFromTheTranscript(unittest.TestCase):
    """Свойство 1. Про разметку отвечает разговор, а не догадка по ответу."""

    @SLOW
    @given(before=st.integers(min_value=0, max_value=3),
           after=st.integers(min_value=0, max_value=3),
           count=st.integers(min_value=1, max_value=3))
    def test_a_block_anywhere_in_the_talk_reads_as_yes(self, before, after, count):
        """Блок хоть в одном ответе — «да», где бы он ни стоял."""
        with tempfile.TemporaryDirectory() as tmp:
            replies = (["Готово."] * before
                       + [a_block([a_unit()] * count)]
                       + ["Ещё готово."] * after)
            path = a_talk(tmp, TALK, replies)
            probe = live.marking([path])
            self.assertTrue(probe["marked"], "блок в разговоре не нашли")
            self.assertEqual(live.mark_word(probe), "да")

    @SLOW
    @given(replies=st.lists(st.text(max_size=40).filter(
        lambda t: marks.XMD1_BEGIN not in t), min_size=0, max_size=4))
    def test_a_talk_without_a_block_reads_as_no(self, replies):
        """Блока нет ни в одном ответе — «нет». Пустой разговор тоже «нет»."""
        with tempfile.TemporaryDirectory() as tmp:
            path = a_talk(tmp, TALK, replies)
            probe = live.marking([path])
            self.assertFalse(probe["marked"])
            self.assertEqual(live.mark_word(probe), "нет")

    @SLOW
    @given(names=st.lists(st.sampled_from(sorted(BAD)), min_size=1, max_size=4))
    def test_a_dropped_block_is_not_called_a_missing_one(self, names):
        """Блок был, а маппер всё отбросил — «отброшено» с его причинами.

        Свали это в «нет» — и починка ушла бы в промпт, где всё исправно, а
        разошлись на самом деле схема просьбы и маппер.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = a_talk(tmp, TALK, [a_block([BAD[name] for name in names])])
            probe = live.marking([path])
            self.assertTrue(probe["marked"], "блок был, а мы говорим, что не было")
            self.assertEqual(probe["units"], 0)
            self.assertEqual(dict(probe["dropped"]), dict(Counter(names)))
            word = live.mark_word(probe)
            self.assertTrue(word.startswith("отброшено:"), word)
            for name in set(names):
                self.assertIn(name, word, "причина маппера пропала из отчёта")

    @SLOW
    @given(good=st.integers(min_value=1, max_value=3),
           bad=st.lists(st.sampled_from(sorted(BAD)), min_size=0, max_size=3))
    def test_one_surviving_unit_is_enough_for_yes(self, good, bad):
        """Хоть одна пережившая маппер единица — «да», сколько бы ни отбросили."""
        with tempfile.TemporaryDirectory() as tmp:
            units = [a_unit()] * good + [BAD[name] for name in bad]
            path = a_talk(tmp, TALK, [a_block(units)])
            probe = live.marking([path])
            self.assertEqual(probe["units"], good)
            self.assertEqual(live.mark_word(probe), "да")

    def test_the_marking_survives_a_missing_talk(self):
        """Разговора нет на диске — «нет», а не падение посреди отчёта."""
        probe = live.marking([Path("/нет/такого/файла.jsonl")])
        self.assertFalse(probe["marked"])
        self.assertEqual(live.mark_word(probe), "нет")


# --- 2 и 3. цепочка и обрыв --------------------------------------------------

# Ступени как булевы: разметка, факт в базе, кандидат, вброс.
STEPS = st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans())


def a_probe(marked, facts, candidates, injected, reason=None, ok=False):
    return {"marked": marked, "dropped": Counter(),
            "facts": 1 if facts else 0, "lapsed": 0,
            "candidates": 2 if candidates else 0,
            "injected": injected, "ok": ok,
            "reason": reason if not injected else None}


class TestTheLineNamesEveryStep(unittest.TestCase):
    """Свойство 2. Строка отчёта называет все четыре ступени, всегда."""

    @SLOW
    @given(steps=STEPS)
    def test_every_step_is_named_in_order(self, steps):
        line = live.probe_line(a_probe(*steps))
        seen = [line.find(name) for name in live.STEPS_IN_LINE]
        self.assertNotIn(-1, seen, "ступень пропала из строки: %s" % line)
        self.assertEqual(seen, sorted(seen), "порядок ступеней поехал: %s" % line)

    @SLOW
    @given(reason=st.sampled_from(ledger.REASONS))
    def test_a_silence_carries_its_reason(self, reason):
        """Молчание в строке названо причиной, а не одним «нет»."""
        line = live.probe_line(a_probe(True, True, True, False, reason=reason))
        self.assertIn(reason, line, line)


class TestTheBreakIsTheFirstNo(unittest.TestCase):
    """Свойство 3. У проигравшей пары обрыв — первый «нет». У прошедшей его нет.

    Первое выяснилось на живом прогоне: пара, чей факт в базу не попал, всё
    равно получила десять чужих кандидатов и вброс, а ответ вышел без нужного
    слова. Обрыв там на «факте в БД» — назови мы его «применением», починка
    ушла бы не туда.

    Второе выяснилось там же, с другой стороны: пара без блока разметки, чей
    факт доехал вырезом по шаблонам и был применён, прошла целиком. Обрыв на
    «разметке» обвинял бы исправное.
    """

    @SLOW
    @given(steps=STEPS)
    def test_a_losing_pair_is_broken_at_its_first_no(self, steps):
        got = live.break_of(a_probe(*steps, ok=False))
        first = next((name for name, ok in zip(live.STEPS, steps) if not ok), "")
        self.assertEqual(got, first)

    @SLOW
    @given(steps=STEPS)
    def test_a_passing_pair_is_never_called_broken(self, steps):
        """Пара прошла — обрыва нет, какая бы ступень ни молчала."""
        self.assertEqual(live.break_of(a_probe(*steps, ok=True)), "")

    @SLOW
    @given(steps=STEPS)
    def test_a_losing_pair_with_a_silent_step_always_names_one(self, steps):
        """Проигравшая пара с молчащей ступенью обрыв называет, а не молчит."""
        if all(steps):
            return
        self.assertNotEqual(live.break_of(a_probe(*steps, ok=False)), "")

    @SLOW
    @given(steps=STEPS)
    def test_a_whole_chain_names_no_break(self, steps):
        """Вся цепочка «да» — потеря уже не в доставке, а в применении."""
        if all(steps):
            self.assertEqual(live.break_of(a_probe(*steps, ok=False)), "")

    @SLOW
    @given(steps=STEPS)
    def test_the_named_break_is_one_of_the_declared_steps(self, steps):
        self.assertIn(live.break_of(a_probe(*steps)), ("",) + tuple(live.STEPS))

    @SLOW
    @given(outcome=st.sampled_from(live.BUCKETS),
           found=st.integers(min_value=0, max_value=9),
           reason=st.sampled_from(ledger.REASONS))
    def test_the_chain_learns_the_outcome_from_the_second_session(self, outcome,
                                                                  found, reason):
        """Исход пары доезжает до цепочки, а не остаётся в разбивке.

        Не доедет — обрыв назовётся у пары, которая прошла целиком: цепочка без
        исхода не отличает потерю от второй дороги.
        """
        row = {"ok": outcome == live.APPLIED, "injected": outcome != live.NOT_FOUND,
               "intruded": outcome == live.INTRUDED, "error": None,
               "reason": reason, "candidates": found}
        probe = live.second_half(a_probe(True, True, True, True), row)
        self.assertEqual(probe["ok"], live.bucket(row) == live.APPLIED,
                         "исход пары не доехал до цепочки")
        self.assertEqual(probe["candidates"], found)
        self.assertEqual(probe["injected"], row["injected"])
        self.assertEqual(probe["reason"], reason)

    @SLOW
    @given(steps=STEPS)
    def test_a_pair_the_run_called_applied_shows_no_break(self, steps):
        """Пара, которую разбивка назвала удачей, обрыва не получает."""
        row = {"ok": True, "injected": True, "intruded": False, "error": None,
               "reason": None, "candidates": 3}
        probe = live.second_half(a_probe(*steps), row)
        self.assertEqual(live.break_of(probe), "")

    def test_a_negative_pair_is_not_blamed_for_an_empty_base(self):
        """У отрицательной пары ждать нечего: ступени фактов у неё нет.

        Ей нужен как раз пустой ответ, и «обрыв: факт в БД» на ней означал бы,
        что отчёт называет обрывом исправную работу.
        """
        probe = a_probe(True, False, False, False, reason="not_found")
        probe["expected"] = False
        self.assertEqual(live.break_of(probe), "")


# --- 4. кандидаты ------------------------------------------------------------

class TestTheCandidateCountComesFromTheSearch(unittest.TestCase):
    """Свойство 4. Сколько поиск отдал кусков, столько и в ленте."""

    @SLOW
    @given(count=st.integers(min_value=1, max_value=6))
    def test_consult_reports_what_the_search_returned(self, count):
        answer = facts_answer(count)
        found = suggest.consult("овсянка", door=Answering(answer))[4]
        self.assertEqual(found, len(suggest.pieces(answer)))
        self.assertEqual(found, count)

    def test_an_empty_search_reports_zero(self):
        self.assertEqual(suggest.consult("овсянка", door=Answering(""))[4], 0)

    @SLOW
    @given(count=st.integers(min_value=1, max_value=6))
    def test_the_count_reaches_the_ledger_on_a_silence(self, count):
        """Порог срезал всё — в ленте всё равно видно, сколько нашлось.

        Разрыв «нашли N, отдали ноль» это и есть ответ на вопрос, где обрыв.
        Без числа он неотличим от пустой базы.
        """
        with tape():
            suggest.attend("овсянка", session_id=TALK,
                           door=Answering(facts_answer(count, score=0.1)),
                           record=False, min_score=0.9)
            rows = ledger.rows()
            self.assertEqual([row["event"] for row in rows], ["silent"])
            self.assertEqual(rows[0].get("found"), count)

    @SLOW
    @given(count=st.integers(min_value=1, max_value=6))
    def test_the_count_reaches_the_ledger_on_an_injection(self, count):
        """Вброс несёт то же число: «нашли N, отдали M» читается одной строкой."""
        with tape():
            text, kept, why = suggest.attend(
                "овсянка", session_id=TALK, door=Answering(facts_answer(count)),
                record=False, min_score=0.0)
            self.assertTrue(text, "память промолчала: %s" % why)
            row = next(r for r in ledger.rows() if r["event"] == "injected")
            self.assertEqual(row.get("found"), count)
            self.assertLessEqual(row["items"], count)

    @SLOW
    @given(count=st.integers(min_value=1, max_value=6))
    def test_the_ledger_reader_gives_the_count_back(self, count):
        """`verdict_of` отдаёт счёт кандидатов, а не только исход захода."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger.silence("below_threshold", session_id=TALK, query="q",
                           log=path, found=count)
            box = mock.Mock(state=Path(tmp))
            injected, reason, found = live.verdict_of(box, TALK)
            self.assertFalse(injected)
            self.assertEqual(reason, "below_threshold")
            self.assertEqual(found, count)


# --- 5. руки -----------------------------------------------------------------

class TestTheArmsAreCountedApart(unittest.TestCase):
    """Свойство 5. Две руки — две цифры и разница, а не одна сумма."""

    def test_the_default_arm_is_memory_alone(self):
        """Умолчание — одна рука с памятью. Голую руку зовут явно."""
        args = live.parser().parse_args(["--pairs", "нет.json"])
        self.assertEqual(args.arms, "memory")
        self.assertEqual(live.arms_of(args.arms), ("memory",))

    def test_both_asks_for_two_arms_in_a_fixed_order(self):
        """Рука с памятью идёт первой: с неё снимается цифра МВП."""
        self.assertEqual(live.arms_of("both"), ("memory", "bare"))
        self.assertEqual(live.arms_of("bare"), ("bare",))

    @SLOW
    @given(memory=st.integers(min_value=0, max_value=9),
           bare=st.integers(min_value=0, max_value=9),
           total=st.integers(min_value=9, max_value=12))
    def test_the_difference_is_memory_minus_bare(self, memory, bare, total):
        bout = live.Bout({"memory": FakeReport(memory, total),
                          "bare": FakeReport(bare, total)})
        self.assertEqual(bout.diff, memory - bare)
        text = bout.text()
        for name in ("memory", "bare"):
            self.assertIn(name, text)
        self.assertIn("разница", text)

    @SLOW
    @given(memory=st.integers(min_value=0, max_value=9),
           total=st.integers(min_value=9, max_value=12))
    def test_one_arm_names_no_difference(self, memory, total):
        """Одна рука — сравнивать не с чем, и выдуманной разницы нет."""
        bout = live.Bout({"memory": FakeReport(memory, total)})
        self.assertIsNone(bout.diff)
        self.assertNotIn("разница", bout.text())

    @SLOW
    @given(memory=st.integers(min_value=0, max_value=9),
           bare=st.integers(min_value=0, max_value=9),
           total=st.integers(min_value=9, max_value=12))
    def test_the_arms_never_merge_into_one_number(self, memory, bare, total):
        """Цифра руки остаётся своей: сумма рук нигде не печатается."""
        bout = live.Bout({"memory": FakeReport(memory, total),
                          "bare": FakeReport(bare, total)})
        self.assertEqual(bout.passed("memory"), memory)
        self.assertEqual(bout.passed("bare"), bare)
        text = bout.text()
        for name, got in (("memory", memory), ("bare", bare)):
            with self.subTest(arm=name):
                self.assertIn("итог: %d из %d" % (got, total), text)

    def test_the_bare_arm_silences_our_hooks(self):
        """Рука без нашей памяти — это выключенный рубильник, а не пустая база.

        Встроенную память Claude Code не трогаем: она часть этой руки.
        """
        self.assertEqual(live.hooks_of_arm("memory"), True)
        self.assertEqual(live.hooks_of_arm("bare"), False)


class FakeReport:
    """Отчёт руки, из которого нужны только цифры."""

    def __init__(self, passed, total):
        self.passed, self.total = passed, total
        self.root = Path("/тут/песочница")
        self.probe = {}

    def text(self):
        return "итог: %d из %d" % (self.passed, self.total)


# --- 6. песочница при нуле ---------------------------------------------------

class TestAZeroKeepsTheSandbox(unittest.TestCase):
    """Свойство 6. Ноль в руке с памятью — песочницу оставляем."""

    @SLOW
    @given(passed=st.integers(min_value=0, max_value=5),
           asked=st.booleans())
    def test_a_zero_is_kept_and_anything_else_is_not(self, passed, asked):
        keep = live.keep_after(passed=passed, done=True, asked=asked)
        self.assertEqual(keep, asked or passed == 0)

    @SLOW
    @given(passed=st.integers(min_value=0, max_value=5))
    def test_an_interrupted_run_still_cleans_up(self, passed):
        """Обрыв песочницу уносит: сохранять нечего, прогон не досчитал."""
        self.assertFalse(live.keep_after(passed=passed, done=False, asked=False))
        self.assertTrue(live.keep_after(passed=passed, done=False, asked=True))

    def test_a_kept_sandbox_prints_its_path(self):
        """Путь к сохранённой песочнице печатается: иначе её не найти."""
        report = live.Report(mock.Mock(root=Path("/тут/песочница")), "claude", [])
        report.kept = True
        self.assertIn("/тут/песочница", report.text())


if __name__ == "__main__":
    unittest.main()
