#!/usr/bin/env python3
"""Агент видит ключ подсказки и отвечает по нему, помогла она или нет.

Запуск: python3 -m unittest tests.test_inline_verdict -v

Лента обращений умеет принимать ответ про пользу, но взять его было неоткуда:
единственный проложенный способ — догадка по архиву, «ход дошёл до конца».
Самого агента никто не спрашивал, хотя он и есть тот, кто знает, пригодилась
подсказка или нет.

Спросить нельзя, пока агент не видит, о чём его спрашивают. У вставки есть
ключ — разговор и время, — он лежит в хранилище и в ленте, но в текст, который
агент читает, не попадает. За ход подсказок бывает несколько, и ответ без
ключа повисает между ними.

**Правила, записанные до кода.**

1. У говорящего захода ключ вставки стоит в тексте подсказки. Тот самый, каким
   лента ключует показ, а не похожий.
2. Ответ агента принимается только по известному ключу. Выдуманный ключ в ленту
   не идёт: иначе доля пользы правится текстом ответа.
3. Ответ достаётся своей вставке. Два вброса в одном ходе — два ключа, и ответ
   по одному не трогает второй.
4. Ответ снят своим способом (`inline`) и с догадкой по архиву не складывается:
   у одного показа было бы два ответа, и «помог M из N» перестало бы сходиться.
5. Служебный блок с ответом человеку не показывают. Срезает его та же точка и
   те же маркеры, что и разметку фактов, — второй набор маркеров означал бы
   второй разбор в горячей точке печати.
6. Строка ответа не считается отброшенным фактом. Доля отброшенного — это
   сигнал, что просьба разошлась со схемой; свои же служебные строки в ней
   означают шум ровно там, где нужен сигнал.
"""
import contextlib, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import ledger, marks, models
from pipeline import display, prompt, suggest, understand
from storage import local, port

SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow,
                                       HealthCheck.function_scoped_fixture])

# Разговор: то, чем его называет харнесс. Разделитель ключа внутрь не берём —
# по нему ключ и разбирается обратно.
TALKS = st.text(alphabet="abcdef0123456789-", min_size=4, max_size=16) \
          .map(str.strip).filter(lambda s: s and "|" not in s)

# Время вставки: ISO с микросекундами. Два вброса в одном ходе различаются
# именно им, поэтому в списках требуем уникальности.
STAMPS = st.integers(min_value=0, max_value=999999).map(
    lambda n: "2026-09-01T12:00:00.%06d+00:00" % n)

VERDICTS = st.sampled_from(ledger.VERDICTS)
QUERIES = st.text(alphabet="абвгдежз ", min_size=1, max_size=20).map(str.strip) \
            .filter(bool)


@contextlib.contextmanager
def tape():
    """Своя лента и свой журнал вставок на время проверки."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(ledger, "LOG", Path(tmp) / "ledger.jsonl"), \
             mock.patch.object(suggest, "LOG", Path(tmp) / "suggest-log.jsonl"):
            yield Path(tmp)


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


class Answering:
    """Дверь, отвечающая заданным. Подменяем ответ хранилища, а не его."""

    name = "local"

    def __init__(self, answer=""):
        self.answer = answer

    def read(self, query, mode="single"):
        return self.answer

    def write(self, text, wait=False):
        return port.door().write(text, wait)

    def write_objects(self, records, relations=(), op="create"):
        return port.door().write_objects(records, relations, op)


def fact_piece(content="память тут была"):
    return json.dumps([dict(object_type="Fact", fact_type="project_state",
                            subject="демо", scope="project",
                            content="%s Оценка уверенности: 0.90" % content)],
                      ensure_ascii=False)


def use_line(key, verdict):
    return json.dumps({"injection": key, "used": verdict}, ensure_ascii=False)


def reply_with(lines):
    """Ответ агента со служебным блоком в конце — так его пишет модель."""
    sch = marks.scheme()
    return "Готово.\n\n%s\n%s\n%s" % (sch.begin, "\n".join(lines), sch.end)


def note(talk, at):
    """Строка журнала о вставке: только по ней ответ и считается известным."""
    suggest.remember(suggest.injection_of(talk, "подсказка", at))


def transcript(tmp, talk, replies, at="2026-09-01T13:00:00+00:00"):
    """Файл архива с одним эпизодом, в ответах которого стоит блок."""
    path = Path(tmp) / "talk.jsonl"
    rows = [{"sessionId": talk, "timestamp": at, "cwd": str(tmp),
             "type": "user", "message": {"content": "сделай"}}]
    rows.append({"sessionId": talk, "timestamp": at, "cwd": str(tmp),
                 "type": "assistant",
                 "message": {"content": [{"type": "text", "text": reply}
                                         for reply in replies]}})
    # Каждая строка с переводом в конце: недописанную разбор архива не берёт,
    # и файл без него теряет последнюю — ровно ту, где стоит блок.
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in rows), encoding="utf-8")
    return [path]


class TestTheKeyIsVisibleToTheAgent(unittest.TestCase):
    """Правило 1. Ключ вставки стоит в тексте подсказки, и он тот самый."""

    @SLOW
    @given(talk=TALKS, query=QUERIES)
    def test_a_speaking_pass_prints_the_key_of_its_own_injection(self, talk, query):
        with tape(), tempfile.TemporaryDirectory() as tmp, store(tmp):
            text, _kept, why = suggest.attend(query, session_id=talk,
                                              door=Answering(fact_piece()),
                                              record=False)
            self.assertIsNone(why, "память промолчала, проверять нечего")
            shown = [row for row in ledger.rows() if row["event"] == "injected"]
            self.assertEqual(len(shown), 1)
            key = ledger.key_of(shown[0]["session_id"], shown[0]["injected_at"])
            self.assertIn(key, text, "ключ вставки до агента не дошёл")

    @SLOW
    @given(talk=TALKS, at=STAMPS)
    def test_the_key_goes_apart_into_the_pair_it_was_made_of(self, talk, at):
        """Ключ самодостаточен: по нему видно и разговор, и время вставки."""
        self.assertEqual(ledger.key_parts(ledger.key_of(talk, at)), (talk, at))

    @SLOW
    @given(talk=TALKS, query=QUERIES)
    def test_the_agent_is_also_told_what_to_do_with_the_key(self, talk, query):
        """Голый ключ ничего не значит: рядом стоит просьба ответить по нему."""
        with tape(), tempfile.TemporaryDirectory() as tmp, store(tmp):
            text, _kept, _why = suggest.attend(query, session_id=talk,
                                               door=Answering(fact_piece()),
                                               record=False)
            shown = [row for row in ledger.rows() if row["event"] == "injected"][0]
            key = ledger.key_of(shown["session_id"], shown["injected_at"])
            self.assertIn(prompt.used([key]), text, "просьба ответить не пришла")

    def test_a_silent_pass_asks_nothing(self):
        """Не о чем спрашивать — не спрашиваем: ключа без вставки не бывает."""
        with tape():
            text, _kept, why = suggest.attend("альфа", session_id="talk",
                                              door=Answering(""), record=False)
            self.assertEqual(text, "")
            self.assertEqual(why, "not_found")


class TestTheAnswerLandsInItsOwnInjection(unittest.TestCase):
    """Правила 2 и 3. Ответ принимается по известному ключу и по своему."""

    @SLOW
    @given(talk=TALKS,
           pairs=st.lists(st.tuples(STAMPS, VERDICTS), min_size=1, max_size=4,
                          unique_by=lambda p: p[0]))
    def test_each_answer_reaches_the_injection_it_names(self, talk, pairs):
        with tape() as tmp:
            for at, _verdict in pairs:
                note(talk, at)
            files = transcript(tmp, talk, [reply_with(
                [use_line(ledger.key_of(talk, at), verdict)
                 for at, verdict in pairs])])
            suggest.harvest(files)
            got = ledger.verdicts(ledger.rows(), source="inline")
            self.assertEqual(got, {(talk, at): verdict for at, verdict in pairs},
                             "ответ достался не своей вставке")

    @SLOW
    @given(talk=TALKS, mine=STAMPS, alien=STAMPS, verdict=VERDICTS)
    def test_an_unknown_key_is_not_accepted(self, talk, mine, alien, verdict):
        """Выдуманный ключ в ленту не идёт: доля пользы не правится текстом."""
        with tape() as tmp:
            note(talk, mine)
            files = transcript(tmp, talk, [reply_with(
                [use_line(ledger.key_of(talk, alien), verdict)])])
            suggest.harvest(files)
            got = ledger.verdicts(ledger.rows(), source="inline")
            expected = {(talk, alien): verdict} if alien == mine else {}
            self.assertEqual(got, expected, "принят ответ по чужому ключу")

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS)
    def test_a_second_pass_does_not_double_the_answer(self, talk, at, verdict):
        """Проход по архиву идёт целиком каждый раз, а ответ остаётся один."""
        with tape() as tmp:
            note(talk, at)
            files = transcript(tmp, talk, [reply_with(
                [use_line(ledger.key_of(talk, at), verdict)])])
            first = suggest.harvest(files)
            second = suggest.harvest(files)
            rows = [row for row in ledger.rows() if row["event"] == "helped"]
            self.assertEqual(len(rows), 1, "ответ записан дважды")
            self.assertEqual(first["logged"], 1)
            self.assertEqual(second["logged"], 0)

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS)
    def test_the_answer_is_marked_by_the_way_it_was_taken(self, talk, at, verdict):
        """Правило 4. Способ съёма назван, и с догадкой по архиву не смешан."""
        with tape() as tmp:
            note(talk, at)
            files = transcript(tmp, talk, [reply_with(
                [use_line(ledger.key_of(talk, at), verdict)])])
            suggest.harvest(files)
            rows = [row for row in ledger.rows() if row["event"] == "helped"]
            self.assertEqual([row["source"] for row in rows], ["inline"])
            self.assertEqual(ledger.verdicts(ledger.rows(), source="transcript"), {},
                             "ответ агента засчитан как догадка по архиву")


class TestTheBlockNeverReachesTheHuman(unittest.TestCase):
    """Правило 5. Служебный блок срезает та же точка и те же маркеры."""

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS)
    def test_the_key_is_not_left_on_the_screen(self, talk, at, verdict):
        key = ledger.key_of(talk, at)
        body = reply_with([use_line(key, verdict)])
        self.assertNotIn(key, marks.strip(body), "ключ уехал человеку на экран")

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS)
    def test_the_display_point_cuts_it_line_by_line(self, talk, at, verdict):
        """Та же точка, что и у разметки фактов: второго разбора в печати нет."""
        key = ledger.key_of(talk, at)
        body = reply_with([use_line(key, verdict)])
        shown, inside = display.visible(body, False, marks.scheme())
        self.assertNotIn(key, shown)
        self.assertIn("Готово.", shown)
        self.assertFalse(inside, "блок остался открытым")

    def test_the_answer_needs_no_second_pair_of_markers(self):
        """Маркеры печатаются точке печати списком, и он не вырос."""
        sch = marks.scheme()
        self.assertEqual([sch.begin, sch.end],
                         [marks.XMD1_BEGIN, marks.XMD1_END])


class TestTheAnswerIsNotADroppedFact(unittest.TestCase):
    """Правило 6. Своя служебная строка не растит долю отброшенного."""

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS,
           facts=st.integers(min_value=0, max_value=3))
    def test_the_verdict_line_is_neither_a_fact_nor_a_loss(self, talk, at,
                                                           verdict, facts):
        good = [json.dumps({"type": "preference", "subject": "человек",
                            "predicate": "любит", "value": "краткость",
                            "source": "stated", "confidence": 0.9},
                           ensure_ascii=False)] * facts
        body = reply_with(good + [use_line(ledger.key_of(talk, at), verdict)])
        raw, bad = marks.units(body)
        kept, dropped = marks.to_facts(raw)
        self.assertEqual(bad, {})
        self.assertEqual(len(kept), facts, "факты разобрались не как раньше")
        self.assertEqual(sum(dropped.values()), 0,
                         "строка ответа посчитана отброшенным фактом")

    @SLOW
    @given(talk=TALKS, at=STAMPS, verdict=VERDICTS)
    def test_the_verdict_is_read_out_of_the_same_block(self, talk, at, verdict):
        key = ledger.key_of(talk, at)
        body = reply_with([use_line(key, verdict)])
        self.assertEqual(marks.uses(body), [(key, verdict)])

    @SLOW
    @given(talk=TALKS, at=STAMPS)
    def test_an_answer_nobody_understands_is_unknown_not_no(self, talk, at):
        """Непонятный ответ — «ответа нет». Свали его в «не помог» — и доля
        пользы поедет вниз от кривой строки, а не от бесполезной памяти."""
        key = ledger.key_of(talk, at)
        body = reply_with([json.dumps({"injection": key, "used": "ну как сказать"},
                                      ensure_ascii=False)])
        self.assertEqual(marks.uses(body), [(key, "unknown")])


class TestTheSummaryShowsEveryWayApart(unittest.TestCase):
    """Ответ, снятый одним способом, виден в сводке — и не смешан с другим.

    Сводка считала один способ, догадку по архиву. Ответ агента ложился в ленту
    и в сводке не появлялся вовсе: «без ответа», хотя ответ был. Молчащая
    сводка неотличима от неработающей петли, а чинить по ней и предлагается.
    """

    @SLOW
    @given(talk=TALKS, at=STAMPS, key=st.sampled_from(["a|b|project"]),
           inline=VERDICTS, guess=VERDICTS)
    def test_both_ways_are_counted_and_named(self, talk, at, key, inline, guess):
        with tape() as tmp:
            ledger.injected(talk, at, [key], query="вопрос")
            ledger.helped(talk, at, inline, source="inline")
            ledger.helped(talk, at, guess, source="transcript")
            got = ledger.report(ledger.LOG)
            self.assertIn("inline", got, "способ съёма в сводке не назван")
            self.assertIn("transcript", got)
            self.assertIn("показан 1", got)
            for source, verdict in (("inline", inline), ("transcript", guess)):
                field = {"yes": "помог 1", "no": "не помог 1",
                         "unknown": "без ответа 1"}[verdict]
                line = [row for row in got.splitlines()
                        if row.strip().startswith(source)]
                self.assertEqual(len(line), 1, "способ назван не один раз")
                self.assertIn(field, line[0], "ответ уехал в чужую колонку")

    def test_a_way_nobody_answered_is_not_printed(self):
        """Пустая колонка — шум: способов три, отвечен обычно один."""
        with tape():
            ledger.injected("t", "2026-09-01T00:00:00+00:00", ["a|b|project"])
            ledger.helped("t", "2026-09-01T00:00:00+00:00", "yes", source="inline")
            got = ledger.report(ledger.LOG)
            self.assertIn("inline", got)
            self.assertNotIn("turn_end", got, "напечатан способ, которым не спрашивали")
