#!/usr/bin/env python3
"""Батарея по умолчанию не поднимает живой стенд.

Запуск: python3 -m unittest tests.test_suite_shape -v

Полный прогон занимал больше десяти минут, и почти всё это время держали
проверки, гонявшие настоящий стенд: `live.run` поднимает песочницу, играет
ходы, ждёт фоновую половину каждого хода и убирает за собой. Одна такая
проверка стоит десятки секунд, их было два десятка. Исполнитель, которому
нужно быстро узнать «сломал я что-нибудь или нет», ждал десять минут и на
этом заканчивал день.

Развели так: живой стенд помечается `slow` и по умолчанию не собирается;
включается ключом `XMEM_SLOW=1`. Само по себе это соглашение держится ровно до
первой забытой пометки — новая проверка со стендом молча вернёт десять минут
всем. Поэтому здесь проверяется не соглашение, а его следствия:

  ворота   решение «собирать ли медленные» — чистая функция от окружения:
           читается в момент вызова, не зависит от чужих ключей, не меняется
           от регистра и пробелов;
  форма    ни одна проверка не зовёт `live.run`/`live.main` без пометки, и
           самих помеченных не больше трёх;
  чистота  выброшенные модули лаборатории не поминаются ни одним модулем.

Проверки формы идут по разбору, а не по тексту: docstring, называющий
`live.run`, — не вызов `live.run`, и запрет по подстроке краснел бы на нём.

Мутации, на которых проверки обязаны краснеть:
  * снять @pytest.mark.slow с любой проверки стенда  → TestEveryStandTestIsMarked
  * добавить четвёртую медленную проверку            → TestTheStandIsHeldToThreeTests
  * прочитать ключ один раз на импорте               → TestTheGateReadsTheEnvironment
  * вернуть eval/matrix.py или eval/holdout.py       → TestTheLabLeftovers
"""
import ast
import os
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from tests import slow

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# Больше трёх медленных проверок держать нельзя: четыре минуты на них уже
# уходит, и каждая следующая уводит их в тот же разряд, из которого выводили
# полный прогон.
LIMIT = 3

# Живой стенд поднимают только эти вызовы. Всё остальное в `eval.live` —
# разбор, разметка и подсчёт — работает без песочницы и в пометке не нуждается.
STAND = ("run", "main")


def modules():
    return sorted(p for p in HERE.glob("test_*.py"))


def tree_of(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def marked_slow(node):
    """Пометка `slow` на узле: `@pytest.mark.slow` или `@slow` в любом виде."""
    for one in getattr(node, "decorator_list", []):
        while isinstance(one, ast.Call):
            one = one.func
        if isinstance(one, ast.Attribute) and one.attr == "slow":
            return True
        if isinstance(one, ast.Name) and one.id == "slow":
            return True
    return False


def module_marked(tree):
    """`pytestmark = pytest.mark.slow` на весь модуль."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "pytestmark" not in names:
            continue
        for one in ast.walk(node.value):
            if isinstance(one, ast.Attribute) and one.attr == "slow":
                return True
    return False


def calls_the_stand(node):
    """Зовёт ли тело `live.run` или `live.main`. По разбору, не по тексту."""
    for one in ast.walk(node):
        if not isinstance(one, ast.Call):
            continue
        what = one.func
        if (isinstance(what, ast.Attribute) and what.attr in STAND
                and isinstance(what.value, ast.Name) and what.value.id == "live"):
            return True
    return False


def probes_of(path):
    """Проверки модуля: (имя, узел, помечена ли она или её класс)."""
    tree = tree_of(path)
    whole = module_marked(tree)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            out.append((node.name, node, whole or marked_slow(node)))
        if isinstance(node, ast.ClassDef):
            up = whole or marked_slow(node)
            for inner in node.body:
                if (isinstance(inner, ast.FunctionDef)
                        and inner.name.startswith("test")):
                    out.append(("%s.%s" % (node.name, inner.name), inner,
                                up or marked_slow(inner)))
    return out


def all_tests():
    for path in modules():
        for name, node, is_slow in probes_of(path):
            yield path.name, name, node, is_slow


# Нулевой байт в значение переменной окружения положить нельзя — os
# отказывает раньше нашего кода, и проверять это не про что.
VALUES = st.text(max_size=12).filter(lambda s: "\x00" not in s)
OTHER = st.dictionaries(
    st.text(alphabet="ABCDEFGH_", min_size=1, max_size=6).filter(
        lambda k: k != slow.KEY),
    st.text(max_size=8), max_size=4)


class TestTheGateReadsTheEnvironment(unittest.TestCase):
    """Решение о медленных — чистая функция от одного ключа окружения."""

    @given(other=OTHER)
    @settings(deadline=None, max_examples=40)
    def test_without_the_key_the_stand_stays_down(self, other):
        """Ключа нет — медленных нет. Это состояние всякой чужой машины."""
        self.assertFalse(slow.wanted(other))

    @given(other=OTHER, value=st.sampled_from(sorted(slow.OFF)))
    @settings(deadline=None, max_examples=40)
    def test_an_off_word_keeps_the_stand_down(self, other, value):
        self.assertFalse(slow.wanted(dict(other, **{slow.KEY: value})))

    @given(other=OTHER, value=VALUES)
    @settings(deadline=None, max_examples=100)
    def test_anything_that_is_not_an_off_word_raises_the_stand(self, other, value):
        """Любое непустое значение вне списка выключения включает стенд.

        Свойство, а не пример: человек пишет `1`, `true`, `да` и `on`, и
        угадывать этот список нельзя — включает всё, кроме явного выключения.
        """
        expect = value.strip().lower() not in slow.OFF
        self.assertEqual(expect, slow.wanted(dict(other, **{slow.KEY: value})))

    @given(value=VALUES, pad=st.text(alphabet=" \t", max_size=3))
    @settings(deadline=None, max_examples=100)
    def test_case_and_padding_do_not_change_the_verdict(self, value, pad):
        """`1`, ` 1 ` и `TRUE` против `true` — одно и то же решение.

        Ключ ставят руками и в чужих обёртках, где значение приезжает с
        пробелом. Разное решение на одинаковом по смыслу значении — это
        медленные, включённые наполовину.
        """
        plain = slow.wanted({slow.KEY: value})
        for twist in (pad + value + pad, value.upper(), value.lower(),
                      pad + value.upper() + pad):
            self.assertEqual(plain, slow.wanted({slow.KEY: twist}),
                             "решение поехало от вида значения: %r" % twist)

    @given(first=VALUES, second=VALUES)
    @settings(deadline=None, max_examples=60)
    def test_the_key_is_read_at_call_time(self, first, second):
        """Ключ читается на вызове, а не запоминается на импорте.

        Запомненный на импорте ключ — это медленные, которые нельзя включить
        из-под запущенного процесса, и проверка на нём зеленеет всегда.
        """
        was = os.environ.get(slow.KEY)
        try:
            os.environ[slow.KEY] = first
            one = slow.wanted()
            os.environ[slow.KEY] = second
            two = slow.wanted()
        finally:
            os.environ.pop(slow.KEY, None) if was is None \
                else os.environ.__setitem__(slow.KEY, was)
        self.assertEqual(slow.wanted({slow.KEY: first}), one)
        self.assertEqual(slow.wanted({slow.KEY: second}), two)


class TestEveryStandTestIsMarked(unittest.TestCase):
    """Зовёшь стенд — носи пометку. Иначе быстрый прогон снова станет долгим."""

    def test_no_unmarked_test_raises_the_stand(self):
        loose = ["%s::%s" % (where, name)
                 for where, name, node, is_slow in all_tests()
                 if calls_the_stand(node) and not is_slow]
        self.assertEqual([], loose,
                         "проверки поднимают стенд без пометки slow: %s" % loose)

    def test_the_marked_ones_do_raise_it(self):
        """Обратная проверка: пометка не вешается на то, что стенд не зовёт.

        Без неё запрет выше проходит и на батарее, где помечено всё подряд, —
        а помеченное не собирается, и не проверяется ничего.
        """
        idle = ["%s::%s" % (where, name)
                for where, name, node, is_slow in all_tests()
                if is_slow and not calls_the_stand(node)]
        self.assertEqual([], idle,
                         "пометка slow стоит там, где стенда нет: %s" % idle)


class TestTheStandIsHeldToThreeTests(unittest.TestCase):
    """Медленных не больше трёх, и живут они в одном месте."""

    def test_no_more_than_the_limit(self):
        marked = ["%s::%s" % (where, name)
                  for where, name, _node, is_slow in all_tests() if is_slow]
        self.assertLessEqual(len(marked), LIMIT,
                             "медленных %d, а держим не больше %d: %s"
                             % (len(marked), LIMIT, marked))

    def test_at_least_one_stand_test_is_left(self):
        """Ноль медленных — это не быстрая батарея, а невыполняемый прогон."""
        marked = [name for _where, name, _node, is_slow in all_tests() if is_slow]
        self.assertTrue(marked, "живого прогона не проверяет ничто")


class TestTheLabLeftovers(unittest.TestCase):
    """Выброшенное из лаборатории не поминается ни одним модулем."""

    GONE = ("matrix", "holdout", "swecontextbench")

    def sources(self):
        """Все модули репозитория. Ровно тот же обход, что у грепа в задаче.

        Кроме себя самого: список выброшенного здесь и написан, и упоминание
        в нём — не возврат модуля.
        """
        mine = Path(__file__).resolve()
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT)
            if (rel.parts and rel.parts[0] == ".git") or path.resolve() == mine:
                continue
            yield rel, path.read_text(encoding="utf-8")

    @given(word=st.sampled_from(GONE))
    @settings(deadline=None, max_examples=len(GONE))
    def test_no_module_names_what_was_thrown_out(self, word):
        named = ["%s" % rel for rel, body in self.sources() if word in body]
        self.assertEqual([], named,
                         "%s помянут после выброса: %s" % (word, named))

    def test_the_files_are_gone(self):
        for name in ("eval/matrix.py", "eval/holdout.py",
                     "swecontextbench-cases.json"):
            self.assertFalse((ROOT / name).exists(), "%s остался" % name)


if __name__ == "__main__":
    unittest.main()
