#!/usr/bin/env python3
"""Понимание в живом контуре. Запуск: python3 -m unittest tests.test_understanding -v

До сих пор конец хода доводил разговор только до сырых записей: разговоры,
события. Факты появлялись отдельным ручным запуском, и тот каждый раз
перепахивал весь архив. Отсюда перекос базы: сорок две тысячи событий против
восьми десятков фактов, и все факты положены руками.

Проверяется двое: что понимание вообще зовут в конце хода и что оно идёт по
отметке о прочитанном, а не по всему архиву. Второе без первого бессмысленно
(память не пополняется), первое без второго невыносимо (каждый ход
перечитывает архив целиком).

Свойства проверяются перебором, а не одним примером: у курсора мало правил,
но много краёв — пустой файл, дописанный файл, подменённый файл, потолок
посреди файла. Перечислять их руками значит перечислить не все.
"""
import contextlib, fcntl, json, os, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

os.environ.setdefault("XMEM_INSTANCE_ID", "test-instance")

from infra import locks
from pipeline import drain, save, understand
from storage import db, port

HERE = Path(__file__).resolve().parent.parent
HOOKS = HERE / "hooks"

CWD = "/home/person/dev/demo"

# Перебор ходит по файловой системе и разбирает архив, поэтому срок примера
# снимаем: он меряет не наш код, а скорость диска под нагрузкой.
SLOW = settings(deadline=None, max_examples=25,
                suppress_health_check=[HealthCheck.too_slow])

# Форма архива: список файлов, в каждом столько-то эпизодов.
ARCHIVES = st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=3)


def rows(session, first, count):
    """Строки транскрипта: на эпизод одно сообщение человека и один ответ."""
    out = []
    for number in range(first, first + count):
        stamp = "2026-08-27T%02d:00:00Z" % (number % 24)
        out.append({"type": "user", "sessionId": session, "timestamp": stamp,
                    "cwd": CWD, "gitBranch": "understand-cursor",
                    "message": {"content": "Задача %d. Отвечай кратко" % number}})
        out.append({"type": "assistant", "sessionId": session, "timestamp": stamp,
                    "cwd": CWD, "gitBranch": "understand-cursor",
                    "message": {"content": [{"type": "text",
                                             "text": "Готово %d" % number}]}})
    return out


def append(path, lines):
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def archive(root, sizes):
    """Архив заданной формы. Отдаёт пути файлов в том же порядке."""
    out = []
    for number, size in enumerate(sizes):
        path = Path(root) / ("разговор-%d.jsonl" % number)
        append(path, rows("session-%d" % number, 0, size))
        out.append(path)
    return out


@contextlib.contextmanager
def within(seconds=5):
    """Срок на сам замер: зависание должно падать, а не висеть.

    Замок неблокирующий, и проверять это надо так, чтобы поломка выглядела
    красным тестом. Сделай замок блокирующим — и понимание на занятом замке
    будет ждать очередь вечно: батарея не покраснеет, она просто не кончится.
    Тот же приём и по той же причине в test_write_path.
    """
    def ring(signum, frame):
        raise AssertionError("заход завис на занятом замке дольше %s с" % seconds)
    was = signal.signal(signal.SIGALRM, ring)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, was)


class Collector:
    """Дверь, которая ничего не умеет, кроме как запомнить принятое."""

    def __init__(self):
        self.texts = []

    def write(self, text, wait=False):
        self.texts.append(text)
        return ""


class BreakingDoor(Collector):
    """Дверь, которая один раз спотыкается на записи с заданным номером.

    Так ведёт себя хранилище на деле: сеть отвалилась, запись не прошла
    проверку, квота кончилась. Дальше оно снова принимает.
    """

    def __init__(self, at):
        super().__init__()
        self.at, self.calls = at, 0

    def write(self, text, wait=False):
        self.calls += 1
        if self.calls == self.at:
            raise RuntimeError("хранилище не приняло запись")
        return super().write(text, wait)


class Digest(unittest.TestCase):
    """Общая часть: прогон понимания с отметкой в своём каталоге."""

    def run_understanding(self, tmp, files, **kwargs):
        kwargs.setdefault("dry", False)
        kwargs.setdefault("door", Collector())
        with mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
             mock.patch.dict(os.environ, {"XMEM_BACKEND": "local", "XMEM_DISABLED": ""}):
            return understand.digest(files, **kwargs)


class TestCursorReadsEachEpisodeOnce(Digest):
    """Отметка о прочитанном: первый заход берёт всё, второй — ничего."""

    @SLOW
    @given(sizes=ARCHIVES)
    def test_the_first_run_reads_every_episode(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], sum(sizes),
                             "первый заход прошёл мимо эпизодов")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_second_run_over_the_same_archive_reads_nothing(self, sizes):
        """Главное свойство курсора: неизменный архив разбирается один раз."""
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            again = self.run_understanding(tmp, files)
            self.assertEqual(again["episodes"], 0,
                             "второй заход снова перепахал архив")
            self.assertEqual(again["facts"], 0, "второй заход снова написал факты")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_second_run_does_not_weigh_the_whole_archive(self, sizes):
        """Нечего разбирать — не за что и платить.

        Мера считается по всему архиву намеренно: факт из чужого проекта тоже
        подтверждение. Но заход, которому нечего разбирать, не должен ради
        этого открывать все файлы: конец хода зовёт понимание каждый раз.
        """
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            with mock.patch.object(understand, "weigh") as weighed:
                again = self.run_understanding(tmp, files)
            self.assertEqual(again["episodes"], 0)
            self.assertFalse(weighed.called,
                             "холостой заход всё равно взвесил весь архив")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_dry_run_leaves_the_cursor_where_it_was(self, sizes):
        """Холостой прогон ничего не пишет, значит и отметку не двигает."""
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files, dry=True)
            after = self.run_understanding(tmp, files)
            self.assertEqual(after["episodes"], sum(sizes),
                             "холостой прогон закрыл архив, ничего не записав")


class TestCursorFollowsTheGrowingArchive(Digest):
    """Живой архив дописывается. Дописанное обязано попасть в разбор."""

    @SLOW
    @given(sizes=ARCHIVES, added=st.integers(min_value=1, max_value=3))
    def test_only_the_new_episodes_are_read_again(self, sizes, added):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            append(files[0], rows("session-0", sizes[0], added))
            got = self.run_understanding(tmp, files)
            self.assertGreaterEqual(got["episodes"], added,
                                    "дописанное в архив прошло мимо разбора")
            # Хвостовой эпизод перечитывается намеренно: на конце хода он ещё
            # мог дописываться. Всё, что дальше него, — уже перепашка.
            self.assertLessEqual(got["episodes"], added + 1,
                                 "ради дописанного перечитан весь файл")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_replaced_file_with_a_reused_inode_is_read_from_the_start(self, sizes):
        """Узел файла после удаления переиспользуется — на overlayfs всегда.

        Подмена тогда неотличима от дописывания по stat: узел прежний, размер
        больше. Проверка ставит ровно этот случай руками, потому что на APFS
        его не воспроизвести, а в контейнере он случается сам собой.
        """
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            files[0].unlink()
            append(files[0], rows("session-новая", 0, sizes[0] + 2))
            with mock.patch.object(understand, "STATE",
                                   Path(tmp) / "understand.json"), \
                 mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                state_file = understand.state_path()
            state = json.loads(state_file.read_text(encoding="utf-8"))
            mark = state["files"][str(files[0])]
            mark["inode"] = files[0].stat().st_ino      # узел «переиспользован»
            state_file.write_text(json.dumps(state, ensure_ascii=False),
                                  encoding="utf-8")
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], sizes[0] + 2,
                             "подменённый файл разобран по старой отметке")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_replaced_file_is_read_from_the_start(self, sizes):
        """Файл подменили или обрезали — отметка в нём больше ничего не значит."""
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            files[0].unlink()
            append(files[0], rows("session-новая", 0, 2))
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], 2,
                             "подменённый файл разобран по старой отметке")

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_lost_cursor_means_the_whole_archive_again(self, sizes):
        """Отметка потеряна — разбирать всё заново, а не молчать.

        Это вторая мутация из задачи: сбросить курсор в никуда. Молчащий
        прогон здесь хуже повторной записи — он выглядит как исправная работа.
        """
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            with mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
                 mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                understand.state_path().unlink()
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], sum(sizes),
                             "без отметки понимание разобрало не весь архив")


class TestCeilingKeepsTheTurnShort(Digest):
    """Потолок на заход. Горячий путь не ждёт, но и не должен заклинивать."""

    @SLOW
    @given(sizes=ARCHIVES, ceiling=st.integers(min_value=1, max_value=3))
    def test_a_run_never_takes_more_than_the_ceiling(self, sizes, ceiling):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            got = self.run_understanding(tmp, files, limit=ceiling)
            self.assertLessEqual(got["episodes"], ceiling,
                                 "потолок на заход не соблюдён")

    @SLOW
    @given(sizes=ARCHIVES, ceiling=st.integers(min_value=1, max_value=3))
    def test_repeated_runs_under_a_ceiling_still_finish_the_archive(self, sizes, ceiling):
        """Потолок замедляет разбор, но не отменяет его.

        Заклинившая очередь у сохранения уже стоила потерянного входа: файл,
        не влезающий в потолок, не разбирался никогда.
        """
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            total, runs = 0, 0
            # Заходов с запасом: хвостовой эпизод файла перечитывается, поэтому
            # чистой арифметики «всего делить на потолок» не выходит.
            while runs < 4 * sum(sizes) + 4:
                runs += 1
                got = self.run_understanding(tmp, files, limit=ceiling)
                total += got["episodes"]
                if got["episodes"] == 0:
                    break
            self.assertGreaterEqual(total, sum(sizes),
                                    "под потолком разбор так и не дошёл до конца")
            self.assertEqual(self.run_understanding(tmp, files)["episodes"], 0,
                             "после разбора под потолком архив всё ещё не закрыт")


class TestCursorIsPerStore(Digest):
    """Отметка принадлежит хранилищу, а не архиву.

    То же правило, что у сохранения: ход пишет в локальную базу каждые
    несколько минут, ручной прогон уходит в сеть. Общая книжка учёта означала
    бы, что ход закрывает архив для сети — хук выигрывает эту гонку всегда.
    """

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_local_run_leaves_the_archive_open_for_the_network(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            with mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"):
                with mock.patch.dict(os.environ, {"XMEM_BACKEND": "local"}):
                    here = understand.digest(files, dry=False, door=Collector())
                with mock.patch.dict(os.environ, {"XMEM_BACKEND": "sdk"}):
                    there = understand.digest(files, dry=False, door=Collector())
            self.assertEqual(here["episodes"], sum(sizes))
            self.assertEqual(there["episodes"], sum(sizes),
                             "локальный ход закрыл архив для сетевого прогона")


class TestFactsReachTheStore(Digest):
    """Разбор без записи фактов — это не разбор."""

    @SLOW
    @given(sizes=ARCHIVES)
    def test_every_read_episode_leaves_a_fact_and_an_episode(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            door = Collector()
            got = self.run_understanding(tmp, files, door=door)
            self.assertEqual(got["episodes"], sum(sizes))
            self.assertGreater(got["facts"], 0, "ни одного факта из разбора")
            self.assertEqual(len([t for t in door.texts if t.startswith("Episode")]),
                             got["episodes"], "счётчик эпизодов разошёлся с записью")
            self.assertEqual(len([t for t in door.texts if t.startswith("Fact.")]),
                             got["facts"], "счётчик фактов разошёлся с записью")


class TestABrokenWriteDoesNotStopTheContour(Digest):
    """Сбой записи не должен ни ронять проход, ни заклинивать его навсегда.

    Пока понимание звали руками, упавшую запись видел человек. Теперь оно
    висит на конце хода, и вылетевшее исключение уходит в журнал: снаружи
    контур выглядит исправным, а на деле каждый ход умирает на одном и том же
    эпизоде и до остальных файлов не доходит вовсе.
    """

    @SLOW
    @given(sizes=st.lists(st.integers(min_value=1, max_value=3),
                          min_size=2, max_size=3),
           at=st.integers(min_value=1, max_value=3))
    def test_a_failed_write_does_not_bury_the_rest_of_the_archive(self, sizes, at):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            # спотыкаемся внутри первого файла: на эпизод приходится не меньше
            # одной записи, значит номер в пределах его эпизодов точно наступит
            door = BreakingDoor(min(at, sizes[0]))
            got = self.run_understanding(tmp, files, door=door)
            self.assertGreaterEqual(got["broken"], 1, "сбой записи прошёл незамеченным")
            self.assertGreaterEqual(
                got["episodes"], sum(sizes[1:]),
                "из-за сбоя в первом файле остальные не разобраны вовсе")

    @SLOW
    @given(sizes=st.lists(st.integers(min_value=1, max_value=3),
                          min_size=2, max_size=3))
    def test_a_failed_file_stays_open_and_the_others_close(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files, door=BreakingDoor(1))
            again = self.run_understanding(tmp, files)
            self.assertGreaterEqual(again["episodes"], 1,
                                    "файл со сбоем закрыт отметкой, эпизод потерян")
            self.assertEqual(self.run_understanding(tmp, files)["episodes"], 0,
                             "после доразбора архив всё ещё не закрыт")


class TestAFileWithoutEpisodesIsClosedToo(Digest):
    """Разговор без единого сообщения человека — тоже разобранный разговор."""

    def test_an_empty_file_is_not_opened_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "без-человека.jsonl"
            append(path, [{"type": "assistant", "sessionId": "тихая",
                           "timestamp": "2026-08-27T10:00:00Z", "cwd": CWD,
                           "message": {"content": [{"type": "text", "text": "тишина"}]}}])
            files = [path]
            self.assertEqual(self.run_understanding(tmp, files)["episodes"], 0)
            with mock.patch.object(understand, "episodes_from_file",
                                   side_effect=AssertionError("файл открыт снова")):
                got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], 0)


class TestMemoryOffIsAStoreOfItsOwn(Digest):
    """Выключенная память — тоже путь наружу, и книжка учёта у неё своя.

    `XMEM_DISABLED` отдаёт молчащую дверь: она гасит запись и возвращает
    пустоту. Веди такой прогон общую книжку с локальным — он закрывал бы архив,
    не записав ни строчки. Половина сравнения «без памяти» гоняется именно так.
    """

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_run_with_memory_off_leaves_the_archive_open(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            with mock.patch.object(understand, "STATE", Path(tmp) / "understand.json"), \
                 mock.patch.dict(os.environ, {"XMEM_BACKEND": "local",
                                              "XMEM_DISABLED": "1"}):
                understand.digest(files, dry=False, door=port.door())
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], sum(sizes),
                             "прогон с выключенной памятью закрыл архив вхолостую")


class TestOnlyOnePassWritesAtATime(unittest.TestCase):
    """Очередь и понимание держат один замок, потому что пишут в одну базу.

    Хук конца хода зовёт их подряд, но ходов много: пока понимание ещё пишет,
    следующий ход запускает свою очередь. Замки врозь — и два процесса лезут в
    один SQLite, который открыт без журнала упреждающей записи.
    """

    def test_the_queue_lock_holds_off_understanding(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, [2])
            door = Collector()
            state = Path(tmp) / "understand.json"
            # Замок берём временный, а не боевой. Боевой держит фоновый проход
            # этого же репозитория на каждом ходе: захват LOCK_EX по нему
            # повесил бы батарею наглухо — ровно то, что этот тест обязан
            # ловить красным. И на чистой машине каталога состояния ещё нет.
            held_at = Path(tmp) / "save.lock"
            with mock.patch.object(locks, "PASS", held_at), \
                 held_at.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with mock.patch.object(understand, "TRANSCRIPTS", Path(tmp)), \
                     mock.patch.object(understand, "STATE", state), \
                     mock.patch.object(understand.port, "door", lambda: door), \
                     mock.patch.dict(os.environ, {"XMEM_BACKEND": "local",
                                                  "XMEM_DISABLED": ""}), \
                     mock.patch.object(sys, "argv", ["understand", "--send"]):
                    with within():
                        understand.main()
            self.assertEqual(door.texts, [],
                             "понимание писало в базу, пока её писала очередь")
            self.assertFalse(list(Path(tmp).glob("understand-*.json")),
                             "занятый заход всё равно сдвинул отметку")
            self.assertEqual(len(files), 1)


class TestTheCeilingBoundsTheParseToo(Digest):
    """Потолок обязан ограничивать разбор, а не только запись.

    Разбери мы весь архив в память и запиши сотню эпизодов — заход стоил бы
    как полный проход, а отметки у нетронутых файлов остались бы прежними:
    следующий ход перечитал бы всё заново.
    """

    @SLOW
    @given(sizes=st.lists(st.integers(min_value=1, max_value=2),
                          min_size=3, max_size=4))
    def test_a_ceilinged_run_does_not_parse_the_whole_archive(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            # Считаем разбор ради записи, а не обход ради веса: вес по всему
            # архиву — это мера, и она считается вся, см. digest.
            real, opened = understand.unread, []

            def counting(path, before, counters=None, event_counters=None):
                opened.append(path)
                return real(path, before, counters, event_counters)

            with mock.patch.object(understand, "unread", counting):
                self.run_understanding(tmp, files, limit=1)
            self.assertLess(len(opened), len(files),
                            "под потолком в один эпизод разобран весь архив")


class TestAVanishedTranscriptDoesNotKillThePass(Digest):
    """Архив живой: файл могут удалить или переименовать посреди захода.

    Список файлов снимается один раз, а вес считается по нему же. Пропавший
    между этими мигами файл роняет проход целиком, до единой записи, и в
    журнал уходит traceback — снаружи контур выглядит исправным.
    """

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_missing_file_is_skipped_not_fatal(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            ghost = Path(tmp) / "испарился.jsonl"
            got = self.run_understanding(tmp, files, archive=files + [ghost])
            self.assertEqual(got["episodes"], sum(sizes),
                             "пропавший файл унёс с собой весь проход")


class TestWhatWasWrittenStaysWritten(Digest):
    """Сбой записи не должен заставлять переписывать уже записанное.

    Отметка обязана встать на последнем записанном эпизоде. Откатись она к
    началу файла — каждый заход переписывал бы весь непрочитанный кусок, и на
    неровной сети эпизоды с фактами задваивались бы без конца.
    """

    @SLOW
    @given(sizes=st.lists(st.integers(min_value=2, max_value=4),
                          min_size=1, max_size=2),
           at=st.integers(min_value=2, max_value=5))
    def test_a_failed_pass_does_not_rewrite_what_it_stored(self, sizes, at):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            broken = BreakingDoor(at)
            self.run_understanding(tmp, files, door=broken)
            after = Collector()
            self.run_understanding(tmp, files, door=after)
            written = [t for t in broken.texts + after.texts
                       if t.startswith("Episode")]
            self.assertGreaterEqual(len(written), sum(sizes),
                                    "часть эпизодов потеряна")
            # Один повтор допустим: эпизод, оборванный посреди своих фактов,
            # пишется заново целиком. Всё сверх того — откат отметки.
            self.assertLessEqual(len(written), sum(sizes) + 1,
                                 "отметка откатилась, записанное переписано")


class TestMemoryOffIsAStoreOfItsOwnForSavingToo(unittest.TestCase):
    """Правило про выключенную память одно на оба прохода, а не на один.

    Понимание уже считает `off` отдельным хранилищем. Сохранение читало только
    `XMEM_BACKEND`, а значит половина сравнения «без памяти» закрывала архив
    для настоящего локального прохода — ровно та беда, от которой правило и
    заведено.
    """

    def names(self, module, **env):
        with mock.patch.dict(os.environ, dict({"XMEM_DISABLED": ""}, **env)):
            return module.state_path().name

    def test_saving_keeps_a_separate_book_when_memory_is_off(self):
        for module in (save, understand):
            here = self.names(module, XMEM_BACKEND="local")
            off = self.names(module, XMEM_BACKEND="local", XMEM_DISABLED="1")
            self.assertNotEqual(here, off,
                                "%s: выключенная память ведёт общую книжку "
                                "с локальной" % module.__name__)


def grow(path, session, first, count):
    """Дописать в разговор новые эпизоды целиком."""
    append(path, rows(session, first, count))


def extend_tail(path, session, number):
    """Дорастить последний эпизод, не начиная нового: человек молчит."""
    append(path, [{"type": "assistant", "sessionId": session,
                   "timestamp": "2026-08-27T%02d:30:00Z" % (number % 24), "cwd": CWD,
                   "message": {"content": [{"type": "tool_use", "id": "t%d" % number,
                                            "name": "Bash",
                                            "input": {"command": "echo %d" % number}}]}}])


class TestNothingIsWrittenTwice(Digest):
    """Живой архив дописывается каждый ход. Перезапись — не мелочь, а норма.

    Отметка отступала на эпизод назад при любом росте файла. На конце хода
    файл растёт всегда, значит каждый эпизод и каждый его факт ложились в
    хранилище дважды. Отступать надо, только если хвостовой эпизод правда
    дорос, а не всякий раз, когда в файл что-то дописали.
    """

    @SLOW
    @given(first=st.integers(min_value=1, max_value=3),
           turns=st.lists(st.integers(min_value=1, max_value=2),
                          min_size=1, max_size=3))
    def test_whole_new_episodes_do_not_reopen_the_previous_one(self, first, turns):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, [first])
            door = Collector()
            self.run_understanding(tmp, files, door=door)
            written = first
            for added in turns:
                grow(files[0], "session-0", written, added)
                self.run_understanding(tmp, files, door=door)
                written += added
            episodes = [x for x in door.texts if x.startswith("Episode")]
            self.assertEqual(len(episodes), written,
                             "эпизоды записаны по нескольку раз: %d вместо %d"
                             % (len(episodes), written))

    @SLOW
    @given(sizes=ARCHIVES)
    def test_a_tail_that_really_grew_is_read_again(self, sizes):
        """Обратное свойство: дорос хвост — эпизод обязан перечитаться."""
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            self.run_understanding(tmp, files)
            extend_tail(files[0], "session-0", sizes[0])
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], 1,
                             "доросший хвостовой эпизод не перечитан")


class TestAnInPlaceRewriteIsNoticed(Digest):
    """Файл переписали на месте: стал длиннее, а эпизодов меньше.

    Ни номер узла, ни размер тут не помогают: узел прежний, размер вырос.
    Отметка при этом указывает дальше конца файла — и он молча считается
    дочитанным навсегда.
    """

    def test_a_rewritten_file_is_read_from_the_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, [4])
            self.run_understanding(tmp, files)
            was = files[0].stat().st_size
            long_talk = rows("session-0", 0, 1)
            long_talk[0]["message"]["content"] = "Задача 0. " + "Отвечай кратко " * 400
            with files[0].open("w", encoding="utf-8") as fh:
                for line in long_talk:
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            self.assertGreater(files[0].stat().st_size, was,
                               "стенд не воспроизвёл случай: файл обязан вырасти")
            got = self.run_understanding(tmp, files)
            self.assertEqual(got["episodes"], 1,
                             "переписанный файл молча считается дочитанным")


class TestTheLockIsHeldOnlyForWriting(Digest):
    """Замок общий, значит держать его надо ровно на запись.

    Вес считается по всему архиву — полторы секунды и растёт. Держи мы замок
    всё это время, очередь следующего хода уходила бы ни с чем каждый раз.
    """

    def test_weighing_happens_outside_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, [1])
            lock = Path(tmp) / "save.lock"
            free = []
            real = understand.weigh

            def watching(paths):
                with locks.alone(lock) as mine:
                    free.append(mine)
                return real(paths)

            with mock.patch.object(understand, "weigh", watching):
                self.run_understanding(tmp, files, lock=lock)
            self.assertEqual(free, [True],
                             "замок занят уже на счёте веса, очередь ждёт зря")


class TestBothPassesShareOneLock(unittest.TestCase):
    """Очередь и понимание обязаны спорить за один и тот же файл замка."""

    def test_the_queue_and_understanding_take_the_same_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, [2])
            door = Collector()
            with mock.patch.object(locks, "PASS", Path(tmp) / "save.lock"), \
                 within():
                with drain.alone() as mine:
                    self.assertTrue(mine, "очередь не смогла взять свой замок")
                    with mock.patch.object(understand, "TRANSCRIPTS", Path(tmp)), \
                         mock.patch.object(understand, "STATE",
                                           Path(tmp) / "understand.json"), \
                         mock.patch.object(understand.port, "door", lambda: door), \
                         mock.patch.dict(os.environ, {"XMEM_BACKEND": "local",
                                                      "XMEM_DISABLED": ""}), \
                         mock.patch.object(sys, "argv", ["understand", "--send"]):
                        understand.main()
            self.assertEqual(door.texts, [],
                             "понимание пишет, пока очередь держит замок: "
                             "замки разные, писателей в базу двое")
            self.assertEqual(len(files), 1)


class TestTheCountedFilesAreTheTouchedOnes(Digest):
    """Счётчик в журнале должен называть сделанное, а не намеченное."""

    @SLOW
    @given(sizes=st.lists(st.integers(min_value=1, max_value=2),
                          min_size=3, max_size=4))
    def test_a_ceilinged_run_reports_only_the_files_it_read(self, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            files = archive(tmp, sizes)
            got = self.run_understanding(tmp, files, limit=1)
            self.assertEqual(got["files"], 1,
                             "журнал называет весь архив, а разобран один файл")


class TestTheSourceIsTidy(unittest.TestCase):
    """Мёртвые импорты: мелочь, но она врёт про связи модуля."""

    def test_no_unused_imports(self):
        got = subprocess.run([sys.executable, "-m", "pyflakes",
                              str(HERE / "pipeline" / "understand.py"),
                              str(HERE / "tests" / "test_understanding.py")],
                             capture_output=True, text=True)
        if "No module named" in got.stderr:
            self.skipTest("pyflakes не установлен")
        unused = [l for l in got.stdout.splitlines() if "imported but unused" in l]
        self.assertEqual(unused, [], "\n".join(unused))


def python_that_logs(tmp):
    """Каталог с python3, который записывает, как его позвали, и не работает.

    Служебный вызов `-c` пропускаем настоящему питону: им хук достаёт путь
    транскрипта из полезной нагрузки. Гаси мы и его — хук считал бы, что
    транскрипта нет, и стенд проверял бы не ту ветку.
    """
    binary = Path(tmp) / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    stub = binary / "python3"
    stub.write_text('#!/bin/sh\n'
                    '[ "$1" = "-c" ] && exec "$REAL" "$@"\n'
                    'printf "%s\\n" "$*" >> "$LOG"\nexit 0\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    return binary


class TestEndOfTurnCallsUnderstanding(unittest.TestCase):
    """Конец хода зовёт понимание. Без этого база не пополняется сама."""

    def calls(self, tmp, transcript=True):
        """Чем конец хода позвал питон, по порядку."""
        state = Path(tmp) / ".local" / "state" / "memory-encoder"
        state.mkdir(parents=True, exist_ok=True)
        (state / "live-projects").write_text("%s\n" % tmp, encoding="utf-8")
        log = Path(tmp) / "calls.log"
        env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR=tmp, LOG=str(log),
                   REAL=sys.executable,
                   PATH="%s:%s" % (python_that_logs(tmp), os.environ["PATH"]))
        env.pop("XMEM_LIVE", None)
        payload = {"session_id": "stop-1"}
        if transcript:
            talk = Path(tmp) / ".claude" / "projects" / "demo" / "session.jsonl"
            talk.parent.mkdir(parents=True, exist_ok=True)
            append(talk, rows("stop-1", 0, 1))
            payload["transcript_path"] = str(talk)
        got = subprocess.run(["bash", str(HOOKS / "on_stop.sh")],
                             input=json.dumps(payload), env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(got.returncode, 0, got.stderr[-300:])
        want = "pipeline.understand" if transcript else "pipeline.drain"
        deadline = time.time() + 10
        while time.time() < deadline:
            said = log.read_text(encoding="utf-8") if log.exists() else ""
            if want in said and "pipeline.drain" in said:
                break
            time.sleep(0.1)
        if not transcript:
            time.sleep(0.5)      # дать понимание шанс позваться, если позовётся
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_the_end_of_turn_calls_understanding(self):
        with tempfile.TemporaryDirectory() as tmp:
            said = self.calls(tmp)
            self.assertIn("pipeline.understand", said,
                          "конец хода не зовёт понимание: факты появляются только руками")

    def test_understanding_writes_and_keeps_a_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            line = [row for row in self.calls(tmp).splitlines()
                    if "pipeline.understand" in row]
            self.assertTrue(line, "понимание не позвано вовсе")
            self.assertIn("--send", line[0], "понимание позвано вхолостую")
            self.assertIn("--limit", line[0], "понимание позвано без потолка на заход")

    def test_understanding_comes_after_the_queue(self):
        """Сначала очередь, потом понимание: разбирать надо уже сохранённое."""
        with tempfile.TemporaryDirectory() as tmp:
            said = self.calls(tmp).splitlines()
            where = [i for i, row in enumerate(said) if "pipeline.drain" in row]
            after = [i for i, row in enumerate(said) if "pipeline.understand" in row]
            self.assertTrue(where and after, "позван не весь конец хода: %s" % said)
            self.assertLess(where[0], after[0], "понимание идёт раньше очереди")

    def test_understanding_stays_inside_this_projects_archive(self):
        """Ворота пускают хук только в названные проекты — понимание тоже.

        Само понимание обходит `~/.claude/projects` целиком: пока его звали
        руками, это был осознанный поступок человека. На конце хода это значит,
        что разговоры всех остальных проектов уходят в хранилище сами и на
        каждом ходе, в том числе по сети.
        """
        with tempfile.TemporaryDirectory() as tmp:
            line = [row for row in self.calls(tmp).splitlines()
                    if "pipeline.understand" in row]
            self.assertTrue(line, "понимание не позвано вовсе")
            self.assertIn("--only", line[0],
                          "понимание позвано по всему архиву, мимо ворот")
            self.assertIn("projects/demo/", line[0],
                          "понимание сужено не до архива этого проекта")

    def test_a_sibling_project_is_not_swept_in_by_prefix(self):
        """Каталоги архива — это уплощённые пути, и один вложен в другой.

        `--only` ищет подстроку. Без завершающей черты каталог `-dev` ловит
        `-dev-marginal-gain`, `-dev-job-hunt` и все прочие, а домашний каталог
        в списке живых проектов — вообще весь архив.
        """
        with tempfile.TemporaryDirectory() as tmp:
            line = [row for row in self.calls(tmp).splitlines()
                    if "pipeline.understand" in row][0]
            only = line.split("--only ")[1].strip().split(" ")[0]
            self.assertTrue(only.endswith("/"),
                            "путь без черты на конце ловит соседние проекты: %s" % only)

    def test_without_a_transcript_understanding_is_not_called(self):
        """Не знаем, чей ход, — не разбираем ничей архив."""
        with tempfile.TemporaryDirectory() as tmp:
            said = self.calls(tmp, transcript=False)
            self.assertIn("pipeline.drain", said, "очередь не позвана")
            self.assertNotIn("pipeline.understand", said,
                             "без транскрипта понимание пошло по всему архиву")

    def test_the_gate_still_silences_the_turn(self):
        """Ворота сильнее: в чужом проекте понимание тоже не зовут."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".local" / "state" / "memory-encoder"
            state.mkdir(parents=True, exist_ok=True)
            (state / "live-projects").write_text("%s\n" % HERE, encoding="utf-8")
            log = Path(tmp) / "calls.log"
            env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR="/tmp/чужое",
                       LOG=str(log), REAL=sys.executable,
                       PATH="%s:%s" % (python_that_logs(tmp), os.environ["PATH"]))
            env.pop("XMEM_LIVE", None)
            got = subprocess.run(["bash", str(HOOKS / "on_stop.sh")],
                                 input=json.dumps({"session_id": "stop-2"}), env=env,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(got.returncode, 0)
            time.sleep(0.5)
            self.assertFalse(log.exists(), "в чужом проекте конец хода всё равно работает")


class TestTheTurnFillsTheStoreByItself(unittest.TestCase):
    """Сквозная: разговор в архиве, ход закончился — в базе появились факты.

    Проверка идёт настоящим питоном и настоящей базой, только дом подменён.
    Ради этого она и написана: всё остальное здесь проверяет части, а вопрос
    задачи звучит про целое — пополняется ли память сама.
    """

    def test_a_turn_leaves_facts_in_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / ".claude" / "projects" / "demo"
            project.mkdir(parents=True)
            talk = project / "session.jsonl"
            append(talk, rows("живой-ход", 0, 2))
            state = Path(tmp) / ".local" / "state" / "memory-encoder"
            state.mkdir(parents=True, exist_ok=True)
            (state / "live-projects").write_text("%s\n" % tmp, encoding="utf-8")
            base = Path(tmp) / "memory.db"
            env = dict(os.environ, HOME=tmp, CLAUDE_PROJECT_DIR=tmp,
                       XMEM_BACKEND="local", XMEM_LOCAL_PATH=str(base))
            env.pop("XMEM_LIVE", None)
            got = subprocess.run(["bash", str(HOOKS / "on_stop.sh")],
                                 input=json.dumps({"session_id": "живой-ход",
                                                   "transcript_path": str(talk)}),
                                 env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(got.returncode, 0, got.stderr[-300:])
            facts = self.wait_for(base, "Fact")
            log = state / "save.log"
            self.assertGreater(facts, 0,
                               "ход не оставил ни одного факта; журнал: %s"
                               % (log.read_text(encoding="utf-8")[-500:]
                                  if log.exists() else "пуст"))
            self.assertGreater(self.counts(base)["Event"], 0,
                               "ход не оставил событий: сломан не только разбор")

    def counts(self, base):
        repo = db.Repository(base)
        try:
            return repo.counts()
        finally:
            repo.close()

    def wait_for(self, base, name, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if base.exists():
                try:
                    got = self.counts(base)[name]
                except Exception:
                    got = 0
                if got:
                    return got
            time.sleep(0.3)
        return 0


if __name__ == "__main__":
    unittest.main()
