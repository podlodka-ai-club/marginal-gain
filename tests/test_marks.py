#!/usr/bin/env python3
"""Разметка фактов моделью. Запуск: python3 -m unittest tests.test_marks -v

Проверки отвечают на пять вопросов, и каждый из них уже однажды оказывался
местом, где зелёный тест ничего не доказывал:

1. Просьба уходит в запрос одинаково у разных переносчиков и без веток по
   вендору. Иначе механика привязана к одному агенту, а выглядит общей.
2. Факты берутся из блока, а не шаблоном по тексту. Убрать блок — фактов нет.
3. Схема хранилища на месте: ключ, типы, адресация из Association.
4. Знание о внешнем формате заперто в маппере: переименование поля снаружи
   краснит маппер и ничего больше.
5. Блок служебный. В том, что видит человек, его нет; в записи разговора есть.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import marks, models
from infra import config
from pipeline import display, understand

HERE = Path(__file__).resolve().parent.parent
HOOKS = HERE / "hooks"

# Единица внешней разметки в том виде, в каком её пишет модель.
UNIT = {"type": "preference", "subject": "длина ответа",
        "predicate": "человек просит", "value": "отвечать коротко",
        "time": "2026-08-28T10:00:00+00:00", "source": "stated", "confidence": 0.9}


def block_of(*units):
    """Ответ модели с блоком разметки в конце — как он ляжет в транскрипт."""
    lines = [marks.XMD1_BEGIN]
    lines += [json.dumps(u, ensure_ascii=False) for u in units]
    lines.append(marks.XMD1_END)
    return "Готово, поправил три файла.\n\n" + "\n".join(lines)


def episode(*replies, **kw):
    ep = {"session_id": "s1", "number": 1, "request": "сделай коротко",
          "started_at": "2026-08-28T10:00:00Z", "ended_at": "2026-08-28T10:05:00Z",
          "cwd": "/Users/person/dev/marginal-gain", "branch": "memory-encoder",
          "files": [], "commands": [], "replies": list(replies), "errors": []}
    ep.update(kw)
    return ep


class TestAskReachesTheRequest(unittest.TestCase):
    """Просьба одна на всех переносчиков и не знает ни одного вендора."""

    def test_the_same_line_goes_out_through_two_different_carriers(self):
        """Хук одного агента и голый вывод для любого другого — строка одна.

        Двух провайдеров здесь изображают два переносчика: путь хука, где
        запрос приходит json на вход, и путь `python3 -m pipeline.prompt`,
        которым просьбу забирает обвес, ничего про наши хуки не знающий.
        Совпадать они обязаны посимвольно: разошлись — значит одна половина
        сравнения просит одно, а разбирает другое.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, XMEM_LIVE="1", XMEM_BACKEND="local",
                       XMEM_LOCAL_PATH=str(Path(tmp) / "memory.db"),
                       XMEM_HOOK_SECONDS="5", PYTHONPATH=str(HERE))
            payload = json.dumps({"prompt": "что по задаче", "session_id": "ask-1"})
            hook = subprocess.run(["bash", str(HOOKS / "on_prompt_read.sh")],
                                  input=payload, env=env, capture_output=True,
                                  text=True, timeout=60)
            plain = subprocess.run([sys.executable, "-m", "pipeline.prompt"],
                                   env=env, capture_output=True, text=True,
                                   cwd=str(HERE), timeout=60)
        self.assertEqual(hook.returncode, 0, hook.stderr[-300:])
        self.assertEqual(plain.returncode, 0, plain.stderr[-300:])
        line = plain.stdout.strip()
        self.assertTrue(line, "голый переносчик промолчал")
        self.assertIn(line, hook.stdout, "у хука просьба другая или её нет вовсе")
        self.assertEqual(line, marks.ask())

    def test_no_vendor_knows_its_name_on_the_ask_path(self):
        """Ни имени вендора, ни ветки по нему в коде пути просьбы.

        Смотрим на код, а не на текст файла: в пояснении вендор упомянуть
        можно и нужно, в условии — нельзя. Разбираем дерево и берём имена и
        строковые значения, минуя описания модулей и функций.
        """
        vendors = ("anthropic", "openai", "gemini", "mistral", "cohere",
                   "ollama", "claude", "gpt", "llama")
        offenders = []
        for name in ("domain/marks.py", "infra/config.py", "pipeline/prompt.py"):
            tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
            # Сырой текст описания, без вычистки отступов: сравнивать будем
            # с константой из дерева, а вычищенное с ней не совпадает.
            docs = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    found = node.id.lower()
                elif isinstance(node, ast.Attribute):
                    found = node.attr.lower()
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in docs:
                        continue
                    found = node.value.lower()
                else:
                    continue
                offenders += ["%s: %s" % (name, v) for v in vendors if v in found]
        self.assertEqual(sorted(set(offenders)), [])


class TestFactsComeFromTheBlock(unittest.TestCase):
    """Разбор берёт готовое, а не угадывает регэкспом по тексту."""

    def test_marked_unit_becomes_a_fact_with_the_key_the_model_named(self):
        facts, dropped = marks.facts_of(episode(block_of(UNIT)))
        self.assertEqual(facts, [("preference", "длина ответа", "global",
                                  "человек просит отвечать коротко")])
        self.assertEqual(dropped, Counter())
        self.assertEqual(marks.key(facts[0]),
                         ("mark", "preference", "длина ответа", "global"))

    def test_without_the_block_nothing_is_marked(self):
        """Мутация, обязательная для этой ветки: убрать блок из ответа.

        Тот же ответ без разметки не даёт ни одного размеченного факта —
        значит, факты пришли из блока, а не из слов вокруг него.
        """
        plain = "Готово, поправил три файла. Человек просит отвечать коротко."
        facts, dropped = marks.facts_of(episode(plain))
        self.assertEqual(facts, [])
        self.assertEqual(dropped, Counter())

    def test_broken_block_is_counted_not_guessed(self):
        """Вторая мутация: блок на месте, разбор сломан. Тот же красный.

        Битую строку не спасаем эвристикой: разметка либо разбирается, либо
        считается отброшенной. Молчаливое угадывание вернуло бы нас ровно к
        тому, от чего эта работа уходит.
        """
        answer = "\n".join([marks.XMD1_BEGIN, "{это не json}", marks.XMD1_END])
        facts, dropped = marks.facts_of(episode(answer))
        self.assertEqual(facts, [])
        self.assertEqual(dropped["json"], 1)

    def test_pipeline_prefers_marked_facts_over_the_old_rules(self):
        """Конвейер: есть разметка — берём её, нет — работают прежние правила."""
        marked, _ = understand.marked_or_guessed(episode(block_of(UNIT)))
        self.assertEqual([f for f, _ in marked],
                         [("preference", "длина ответа", "global",
                           "человек просит отвечать коротко")])
        self.assertEqual([k[0] for _, k in marked], ["mark"])

        ep = episode("просто ответ", files=["/Users/person/dev/marginal-gain/a.py"])
        guessed, _ = understand.marked_or_guessed(ep)
        self.assertTrue(guessed, "без разметки прежние правила молчат")
        self.assertNotIn("mark", [k[0] for _, k in guessed])


class TestStorageSchemaStaysWhereItWas(unittest.TestCase):
    """Схема хранилища этой работой не двигается. Ключ адресуют связи."""

    def test_fact_key_and_types_are_the_same(self):
        self.assertEqual(models.Fact.KEY, ("fact_type", "subject", "scope"))
        self.assertEqual(models.FACT_TYPES,
                         ("user", "preference", "project_state", "external_resource"))
        self.assertEqual(models.SCOPES, ("project", "global"))

    def test_association_still_finds_a_marked_fact_by_the_old_key(self):
        """Association адресует факт строкой fact_type|subject|scope.

        Размеченный факт находится ею так же, как вырезанный шаблоном: иначе
        смена источника фактов молча порвала бы все связи.
        """
        facts, _ = marks.facts_of(episode(block_of(UNIT)))
        fact = models.Fact(*facts[0]).validate()
        self.assertEqual(fact.identity(), "preference|длина ответа|global")
        link = models.Association(source_key=fact.identity(), target_key="x|y|global",
                                  cue="same_episode", weight=1.0)
        self.assertEqual(link.key()["source_key"], fact.identity())

    def test_the_schema_knows_nothing_about_the_markup(self):
        """Схема хранилища не знает про внешнюю разметку — ни словом.

        Прежде тут стояла проверка диффа: `domain/models.py` не должен был
        появляться в `git diff HEAD`. Она держалась ровно до первой законной
        правки схемы по другой задаче и краснела на ней, ничего не говоря про
        разметку. Требование то же, но сказано про содержимое, а не про
        состояние рабочего дерева: внешний формат знают двое — просьба и
        маппер, и схема к ним не относится.
        """
        body = (HERE / "domain" / "models.py").read_text(encoding="utf-8").lower()
        for word in ("marks", "xmd1", "memory-facts", "confidence", "predicate"):
            self.assertNotIn(word, body,
                             "схема заговорила про внешнюю разметку: %s" % word)


class TestExternalFormatIsLockedInTheMapper(unittest.TestCase):
    """Наружу смотрят двое: текст просьбы и маппер. Больше никто."""

    def test_renaming_a_field_outside_reddens_the_mapper_alone(self):
        """Мутация: снаружи переименовали поле. Падает маппер, не схема."""
        renamed = dict(UNIT)
        renamed["object"] = renamed.pop("subject")
        fact, reason = marks.xmd1_unit(renamed)
        self.assertIsNone(fact)
        self.assertEqual(reason, "empty")

    def test_unknown_type_is_dropped_with_a_reason(self):
        """Типов снаружи больше, чем наших четырёх. Что не сплющилось — прочь."""
        _, dropped = marks.to_facts([dict(UNIT, type="блаженство")])
        self.assertEqual(dropped, Counter({"type": 1}))

    def test_guessed_units_are_not_written_at_all(self):
        """Источник и уверенность — фильтр на входе, а не поле хранилища."""
        kept, dropped = marks.to_facts([dict(UNIT, source="inferred"),
                                        dict(UNIT, confidence=0.1),
                                        UNIT])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, Counter({"source": 1, "confidence": 1}))
        self.assertNotIn("source", models.Fact.__dataclass_fields__)
        self.assertNotIn("confidence", models.Fact.__dataclass_fields__)

    def test_scope_is_derived_not_asked(self):
        """Область выводится из типа: спросишь — модель выдумает третью."""
        kept, _ = marks.to_facts([dict(UNIT, type="resource", subject="графана",
                                       predicate="адрес", value="http://x")])
        self.assertEqual(kept[0][2], "project")


class TestRegistryTakesMoreThanOneMapper(unittest.TestCase):
    """Мапперов будет несколько: второй добавляется одной записью."""

    OTHER = {"kind": "preference", "who": "длина ответа", "says": "коротко"}

    @staticmethod
    def other_unit(raw):
        if raw.get("kind") not in models.FACT_TYPES:
            return None, "type"
        return (raw["kind"], raw["who"], "global", raw["says"]), ""

    def scheme(self):
        return marks.Scheme("other", "просьба второй схемы", "<<<B", "B>>>",
                            self.other_unit)

    def test_second_mapper_is_one_entry_and_the_pipeline_does_not_move(self):
        """Одна запись в реестре — и тот же вход идёт через оба маппера.

        Ни конвейер, ни хранилище про вторую схему не знают: она называется
        настройкой, и цифры двух схем на одном входе сравнимы.
        """
        with mock.patch.dict(marks.SCHEMES, {"other": self.scheme()}), \
             mock.patch.object(marks, "NAMES", marks.NAMES + ["other"]):
            first, _ = marks.to_facts([UNIT], name="xmd1")
            second, _ = marks.to_facts([self.OTHER], name="other")
            self.assertEqual(len(first), len(second))
            self.assertEqual(first[0][:3], second[0][:3])

            answer = "текст\n<<<B\n%s\nB>>>" % json.dumps(self.OTHER, ensure_ascii=False)
            with mock.patch.dict(os.environ, {"XMEM_MARKS": "other"}):
                self.assertEqual(config.marks(), "other")
                self.assertEqual(marks.ask(), "просьба второй схемы")
                facts, _ = marks.facts_of(episode(answer))
                self.assertEqual(facts, [("preference", "длина ответа", "global",
                                          "коротко")])

    def test_removing_a_mapper_from_the_registry_falls_loudly(self):
        """Мутация: убрать маппер из реестра. Его прогон падает, соседний жив."""
        with mock.patch.dict(os.environ, {"XMEM_MARKS": "other"}):
            with self.assertRaises(marks.UnknownScheme):
                marks.ask()
        self.assertTrue(marks.ask("xmd1"))


class TestHumanNeverSeesTheBlock(unittest.TestCase):
    """Блок — служебный канал между моделью и конвейером."""

    def test_the_block_is_cut_from_what_is_shown_and_kept_in_the_record(self):
        answer = block_of(UNIT)
        shown = marks.strip(answer)
        self.assertNotIn(marks.XMD1_BEGIN, shown)
        self.assertNotIn(marks.XMD1_END, shown)
        self.assertNotIn("confidence", shown)
        self.assertEqual(shown, "Готово, поправил три файла.")
        # В записи разговора блок остаётся: оттуда его читает разбор.
        self.assertIn("длина ответа", marks.block(answer))

    def test_cutting_off_the_cut_reddens_this(self):
        """Мутация: выключить срезание. Проверка обязана покраснеть."""
        with mock.patch.object(marks, "strip", lambda text, name=None: text):
            self.assertIn(marks.XMD1_BEGIN, marks.strip(block_of(UNIT)))

    def test_unfinished_block_is_cut_too(self):
        """Оборванный хвост человеку не показываем: маркер закрыт не был."""
        self.assertEqual(marks.strip("ответ\n%s\n{\"a\":" % marks.XMD1_BEGIN), "ответ")

    def test_stream_holds_back_a_marker_split_between_chunks(self):
        """Маркер приходит по частям: придерживаем хвост, а не показываем."""
        answer = block_of(UNIT)
        tail, shown = marks.Tail(), []
        for char in answer:
            shown.append(tail.feed(char))
        shown.append(tail.close())
        got = "".join(shown)
        self.assertNotIn("<<<", got)
        self.assertNotIn("MEMORY-FACTS", got)
        self.assertEqual(got.strip(), "Готово, поправил три файла.")

    def test_stream_never_lets_half_a_marker_out(self):
        """Придержан должен быть сам хвост, а не вывод целиком.

        Проверка посимвольным скармливанием этого не ловит: копить всё и
        отдать в конце — тоже «ничего лишнего не показали». Смотрим на то,
        что ушло наружу до прихода второй половины маркера.
        """
        tail = marks.Tail()
        head = marks.XMD1_BEGIN[:6]           # маркер разорван между кусками
        first = tail.feed("Готово, поправил файл.\n\n" + head)
        self.assertNotIn("<", first, "половина маркера уехала человеку")
        self.assertTrue(first and "Готово" in first, "поток встал совсем")
        rest = tail.feed(marks.XMD1_BEGIN[6:] + "\n" + json.dumps(UNIT)
                         + "\n" + marks.XMD1_END)
        self.assertEqual((first + rest + tail.close()).strip(),
                         "Готово, поправил файл.")

    def test_stream_shows_an_answer_without_a_block_whole(self):
        tail = marks.Tail()
        got = "".join(tail.feed(part) for part in ("Готово, ", "поправил ", "файл."))
        self.assertEqual(got + tail.close(), "Готово, поправил файл.")


class TestDisplayHookCutsTheBlockOnScreen(unittest.TestCase):
    """Точка печати. Проверяем хук целиком, от json на входе до json на выходе.

    Проверять один питон мало: решение «звать или не звать» принимает bash, и
    закрытые ворота выглядят снаружи ровно как исправная работа.
    """

    def run_hook(self, home, delta, message_id="m1", index=1, final=False, **env):
        payload = json.dumps({"turn_id": "t1", "message_id": message_id,
                              "index": index, "final": final, "delta": delta})
        full = dict(os.environ, HOME=str(home), XMEM_LIVE="1", PYTHONPATH=str(HERE))
        full.update(env)
        got = subprocess.run(["bash", str(HOOKS / "on_message_display.sh")],
                             input=payload, env=full, capture_output=True,
                             text=True, timeout=60, cwd=str(HERE))
        self.assertEqual(got.returncode, 0, got.stderr[-300:])
        return got.stdout.strip()

    def shown(self, out):
        """Что хук велел напечатать. Пустой вывод значит «печатай как было»."""
        if not out:
            return None
        return json.loads(out)["hookSpecificOutput"]["displayContent"]

    def test_block_never_reaches_the_screen_and_plain_text_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.shown(self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN))
            inside = self.shown(self.run_hook(tmp, json.dumps(UNIT) + "\n", index=2))
            last = self.shown(self.run_hook(tmp, "%s\n" % marks.XMD1_END,
                                            index=3, final=True))
            after = self.shown(self.run_hook(tmp, "обычный текст\n",
                                             message_id="m2", index=0))
        self.assertEqual(first, "Готово.\n")
        self.assertEqual(inside, "")
        self.assertEqual(last, "")
        # Дельта без маркеров решения не требует: молчание печатает исходное.
        self.assertIsNone(after, "хук вмешался в обычный текст")

    def test_the_switch_turns_cutting_off(self):
        """Рубильник: блок показывается целиком, разбор при этом не меняется."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN,
                                XMEM_HIDE_MARKS="0")
            self.assertEqual(out, "", "рубильник не выключил срезание")
            state = Path(tmp) / ".local/state/memory-encoder/hide-marks"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("0\n")
            out = self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN)
            self.assertEqual(out, "", "файл-рубильник не выключил срезание")

    def test_the_switch_is_read_before_python_is_spawned(self):
        """Выключенное срезание не должно стоить запуска интерпретатора.

        Проверка поведения этого не ловит: питон читает тот же рубильник и
        молчит сам, поэтому «ничего не напечатано» выходит и тогда, когда
        bash-проверку убрали вовсе. Смотрим не на вывод, а на то, звали ли
        питон: точка печати срабатывает по разу на порцию строк ответа.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub, spy = Path(tmp) / "bin", Path(tmp) / "python-was-called"
            stub.mkdir()
            (stub / "python3").write_text("#!/bin/sh\ntouch %s\nexit 0\n" % spy)
            (stub / "python3").chmod(0o755)
            self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN,
                          XMEM_HIDE_MARKS="0",
                          PATH="%s:%s" % (stub, os.environ["PATH"]))
            self.assertFalse(spy.exists(), "рубильник выключен, а питон всё равно звали")

    def test_python_half_reads_the_switch_on_its_own(self):
        """Второй слой рубильника: питон зовут и напрямую, не только из bash.

        В хуке bash отсекает раньше, поэтому проверка через него про питонью
        половину не говорит ничего. Обвес, который берёт `pipeline.display`
        сам, обязан слушаться того же рубильника.
        """
        payload = {"turn_id": "t", "message_id": "m9", "index": 1, "final": False,
                   "delta": "Готово.\n%s\n" % marks.XMD1_BEGIN}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XMEM_STATE_DIR": tmp}), \
                 mock.patch.object(display, "STATE", Path(tmp) / "display"):
                with mock.patch.dict(os.environ, {"XMEM_HIDE_MARKS": "0"}):
                    self.assertIsNone(display.answer(payload))
                with mock.patch.dict(os.environ, {"XMEM_HIDE_MARKS": "1"}):
                    got = display.answer(payload)
                self.assertEqual(got["hookSpecificOutput"]["displayContent"], "Готово.\n")

    def test_closed_gate_leaves_the_screen_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN,
                                XMEM_LIVE="0")
        self.assertEqual(out, "", "хук работает при закрытых воротах")

    def test_a_broken_python_does_not_break_the_screen(self):
        """Питон падает — на экран уходит исходная дельта, а не мусор."""
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bin"
            stub.mkdir()
            (stub / "python3").write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
            (stub / "python3").chmod(0o755)
            out = self.run_hook(tmp, "Готово.\n%s\n" % marks.XMD1_BEGIN,
                                PATH="%s:%s" % (stub, os.environ["PATH"]))
        self.assertEqual(out, "", "сломанный питон заговорил в вывод")

    def test_state_of_an_open_block_lives_between_calls(self):
        """Хук на каждую дельту запускается заново; «внутри блока» — на диске."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_hook(tmp, "%s\n" % marks.XMD1_BEGIN)
            left = list((Path(tmp) / ".local/state/memory-encoder/display").iterdir())
            self.assertEqual(len(left), 1, "отметка об открытом блоке не легла")
            self.run_hook(tmp, "%s\n" % marks.XMD1_END, index=2, final=True)
            left = list((Path(tmp) / ".local/state/memory-encoder/display").iterdir())
            self.assertEqual(left, [], "отметка осталась после конца блока")


class TestDroppedUnitsAreCounted(unittest.TestCase):
    """Доля отброшенного и причина уезжают в телеметрию, а не в никуда."""

    def test_reasons_reach_the_trace_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "trace.jsonl"
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from domain import marks\n"
                    "marks.to_facts([%r, %r])\n"
                    "from infra import telemetry; telemetry.close()\n"
                    % (str(HERE), dict(UNIT, source="inferred"), UNIT))
            env = dict(os.environ, MEM_TRACE="1", MEM_TRACE_LOG=str(log))
            out = subprocess.run([sys.executable, "-c", code], env=env,
                                 capture_output=True, text=True, cwd=str(HERE))
            self.assertEqual(out.returncode, 0, out.stderr[-400:])
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        mapped = [r for r in rows if r["function_name"] == "marks_map"]
        self.assertEqual(len(mapped), 1, "шаг маппера в журнале не отметился")
        meta = mapped[0]["metadata"]
        self.assertEqual((meta["in"], meta["out"], meta["dropped"]), (2, 1, 1))
        self.assertEqual(meta["drop_source"], 1)


if __name__ == "__main__":
    unittest.main()
