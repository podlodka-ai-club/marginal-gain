#!/usr/bin/env python3
"""Форма вброса: чем именно найденное подаётся агенту.

Запуск: python3 -m pytest tests/test_voice.py -q

Прогон на трёх парах давал 2 из 3, и единственный обрыв стоял на последней
ступени: факт записан, найден, вброшен в контекст — и не использован. Ступени
до неё разобраны по именам, а форма вброса была одна, зашитая в `suggest`.
Одна форма не сравнивается ни с чем: «плохо подано» на ней неотличимо от «нечего
подавать».

Поэтому форм несколько, они лежат реестром `voice.VOICES`, и выбирает между
ними прогон, а не правка кода. Проверяется здесь не текст форм — он будет
меняться, — а то, что делает их сменными:

  содержание  форма меняет обёртку, а не то, что в ней. Каждый факт, дошедший
              до вброса, обязан быть в тексте любой формы;
  честность   ни одна форма не приписывает числа, которых ей не дали;
  различие    формы различаются по существу. Совпади они — сравнение форм
              меряло бы шум;
  цена        новая форма стоит одну правку одного файла: `suggest` знает
              точку входа и имя, про сами формы не знает ничего;
  выбор       имя формы берётся рубильником тем же порядком, что и все
              настройки, и попадает в отчёт прогона.

Свойствами, а не примерами: форм четыре (три рабочих и заглушка проверки), а
кусков на вброс приходит до пяти в любом сочетании «с оценкой / без оценки / с
обстановкой / без обстановки». Перебирать это руками — значит проверить три
сочетания из тридцати и не заметить остальных.

Мутации, на которых проверки обязаны краснеть:
  * реестр игнорируется, `render` всегда зовёт `plain`
        → TestTheVoicesDifferInSubstance, TestANewVoiceCostsOneFile
  * `directive` печатает уверенность          → TestTheDirectiveOrdersInsteadOfReporting
  * `inline` собирается блоком с заголовком    → TestTheInlineStandsNextToTheTask
  * форма теряет факт (печатает только первый) → TestEveryVoiceKeepsTheSubstance
  * имя формы не уходит в песочницу прогона    → TestTheRunNamesItsVoice
  * `--voice` не доезжает до отчёта            → TestTheRunNamesItsVoice
"""
import ast
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import live
from infra import config
from domain import context
from pipeline import suggest, voice

ROOT = Path(__file__).resolve().parent.parent

FAST = settings(deadline=None, max_examples=100)

# Слова содержания берём буквами, без цифр: проверка «форма не выдумала число»
# считает числа в готовом тексте, и цифра, пришедшая из самого факта, сделала
# бы её зелёной на любой форме.
WORDS = st.text(alphabet="абвгдежзиклмнопрстуфх ", min_size=1,
                max_size=40).filter(lambda s: s.strip())

# Обстановки — из настоящих осей: `describe` знает только их, и словарь с
# выдуманным ключом молча дал бы пустую строку вместо обстановки.
PLACES = st.sampled_from([
    None,
    {"project": "marginal-gain"},
    {"project": "job-hunt", "day_of_week": "monday"},
    {"working_directory": "/home/person/dev", "git_branch": "main"},
])

SCORES = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0,
                                        allow_nan=False, allow_infinity=False))
FITS = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0,
                                      allow_nan=False, allow_infinity=False))


@st.composite
def item(draw):
    """Один кусок в том виде, в каком его отдаёт порог: оценка, текст, запись."""
    where, fit = draw(PLACES), draw(FITS)
    record = None
    if where is not None or fit is not None:
        record = {"object_type": "Fact", "situation": where, "fit": fit}
    return (draw(SCORES), draw(WORDS), record)


KEPT = st.lists(item(), min_size=1, max_size=5)

# Куски, на которых формы обязаны разойтись: без содержания различать нечего.
SOLID = st.lists(item(), min_size=1, max_size=3)

NUMBER = re.compile(r"\d+[.,]\d+")


def numbers_given(kept):
    """Все числа, которые формам вообще дали: оценки и уместности."""
    out = set()
    for score, _text, record in kept:
        for value in (score, context.fit_of(record)):
            if value is not None:
                out.add(round(float(value), 2))
    return out


def normal(text):
    return " ".join(text.split())


class TestEveryVoiceKeepsTheSubstance(unittest.TestCase):
    """Форма меняет обёртку, а не содержание."""

    @given(kept=KEPT, name=st.sampled_from(sorted(voice.VOICES)))
    @FAST
    def test_no_voice_drops_a_fact(self, kept, name):
        """Каждый дошедший до вброса факт есть в тексте любой формы.

        Отсев кончился на пороге. Форма, теряющая кусок, — это молчаливая
        вторая ступень отсева, которой нет ни в одной разбивке.
        """
        got = voice.render(kept, name)
        for _score, text, _record in kept:
            self.assertIn(normal(text), got,
                          "форма %s потеряла факт %r" % (name, text))

    @given(kept=KEPT, name=st.sampled_from(sorted(voice.VOICES)))
    @FAST
    def test_every_voice_returns_a_string(self, kept, name):
        got = voice.render(kept, name)
        self.assertIsInstance(got, str)
        self.assertEqual(got, got.strip())

    @given(name=st.sampled_from(sorted(voice.VOICES)))
    @settings(deadline=None, max_examples=len(voice.VOICES))
    def test_nothing_kept_means_nothing_said(self, name):
        """Пусто на входе — пусто на выходе, у любой формы.

        Заголовок над пустым списком это подсказка без подсказки: агент читает
        «из памяти:» и не находит ничего.
        """
        self.assertEqual("", voice.render([], name))

    @given(kept=KEPT, name=st.sampled_from(sorted(voice.VOICES)))
    @FAST
    def test_the_same_input_gives_the_same_text(self, kept, name):
        """Форма — чистая функция: два одинаковых вброса читаются одинаково."""
        self.assertEqual(voice.render(kept, name), voice.render(kept, name))


class TestNoVoiceInventsANumber(unittest.TestCase):
    """Выдуманное число выглядит измеренным. Не приписываем."""

    @given(kept=KEPT, name=st.sampled_from(sorted(voice.VOICES)))
    @FAST
    def test_every_printed_number_was_given(self, kept, name):
        given_numbers = numbers_given(kept)
        for shown in NUMBER.findall(voice.render(kept, name)):
            self.assertIn(round(float(shown.replace(",", ".")), 2),
                          given_numbers,
                          "форма %s назвала число, которого ей не давали: %s"
                          % (name, shown))

    @given(kept=st.lists(st.tuples(st.none(), WORDS, st.none()),
                         min_size=1, max_size=4),
           name=st.sampled_from(sorted(voice.VOICES)))
    @FAST
    def test_without_measurements_no_voice_names_them(self, kept, name):
        """Ни оценки, ни обстановки не дали — ни одна форма их не называет."""
        got = voice.render(kept, name)
        self.assertNotIn("уверенность", got)
        self.assertNotIn("уместность", got)
        self.assertNotIn("обстановка", got)


class TestTheVoicesDifferInSubstance(unittest.TestCase):
    """Формы различаются по существу, а не по запятым."""

    @given(kept=SOLID)
    @settings(deadline=None, max_examples=60)
    def test_no_two_voices_read_alike(self, kept):
        """Совпади две формы — сравнение форм мерило бы шум.

        Мутация: заставить реестр всегда отдавать `plain` — краснеет здесь.
        """
        said = {name: voice.render(kept, name) for name in voice.SHIPPED}
        for one in said:
            for other in said:
                if one < other:
                    self.assertNotEqual(said[one], said[other],
                                        "формы %s и %s совпали" % (one, other))

    @given(kept=SOLID)
    @settings(deadline=None, max_examples=60)
    def test_the_shape_of_the_block_differs_too(self, kept):
        """Различие не в словах, а в устройстве: блок против одной строки."""
        self.assertGreater(len(voice.render(kept, "plain").splitlines()), 1)
        self.assertEqual(1, len(voice.render(kept, "inline").splitlines()))


class TestTheDirectiveOrdersInsteadOfReporting(unittest.TestCase):
    """`directive` — то же содержание указанием, без наших чисел."""

    @given(kept=SOLID)
    @settings(deadline=None, max_examples=60)
    def test_it_never_shows_our_numbers(self, kept):
        got = voice.render(kept, "directive")
        self.assertNotIn("уверенность", got)
        self.assertNotIn("уместность", got)

    @given(kept=SOLID)
    @settings(deadline=None, max_examples=60)
    def test_it_opens_with_an_instruction(self, kept):
        """Первая строка — указание, а не заголовок справки."""
        first = voice.render(kept, "directive").splitlines()[0]
        self.assertIn("Учитывай", first)
        self.assertNotIn("Из памяти", first)


class TestTheInlineStandsNextToTheTask(unittest.TestCase):
    """`inline` — факт рядом с задачей, а не блоком сверху."""

    @given(kept=SOLID)
    @settings(deadline=None, max_examples=60)
    def test_it_has_no_block_of_its_own(self, kept):
        got = voice.render(kept, "inline")
        self.assertNotIn("\n", got)
        self.assertNotIn("Из памяти", got)
        self.assertFalse(got.lstrip().startswith("-"),
                         "строка списка — это всё тот же блок: %r" % got)


class TestTheChoiceComesFromTheSwitch(unittest.TestCase):
    """Имя формы берётся тем же порядком, что и все прочие настройки."""

    @given(name=st.sampled_from(sorted(voice.VOICES)))
    @settings(deadline=None, max_examples=len(voice.VOICES))
    def test_the_environment_names_the_voice(self, name):
        with mock.patch.dict(os.environ, {"XMEM_VOICE": name}):
            self.assertEqual(name, voice.name())

    @given(junk=st.text(max_size=12).filter(
        lambda s: s.strip() not in voice.VOICES and "\x00" not in s))
    @FAST
    def test_an_unknown_name_falls_back_to_the_default(self, junk):
        """Мусор в рубильнике не имеет права уронить горячий путь.

        Подсказка идёт в ходе человека: форма, которой нет, обязана дать
        опорную форму, а не исключение посреди разговора.
        """
        with mock.patch.dict(os.environ, {"XMEM_VOICE": junk}):
            self.assertEqual(voice.DEFAULT, voice.name())
            self.assertTrue(voice.render([(0.9, "факт", None)]))

    def test_the_default_is_the_baseline(self):
        """Опора — `plain`: с ней сравниваются остальные."""
        self.assertEqual("plain", voice.DEFAULT)
        self.assertIn(voice.DEFAULT, voice.VOICES)

    @given(name=st.sampled_from(sorted(voice.VOICES)))
    @settings(deadline=None, max_examples=len(voice.VOICES))
    def test_the_argument_beats_the_environment(self, name):
        """Названная явно форма сильнее рубильника: так ходит проверка."""
        other = next(one for one in sorted(voice.VOICES) if one != name)
        with mock.patch.dict(os.environ, {"XMEM_VOICE": other}):
            self.assertEqual(name, voice.name(name))


def stub(kept):
    """Четвёртая форма, заведённая проверкой. Ничего своего не говорит."""
    return "заглушка: %d" % len(kept)


class TestANewVoiceCostsOneFile(unittest.TestCase):
    """Новая форма — одна правка одного файла, и ни строчки в пайплайне."""

    def test_the_pipeline_touches_the_entry_point_and_nothing_else(self):
        """`suggest` знает точку входа, и только её.

        По разбору исходника, а не по вере: `voice.plain(...)` или
        `voice.VOICES[...]` в пайплайне — это и есть та правка, которой при
        заведении новой формы быть не должно. Проверяем обращения к модулю, а
        не слова в тексте: `plain` — ещё и обычное имя переменной.
        """
        tree = ast.parse((ROOT / "pipeline" / "suggest.py")
                         .read_text(encoding="utf-8"))
        touched = {node.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Attribute)
                   and isinstance(node.value, ast.Name)
                   and node.value.id == "voice"}
        self.assertEqual({"render"}, touched,
                         "пайплайн трогает у формы не только точку входа: %s"
                         % sorted(touched))

    def test_a_voice_added_to_the_registry_works_at_once(self):
        """Заглушка заводится записью в реестр и работает через ту же точку."""
        with mock.patch.dict(voice.VOICES, {"stub": stub}):
            self.assertEqual("заглушка: 1",
                             voice.render([(0.9, "факт", None)], "stub"))

    def test_the_pipeline_picks_the_new_voice_up_by_name(self):
        """И доезжает до агента: форму пайплайн берёт рубильником, не кодом.

        Мутация: заставить реестр всегда отдавать `plain` — краснеет здесь.
        """
        door = Door([{"object_type": "Fact", "fact_type": "user",
                      "subject": "город", "scope": "global",
                      "content": "переехал в Казань"}])
        with mock.patch.dict(voice.VOICES, {"stub": stub}), \
                mock.patch.dict(os.environ, {"XMEM_VOICE": "stub"}):
            text, kept, _raw, why, _found = suggest.consult("город", door=door,
                                                            min_score=0.0)
        self.assertIsNone(why)
        self.assertEqual("заглушка: %d" % len(kept), text)

    @given(name=st.sampled_from(sorted(voice.SHIPPED)))
    @settings(deadline=None, max_examples=len(voice.SHIPPED))
    def test_the_pipeline_speaks_in_the_chosen_voice(self, name):
        door = Door([{"object_type": "Fact", "fact_type": "user",
                      "subject": "город", "scope": "global",
                      "content": "переехал в Казань"}])
        with mock.patch.dict(os.environ, {"XMEM_VOICE": name}):
            text, kept, _raw, why, _found = suggest.consult("город", door=door,
                                                            min_score=0.0)
        self.assertIsNone(why)
        self.assertEqual(voice.render(kept, name), text)


class Door:
    """Дверь, отдающая заранее известные записи. Обхода по графу не умеет."""

    name = "fake"

    def __init__(self, records):
        self.records = records

    def read(self, query, mode="single"):
        return json.dumps({"answer": json.dumps(self.records,
                                                ensure_ascii=False)},
                          ensure_ascii=False)


class TestTheRunNamesItsVoice(unittest.TestCase):
    """Цифра без имени формы несравнима."""

    def test_the_key_offers_exactly_the_registry(self):
        """`--voice` предлагает ровно то, что есть в реестре.

        Разойдись список с реестром — ключ либо не пустит рабочую форму, либо
        пустит несуществующую, и прогон пошёл бы опорной, назвавшись чужой.
        """
        keys = {one.dest: one for one in live.parser()._actions}
        self.assertIn("voice", keys)
        self.assertEqual(sorted(voice.VOICES), sorted(keys["voice"].choices))

    @given(name=st.sampled_from(sorted(voice.VOICES)))
    @settings(deadline=None, max_examples=len(voice.VOICES))
    def test_the_sandbox_hands_the_voice_to_the_turns(self, name):
        """Имя формы уходит в окружение ходов: хук берёт форму оттуда."""
        box = live.Sandbox(root="/tmp/не-открывается", voice=name)
        self.assertEqual(name, box.env({})["XMEM_VOICE"])

    def test_without_a_key_the_run_names_the_default(self):
        """Форму не назвали — прогон идёт опорной и говорит об этом.

        Пустая строка в окружении гасит рубильник, оставшийся в профиле
        пользователя: иначе цифра меняется молча, от чужой настройки.
        """
        box = live.Sandbox(root="/tmp/не-открывается")
        self.assertEqual("", box.env({})["XMEM_VOICE"])

    @given(name=st.sampled_from(sorted(voice.VOICES)))
    @settings(deadline=None, max_examples=len(voice.VOICES))
    def test_the_report_says_which_voice_it_ran(self, name):
        box = live.Sandbox(root="/tmp/не-открывается", voice=name)
        report = live.Report(box, live.Agent.name, [])
        said = [line for line in report.text().splitlines() if "форма" in line]
        self.assertTrue(said, "отчёт не называет форму вброса вовсе")
        self.assertTrue(any(name in line for line in said),
                        "отчёт назвал не ту форму: %s" % said)

    def test_the_report_names_the_effective_voice(self):
        """Отчёт называет форму, которой прогон шёл, а не ту, что попросили."""
        box = live.Sandbox(root="/tmp/не-открывается", voice="нет-такой")
        report = live.Report(box, live.Agent.name, [])
        self.assertIn(voice.DEFAULT,
                      [line for line in report.text().splitlines()
                       if "форма" in line][0])

    def chain_of(self, where):
        """Кому в этом теле передают имя формы: имя вызванного -> что передали."""
        tree = ast.parse((ROOT / "eval" / "live.py").read_text(encoding="utf-8"))
        body = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == where)
        out = {}
        for node in ast.walk(body):
            if not isinstance(node, ast.Call):
                continue
            what = node.func
            called = what.attr if isinstance(what, ast.Attribute) else \
                getattr(what, "id", "")
            for word in node.keywords:
                if word.arg == "voice":
                    out[called] = ast.unparse(word.value)
        return out

    def test_the_key_reaches_the_run(self):
        """`--voice` доезжает до прогона, а не оседает в разборе.

        Разбор без передачи — это прогон, честно печатающий имя формы и идущий
        опорной: цифра уехала бы под чужим именем.
        """
        self.assertEqual("args.voice", self.chain_of("main").get("run"),
                         "разобранное имя формы прогону не передают")

    def test_the_run_reaches_the_sandbox(self):
        """А прогон передаёт его песочнице: ходы берут форму из её окружения."""
        self.assertEqual("voice", self.chain_of("run").get("Sandbox"),
                         "прогон не передаёт имя формы песочнице")


class TestTheSwitchIsDocumented(unittest.TestCase):
    """Рубильник, о котором не написано, для чужой машины не существует."""

    def test_the_environment_key_is_named_in_the_memo(self):
        body = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("XMEM_VOICE", body)


if __name__ == "__main__":
    unittest.main()
