#!/usr/bin/env python3
"""Разметка идёт на языке человека и по строке на каждое его утверждение.

Запуск: python3 -m unittest tests.test_mark_language -v

Живой прогон 2026-09-02 дал ноль из трёх пар, и оба обрыва сидели в просьбе, а
не в коде:

1. **Язык.** На русское «на завтрак у нас дома едят только овсянку» модель
   ответила разметкой `subject: household breakfast`. Факт в базе лежит, а
   вопрос второй сессии русский, и словесный поиск проходит мимо: кандидатов
   ноль. Контур языка не знает и знать не должен — он обязан донести до базы
   ровно те буквы, какие написала модель. Выбирает язык просьба.
2. **Полнота.** Из «живу в Казани, работаю смотрителем в музее» записалось
   одно утверждение из двух: Казань потерялась целиком. Просьба разрешала «не
   больше трёх строк» и ни разу не говорила, что утверждений в одном сообщении
   бывает несколько.

Отсюда две группы проверок, и судят они разное.

Первая держит **саму просьбу** — она и есть то, что мы правили. Судить её
можно только текстом, поэтому проверки прозаические: вернёшь прежнюю строку —
краснеют. Прозы ровно столько, сколько правил: язык, «каждое утверждение» и
объявленный потолок строк.

Вторая держит **обещание контура под этой просьбой**: сколько утверждений
модель разметила, столько строк и лежит в базе, и буквы у них те же, какими их
написали. Это уже свойство, а не пример: краёв тут много — кириллица, латиница,
заглавные, вперемешку, — и перечисляя их руками, перечислишь не все. Потолок,
объявленный просьбой, проверяется целиком: если контур роняет десятую строку,
просьба обещает то, чего мы не выполняем.
"""
import contextlib, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from domain import marks
from pipeline import understand
from storage import local, port

CWD = "/home/person/dev/demo"
BRANCH = "mark-language"

# Перебор ходит по диску и разбирает архив: срок примера меряет скорость диска,
# а не наш код.
SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

# Слова трёх алфавитов и в разном регистре. Кириллица здесь не украшение: ровно
# на ней контур и обрывался, а латиница осталась бы зелёной при любой поломке.
WORDS = st.sampled_from([
    "Казань", "казань", "КАЗАНЬ", "музей", "смотритель", "овсянка", "Овсянка",
    "забор", "дача", "breakfast", "Oatmeal", "Kazan", "музей-заповедник",
    "город Казань", "ужин на даче",
])

TYPES = st.sampled_from(["preference", "user", "goal", "event", "resource"])


def unit(kind, subject, predicate, value):
    """Единица разметки в том виде, в каком её пишет модель."""
    return {"type": kind, "subject": subject, "predicate": predicate,
            "value": value, "time": "", "source": "stated", "confidence": 0.9}


# Набор утверждений одного сообщения. Темы разные: ключ факта это
# `fact_type|subject|scope`, и два утверждения об одной теме — одна строка, а не
# две. Считать их порознь значило бы требовать от схемы того, чего она не
# обещала.
UNITS = st.lists(st.tuples(TYPES, WORDS, WORDS), min_size=1, max_size=10,
                 unique_by=lambda item: item[1])


def block_of(units):
    """Ответ модели с блоком разметки в конце — как он ляжет в транскрипт."""
    lines = [marks.XMD1_BEGIN]
    lines += [json.dumps(u, ensure_ascii=False) for u in units]
    lines.append(marks.XMD1_END)
    return "Записал.\n\n" + "\n".join(lines)


def transcript(root, reply, say="Запомни это про меня."):
    """Архив из одного разговора: ход человека и ответ модели с разметкой."""
    path = Path(root) / "разговор.jsonl"
    stamp = "2026-09-03T10:00:00Z"
    rows = [
        {"type": "user", "sessionId": "с-1", "timestamp": stamp, "cwd": CWD,
         "gitBranch": BRANCH, "message": {"content": say}},
        {"type": "assistant", "sessionId": "с-1", "timestamp": stamp, "cwd": CWD,
         "gitBranch": BRANCH,
         "message": {"content": [{"type": "text", "text": reply}]}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return [path]


@contextlib.contextmanager
def store(tmp):
    """Своя база на прогон. Адаптер держит репозиторий на процесс — закрываем."""
    base = Path(tmp) / "memory.db"
    local.close()
    with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": "",
                                      "XMEM_LOCAL_PATH": str(base)}), \
         mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
        try:
            yield base
        finally:
            local.close()


def facts_in(base):
    """Строки таблицы фактов: тема -> содержание."""
    conn = sqlite3.connect(str(base))
    try:
        return {subject: content for subject, content
                in conn.execute('SELECT subject, content FROM "fact"')}
    finally:
        conn.close()


class TestTheAskNamesTheLanguage(unittest.TestCase):
    """Просьба велит писать разметку на языке человека, а не на своём.

    Прозаическая проверка, и другой тут быть не может: правили мы текст
    просьбы, и держать надо его. Прежняя строка про язык не говорила ничего —
    вернёшь её, и проверка краснеет.
    """

    def test_every_scheme_asks_for_the_language_of_the_message(self):
        for name in marks.NAMES:
            said = marks.ask(name).lower()
            self.assertIn("язык", said,
                          "схема %s про язык разметки не просит ничего" % name)

    def test_the_language_rule_covers_the_fields_that_the_search_reads(self):
        """Язык требуется у тех полей, по которым потом ищут.

        Поиск идёт словами вопроса по теме и содержанию, а содержание склеено
        из `predicate` и `value`. Оставь просьба язык на усмотрение модели хоть
        в одном из трёх полей — половина строки уедет на английский, и поиск
        снова пройдёт мимо.
        """
        rule = " ".join(one for one in marks.ask().split(". ")
                        if "язык" in one.lower())
        self.assertTrue(rule, "про язык в просьбе не сказано ничего")
        for field in ("subject", "predicate", "value"):
            self.assertIn(field, rule,
                          "правило языка не называет поле %s" % field)


class TestTheAskWantsEveryStatement(unittest.TestCase):
    """Просьба требует строку на каждое утверждение, а не одну на сообщение."""

    def test_the_ask_names_every_statement_of_the_message(self):
        self.assertIn("каждое утверждение", marks.ask().lower(),
                      "просьба не говорит, что утверждений в сообщении несколько")

    def test_the_old_cap_of_three_lines_is_gone(self):
        self.assertNotIn("не больше трёх строк", marks.ask(),
                         "потолок в три строки на месте: два утверждения из "
                         "трёх упрутся в него молча")

    def test_the_declared_cap_is_the_one_the_scheme_holds(self):
        """Потолок объявлен числом, и число это в просьбе то же самое.

        Держать его прозой нельзя: проверка ниже гоняет через контур ровно
        столько строк, сколько просьба разрешила, и разойдись эти два числа —
        она мерила бы не тот потолок.
        """
        self.assertGreater(marks.XMD1_MAX_UNITS, 3)
        self.assertIn(str(marks.XMD1_MAX_UNITS), marks.ask())


class TestWhatTheModelWroteReachesTheBase(unittest.TestCase):
    """Разметка доезжает до таблицы фактов дословно и целиком.

    Тот самый шаг, на котором прогон терял Казань: блок стоял, а в базе лежало
    одно утверждение из двух.
    """

    @SLOW
    @given(units=UNITS)
    def test_every_marked_statement_becomes_its_own_row(self, units):
        """Сколько утверждений разметила модель, столько строк и в базе."""
        raw = [unit(kind, subject, "говорит про", value)
               for kind, subject, value in units]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = transcript(tmp, block_of(raw))
            understand.digest(files, door=port.door(), dry=False)
            got = facts_in(base)
        self.assertEqual(len(got), len(raw),
                         "разметили %d утверждений, в базе %d строк"
                         % (len(raw), len(got)))

    @SLOW
    @given(units=UNITS)
    def test_the_letters_in_the_base_are_the_letters_the_model_wrote(self, units):
        """Тема и содержание лежат дословно: ни перевода, ни смены регистра.

        Слово набора ищут вхождением, и любая правка букв по дороге — это
        промах поиска, который снаружи выглядит как «факта нет».
        """
        raw = [unit(kind, subject, "говорит про", value)
               for kind, subject, value in units]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = transcript(tmp, block_of(raw))
            understand.digest(files, door=port.door(), dry=False)
            got = facts_in(base)
        for one in raw:
            self.assertIn(one["subject"], got,
                          "темы %r в базе нет вовсе" % one["subject"])
            self.assertIn(one["value"], got[one["subject"]],
                          "значение %r до базы не доехало" % one["value"])

    @SLOW
    @given(data=st.data())
    def test_the_cap_the_ask_declares_goes_through_whole(self, data):
        """Столько строк, сколько разрешила просьба, доезжают все до одной.

        Просьба обещает модели потолок; контур обязан этот потолок выдержать.
        Урежь его где-нибудь по дороге — обещание станет ложью, а потеря будет
        видна только на длинном сообщении.
        """
        subjects = data.draw(st.lists(WORDS, min_size=marks.XMD1_MAX_UNITS,
                                      max_size=marks.XMD1_MAX_UNITS, unique=True))
        raw = [unit("preference", subject, "говорит про", "значение %d" % number)
               for number, subject in enumerate(subjects)]
        with tempfile.TemporaryDirectory() as tmp, store(tmp) as base:
            files = transcript(tmp, block_of(raw))
            understand.digest(files, door=port.door(), dry=False)
            got = facts_in(base)
        self.assertEqual(len(got), marks.XMD1_MAX_UNITS,
                         "просьба разрешает %d строк, до базы доехало %d"
                         % (marks.XMD1_MAX_UNITS, len(got)))


if __name__ == "__main__":
    unittest.main()
