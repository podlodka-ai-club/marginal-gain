#!/usr/bin/env python3
"""Пары про код должны быть отвечаемы без чтения проекта.

Запуск: python3 -m pytest tests/test_pairs_answerable.py -q

Прогон на двадцати парах показал: у `порт-draco`, `нода-borealis`,
`готовность-borealis` и `воркер-borealis` факт до сессии 2 доезжал, но рука не
отвечала на задачу — переспрашивала. Вторая сессия поднимается в пустой
песочнице без файловых инструментов, а задачи этих четырёх были написаны так,
будто проект под рукой: «напиши скрипт», «опиши сервис для docker-compose»,
«напиши базовый образ и матрицу прогона». Отвечать на такую задачу нечем, кроме
угадывания, — рука честно отказывается.

`нода-borealis` позже снята из набора целиком (прогон без памяти её проходил),
поэтому здесь остаётся тройка переписанных.

Переписанная задача спрашивает значение и решение, а не просит собрать артефакт
по несуществующему репозиторию. Признаки, по которым это проверяется здесь:

1. В тексте задачи этих четырёх нет глагола-генератора артефакта и нет имени
   несуществующего артефакта («напиши», «опиши», «собери конфиг», «базовый
   образ», «матрица прогона», «сервис для docker-compose», «Dockerfile»).
2. Задача сформулирована как вопрос — просит назвать значение или решение,
   а не выдать файл.
3. Остальные шестнадцать пар этой правкой не тронуты: их `task.say` побайтово
   равен снимку, снятому до правки.
4. Переписанная задача по-прежнему не называет ответ и без факта не отвечаема.

Мутации, на которых проверки обязаны краснеть:
  * в задачу вернули «напиши …» / «опиши …»          → TestNoArtifactAsk
  * задачу переписали утверждением, не вопросом       → TestEachAskIsAQuestion
  * заодно поправили текст пятой пары                 → TestOtherTasksUntouched
  * в текст вопроса просочилось ожидаемое слово       → TestTheExpectationStillDecides
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from eval import pairs

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD = ROOT / "eval-pairs-example.json"

# Пары, чьи задачи переписаны под ответ без чтения проекта. Была четвёртой
# `нода-borealis` — снята из набора отдельной задачей, здесь её больше нет.
REWORKED = ("порт-draco", "готовность-borealis", "воркер-borealis")

# Фразы, которыми задача просит собрать артефакт по репозиторию, которого в
# сессии 2 нет. Их присутствие в тексте — признак непереписанной задачи.
ARTIFACT_ASKS = (
    "напиши", "опиши", "собери конфиг", "сгенерир",
    "базовый образ", "матриц", "сервис для docker-compose",
    "шаг деплой-пайплайна", "dockerfile",
)

# Снимок task.say всех прочих пар, снятый до правки (main 4165a71).
# Меняются только пары из REWORKED; у всех остальных текст обязан остаться тем же.
TASK_SAY_BEFORE = {
    "завтрак": "Составь список покупок на неделю: завтраки, перекусы, ужины.",
    "город": "Предложи, где на следующей неделе провести личную встречу с коллегой — время не важно, важно место.",
    "забор": "Посоветуй, что приготовить на ужин из того, что обычно есть дома.",
    "макбук": "Хочу на неделю отдать ноутбук в сервис на ремонт клавиатуры. Есть в этом риск для работы?",
    "овсянка-ужин": "Предложи три рецепта на ужин из обычных продуктов, что чаще всего бывают дома.",
    "макбук-не-в-тему": "Уезжаю в отпуск на две недели и беру рабочий ноутбук с собой на всякий случай. Стоит ли перед поездкой обновить на нём систему, или лучше не трогать перед долгим перерывом?",
    "кровь": "Объясни, как устроен алгоритм быстрой сортировки (quicksort).",
    "питание-веган": "Составь список покупок на неделю: чем закупиться на завтраки, обеды и ужины.",
    "ветка-draco": "Напиши шпаргалку для новичка: как завести ветку под фикс в draco и влить её обратно.",
    "секреты-borealis": "Напиши чек-лист дежурному: borealis в проде не поднимается, в логах отказ авторизации в базе. Что проверить и в каком порядке.",
    "префикс-borealis": "Напиши для документации borealis два примера на curl: создать пользователя и получить его по идентификатору.",
    "линт-atlas": "Напиши шаг CI для atlas, который валит сборку, если код в коммите не отформатирован.",
    "пакеты-atlas": "Напиши Dockerfile для atlas: поставить зависимости, собрать фронтенд, отдать статику.",
    "лок-borealis": "В borealis встала сборка в CI: в контейнере поднялась версия библиотеки новее локальной. Напиши, что починить в репозитории и в шаге установки зависимостей.",
    "тесты-atlas": "Новичок в atlas написал функцию, которая форматирует дату. Напиши ему скелет теста на неё и скажи, чем его запускать.",
    "миграции-borealis": "Опиши по шагам релиз borealis: что происходит между сборкой образа и переключением трафика на новую версию.",
}


class Base(unittest.TestCase):
    def setUp(self):
        _, items = pairs.load(HOUSEHOLD)
        self.items = items
        self.by_id = {item["id"]: item for item in items}


class TestNoArtifactAsk(Base):
    """У переписанных четырёх задача не просит собрать артефакт по репозиторию."""

    def test_none_of_the_four_asks_for_a_generated_file(self):
        for id_ in REWORKED:
            said = (self.by_id[id_]["task"].get("say") or "").lower()
            hit = [phrase for phrase in ARTIFACT_ASKS if phrase in said]
            self.assertEqual([], hit,
                             "%s: задача всё ещё просит артефакт: %s" % (id_, hit))


class TestEachAskIsAQuestion(Base):
    """Переписанная задача спрашивает значение/решение — значит это вопрос."""

    def test_every_reworked_task_is_phrased_as_a_question(self):
        for id_ in REWORKED:
            said = (self.by_id[id_]["task"].get("say") or "").strip()
            self.assertTrue(said.endswith("?"),
                            "%s: задача не вопрос: %r" % (id_, said))


class TestOtherTasksUntouched(Base):
    """Шестнадцать остальных пар этой правкой не тронуты."""

    def test_task_say_of_every_other_pair_is_byte_for_byte_the_same(self):
        for id_, before in TASK_SAY_BEFORE.items():
            self.assertEqual(before, self.by_id[id_]["task"]["say"],
                             "task.say пары %r изменён, а не должен был" % id_)

    def test_the_set_is_exactly_the_reworked_plus_the_snapshot(self):
        got = {item["id"] for item in self.items}
        self.assertEqual(set(REWORKED) | set(TASK_SAY_BEFORE), got,
                         "состав набора разошёлся со снимком: %s" % got)


class TestTheExpectationStillDecides(Base):
    """Переписанная задача не называет ответ и без факта не отвечаема.

    `TestTaskNeverNamesTheExpectation` в `test_pairs_form.py` держит это на всём
    наборе; здесь — точечно на четвёрке, чтобы правка формулировок не подсунула
    ожидаемое слово в текст вопроса.
    """

    def test_no_reworked_task_spells_out_its_expectation(self):
        for id_ in REWORKED:
            item = self.by_id[id_]
            said = (item["task"].get("say") or "").lower()
            for wanted in item.get("expect") or []:
                self.assertNotIn(wanted.lower(), said,
                                 "%s: ожидание %r названо в задаче" % (id_, wanted))

    def test_each_reworked_pair_still_applies_a_fact(self):
        for id_ in REWORKED:
            item = self.by_id[id_]
            self.assertEqual("apply", item["aim"], id_)
            self.assertTrue(item.get("expect"), "%s: нечего применять" % id_)
            self.assertTrue(item.get("tell"), "%s: факт не сообщён" % id_)


if __name__ == "__main__":
    unittest.main()
