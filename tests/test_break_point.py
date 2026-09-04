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
import contextlib
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, marks
from eval import live
from pipeline import suggest
from storage import db, port

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
    def test_the_named_step_never_shows_success(self, steps):
        """Названная ступень не та, у которой на руках доказательство успеха.

        Проверяем по уликам, а не повтором формулы: факт в базе, кандидаты у
        поиска, вброс в ленте. Обвинить ступень, чью работу видно, — та самая
        ошибка, из-за которой починка уходит в исправное.
        """
        marked, facts, hit, injected = steps
        probe = a_probe(*steps, ok=False)
        got = live.break_of(probe)
        if facts:
            self.assertNotEqual(got, "факт в БД")
            self.assertNotEqual(got, "разметка", "факт доехал, разметка ни при чём")
        if hit:
            self.assertNotEqual(got, "кандидат")
        if injected:
            self.assertNotEqual(got, "вброс")
        if marked:
            self.assertNotEqual(got, "разметка")

    @SLOW
    @given(steps=STEPS)
    def test_a_step_before_the_named_one_never_shows_failure(self, steps):
        """Раньше названной ступени всё сработало — иначе обрыв назван поздно."""
        probe = a_probe(*steps, ok=False)
        got = live.break_of(probe)
        if not got:
            return
        at = live.STEPS.index(got)
        evidence = (steps[0] or steps[1], steps[1], steps[2], steps[3])
        self.assertTrue(all(evidence[:at]),
                        "перед обрывом тоже «нет»: обрыв назван слишком поздно")

    def test_a_wholly_silent_chain_is_broken_at_the_marking(self):
        """Всё молчит — обрыв на первой ступени, а не на удобной."""
        self.assertEqual(
            live.break_of(a_probe(False, False, False, False, ok=False)),
            "разметка")

    @SLOW
    @given(steps=STEPS)
    def test_a_passing_pair_is_never_called_broken(self, steps):
        """Пара прошла — обрыва нет, какая бы ступень ни молчала."""
        self.assertEqual(live.break_of(a_probe(*steps, ok=True)), "")

    @SLOW
    @given(steps=STEPS)
    def test_a_losing_pair_with_a_silent_step_always_names_one(self, steps):
        """Проигравшая пара с молчащей ступенью обрыв называет, а не молчит.

        Молчание разметки при доехавшем факте молчанием ступени не считается:
        знание пришло второй дорогой, и обвинять там нечего.
        """
        effective = (steps[0] or steps[1],) + tuple(steps[1:])
        if all(effective):
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

    @SLOW
    @given(candidates=st.booleans(), injected=st.booleans())
    def test_a_fact_in_the_base_clears_the_marking_stage(self, candidates,
                                                         injected):
        """Факт доехал — разметку обрывом не называем, даже если блока не было.

        До базы знание доезжает двумя дорогами, и вырез по шаблонам работает
        без всякого блока. Пара, чей факт в базе лежит, а проиграла на поиске,
        должна отсылать к поиску: «обрыв: разметка» увёл бы починку в промпт,
        где всё исправно.
        """
        probe = a_probe(False, True, candidates, injected, reason="not_found")
        self.assertNotEqual(live.break_of(probe), "разметка")

    @SLOW
    @given(marked=st.booleans())
    def test_a_search_that_never_ran_is_not_blamed_for_finding_nothing(self,
                                                                      marked):
        """Счёта кандидатов нет — значит до поиска не дошло, а не «нашли ноль».

        Отказ носителя и вышедший срок обрывают заход раньше поиска, и лента
        поля `found` в такой строке не пишет вовсе (`ledger._found`). Прими мы
        его отсутствие за ноль — отчёт послал бы чинить поиск вместо носителя.
        """
        probe = a_probe(marked, True, True, False, reason="backend_error")
        probe["candidates"] = None
        self.assertNotEqual(live.break_of(probe), "кандидат")

    def test_a_search_that_ran_and_found_nothing_is_still_blamed(self):
        """Ноль кандидатов — это поиск сходил и вернулся пустым. Обрыв на нём."""
        probe = a_probe(True, True, False, False, reason="not_found")
        probe["candidates"] = 0
        self.assertEqual(live.break_of(probe), "кандидат")

    def test_an_unknown_fact_count_is_not_blamed_either(self):
        """База не читалась — обрывом называем не запись факта."""
        probe = a_probe(True, True, False, False, reason="not_found")
        probe["facts"] = None
        self.assertNotEqual(live.break_of(probe), "факт в БД")

    def test_a_negative_pair_is_not_blamed_for_an_empty_base(self):
        """У отрицательной пары ждать нечего: ступени фактов у неё нет.

        Ей нужен как раз пустой ответ, и «обрыв: факт в БД» на ней означал бы,
        что отчёт называет обрывом исправную работу.
        """
        probe = a_probe(True, False, False, False, reason="not_found")
        probe["expected"] = False
        self.assertEqual(live.break_of(probe), "")


class TestTheFactCountReadsTheBase(unittest.TestCase):
    """Свойство 3а. Счёт фактов ищет слово, а не словоформу и не шаблон.

    Ступень «факт в БД» — та, по которой отчёт решает, доехало ли знание.
    Соврёт она — обрыв назовётся не там, и починка уйдёт в исправное.
    """

    def a_base(self, tmp, rows):
        base = Path(tmp) / "memory.db"
        conn = db.connect(base)
        db.migrate(conn)
        for subject, content in rows:
            conn.execute(
                'INSERT INTO "fact" (fact_type, subject, scope, content) '
                "VALUES ('preference', ?, 'global', ?)", (subject, content))
        conn.commit()
        conn.close()
        return base

    @SLOW
    @given(word=st.sampled_from(["Казан", "казан", "КАЗАН", "КаЗаН"]))
    def test_a_capital_cyrillic_word_is_found(self, word):
        """Кириллица с прописной находится. `lower()` в SQLite её не трогает.

        Слово набор даёт как есть («Казан»), а факт модель пишет как придётся
        («Казань», «в Казани»). Сравнение, слепое к регистру только в латинице,
        показало бы «фактов: 0» на факте, который лежит в базе.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("Казань", "человек живёт в Казани")])
            alive, _ = live.facts_with(base, [word])
            self.assertEqual(alive, 1, "факт в базе есть, а счёт его не увидел")

    @SLOW
    @given(form=st.sampled_from(["Казань", "в Казани", "из Казани", "КАЗАНЬ",
                                 "Казанью", "казани"]))
    def test_a_word_form_in_the_fact_is_found_by_the_stem_of_the_set(self, form):
        """Ожидание набора — основа, и любая форма слова в факте по ней видна."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("город", "человек живёт %s" % form)])
            alive, _ = live.facts_with(base, ["Казан"])
            self.assertEqual(alive, 1,
                             "форма %r не нашлась по основе набора" % form)

    @SLOW
    @given(spelling=st.sampled_from(["живёт", "живет"]))
    def test_yo_in_the_fact_does_not_hide_it_from_the_count(self, spelling):
        """Ступень «фактов» размечает поле той же разметкой, что и кандидаты.

        Иначе цепочка врёт про исправное с другой стороны: кандидаты сходятся
        по ключу, где `ё` сведена, а счёт фактов сравнивал поле как есть — и
        отчёт показывал «фактов: 0» там, где поиск факт нашёл и вбросил. Тот же
        класс лжи, что чинили в db7b25b, только зеркальный.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("город", "человек %s в Казани" % spelling)])
            alive, _ = live.facts_with(base, ["живет"])
            self.assertEqual(alive, 1,
                             "запись %r спряталась от счёта фактов" % spelling)

    @SLOW
    @given(phrase=st.sampled_from([
        "задавать вопросы по одному за раз",
        "не использовать тире и дефисы в тексте",
        "отвечать коротко, длинные ответы человек не читает",
        "проверять факты, не выдумывать, не утверждать непроверенное",
        "сначала показать план, править код после согласования"]))
    def test_a_whole_phrase_from_the_set_still_counts(self, phrase):
        """Ожидание бывает не основой, а целой фразой — все пять из eval-cases.

        Прогони поле через стеммер, а фразу нет — и она не совпадёт ни с чем
        никогда: «задавать вопросы» в поле станет «задава вопрос». Отчёт назвал
        бы обрывом запись, которая лежит в базе дословно.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("правило", "человек просит %s" % phrase)])
            alive, _ = live.facts_with(base, [phrase])
            self.assertEqual(alive, 1, "фраза %r не нашлась дословно" % phrase)

    def test_a_short_expectation_is_not_thrown_away(self):
        """Ожидание короче трёх букв — тоже ожидание: `v2`, `БД`, `go`."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("версия", "перешли на v2 в проекте")])
            self.assertEqual(live.facts_with(base, ["v2"])[0], 1,
                             "короткое ожидание выброшено разметкой")

    def test_the_stem_of_the_set_is_not_stemmed_again(self):
        """«Казан» не должен укоротиться до «каза» и найти «казак».

        Ожидание набора — уже основа. Snowball не идемпотентен, и второй проход
        по нему увёл бы счёт вширь: ложная единица здесь хуже нуля, она
        пропускает настоящий обрыв и винит следующую ступень.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("казак", "человек видел казака")])
            self.assertEqual(live.facts_with(base, ["Казан"])[0], 0,
                             "ожидание набора прогнали через стеммер второй раз")

    def test_an_underscore_is_a_letter_and_not_a_wildcard(self):
        """`_` в слове — буква. Иначе счёт находит факт, которого нет.

        Тот же баг, ради которого в `storage/db.py` живёт `like()`: вопрос про
        `on_prompt` находил заодно `onXpromptXpy`. Ложная единица здесь хуже
        нуля — она пропускает настоящий обрыв и винит следующую ступень.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("onXpromptXpy", "правился onXpromptXpy")])
            self.assertEqual(live.facts_with(base, ["on_prompt"])[0], 0)
            self.assertEqual(live.facts_with(base, ["onXprompt"])[0], 1)

    def test_a_percent_sign_is_a_letter_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.a_base(tmp, [("скидка", "цена упала на 20 процентов")])
            self.assertEqual(live.facts_with(base, ["%"])[0], 0)

    def test_an_unreadable_base_is_not_reported_as_an_empty_one(self):
        """База не открылась — это не «фактов ноль».

        Ноль здесь означал бы «искали и не нашли», и отчёт назвал бы обрывом
        запись факта, ни разу в базу не заглянув.
        """
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "memory.db"
            broken.write_text("это не база", encoding="utf-8")
            alive, _ = live.facts_with(broken, ["овсян"])
            self.assertIsNone(alive, "нечитаемая база выдана за пустую")


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


def _a_row(id, ok):
    return {"id": id, "aim": "apply", "ok": ok, "injected": ok,
           "intruded": False, "error": None, "reason": None}


class FakeReport:
    """Отчёт руки: `passed` строк применили факт из `total`, все цели `apply`.

    Ряды нужны, а не только счёт: `Bout.window_line` считает долю по
    `report.asked` (`live.share_of_aim`), и фейку нужно то же сырьё, что несёт
    настоящий отчёт.
    """

    def __init__(self, passed, total):
        self.asked = ([_a_row("p%d" % i, True) for i in range(passed)]
                     + [_a_row("f%d" % i, False) for i in range(total - passed)])
        self.root = Path("/тут/песочница")
        self.probe = {}

    @property
    def passed(self):
        return sum(1 for row in self.asked if live.bucket(row) == live.APPLIED)

    @property
    def total(self):
        return len(self.asked)

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

    @SLOW
    @given(passed=st.integers(min_value=0, max_value=5))
    def test_the_bare_arm_leaves_no_sandbox_behind(self, passed):
        """Голая рука песочницу не копит: её ноль — ожидаемый, а не поломка.

        Рука играет с выключенным контуром и обязана давать около нуля.
        Оставляй мы её песочницу, каждый прогон `--arms both` копил бы на диске
        по каталогу, а обещано это только руке с памятью.
        """
        self.assertFalse(live.keep_after(passed=passed, done=True, asked=False,
                                         arm="bare"))
        self.assertTrue(live.keep_after(passed=passed, done=True, asked=True,
                                        arm="bare"), "явную просьбу уважаем")

    def test_a_broken_probe_does_not_cost_the_run_its_evidence(self):
        """Поломка разбора цепочки не уносит песочницу оплаченного прогона.

        Цепочка считается после обеих сессий, то есть после всех трат. Дай ей
        упасть наружу — и прогон уйдёт в уборку недосчитанным, унеся базу,
        ленту и разговоры, ради которых всё и затевалось.
        """
        class Boom(dict):
            """Разговоры, на которых разбор спотыкается на полпути."""

            def get(self, *_):
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "обрубленный хвост")

        with tempfile.TemporaryDirectory() as tmp:
            box = mock.Mock(db=Path(tmp) / "memory.db")
            probe = live.safe_half(
                box, {"id": "пара", "expect": ["овсян"],
                      "tell": [{"say": "было дело", "place": "кухня"}]}, Boom())
        self.assertIsNone(probe["facts"], "неизвестное выдано за ноль")
        self.assertFalse(probe["marked"])
        # Ступень, про которую разбор ничего не узнал, обрывом не называется:
        # иначе поломка отчёта читалась бы как поломка памяти.
        self.assertEqual(live.break_of(dict(probe, ok=False)), "разметка")

    def test_a_kept_sandbox_prints_its_path(self):
        """Путь к сохранённой песочнице печатается: иначе её не найти."""
        report = live.Report(mock.Mock(root=Path("/тут/песочница")), "claude", [])
        report.kept = True
        self.assertIn("/тут/песочница", report.text())


if __name__ == "__main__":
    unittest.main()
