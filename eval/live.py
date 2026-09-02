#!/usr/bin/env python3
"""Прогон замера целиком, одной командой: чистая база, живые ходы, чистая сессия.

Флоу был описан и разобран на части, но целиком не выполнялся ни разу.
Проигрывателя сценария в репозитории не было вовсе, а прежняя оценка
(`eval/evaluate.py`) дёргала `suggest.suggest` в своём же процессе. Цифра,
снятая так, говорит про выдачу памяти и ничего не говорит про то, помогла ли
она агенту: агента в замере не было.

Что здесь меряется. Пара сессий: в первой человек что-то сообщает, во второй —
чистой — получает задачу, для решения которой это знание нужно, причём сам
факт в задаче не повторяется. Значит меряется **применение**, а не
припоминание. Форма пары и всё, чем судится исход, приходят набором (см.
`eval/pairs.py`); стенд не знает ни тематики набора, ни нашего архива.
Подставили бытовой набор — работает, подставили набор про код — работает.

Порядок внутри команды:

  1. Песочница. Всё состояние прогона — база, очередь, лента, отметки, замок —
     уводится в свой каталог одной переменной `XMEM_STATE_DIR`. Живая база
     пользователя не открывается ни на запись, ни на чтение, ни при удачном
     исходе, ни при обрыве.
  2. Сессия 1 у каждой пары. Реплики проигрываются ходами агента с нашими
     хуками: те же три точки, что человек занимает по `SETUP.md`, тот же
     `common.sh`, те же фоновые проходы в конце хода. Память наполняется ходом,
     а не подкладыванием записей — заглушите хуки, и база останется пустой
     (`tests/test_full_run.py`).
  3. Сброс. Между этапами прогон дожидается фоновой половины последнего хода:
     очередь разбирается и факты пишутся уже после того, как ход закончился, и
     задача, поставленная раньше, встретила бы пустую базу. База остаётся,
     сессия обнуляется.
  4. Сессия 2 у каждой пары, каждая своя и чистая. Отвечает агент, а не выдача
     памяти: подсказка попадает к нему тем же хуком, что и в работе.
  5. Разбивка. Итог печатается не одним числом, а по исходам: применила,
     ничего не нашла, нашла и срезали, отдала и не применил, приплела не по
     делу. Исходы, под которые место оставлено, но которых мы пока не меряем,
     печатаются прочерком и названы — молча отсутствующий исход неотличим от
     нулевого.

Решения, принятые здесь, и доводы за них:

  **Сессия на реплику, а не одна на весь первый этап.** Эпизод в схеме
  подписан парой «разговор, номер». Свали все реплики в один разговор — и
  эпизоды перенумеруются подряд, а обстановка (место, пометка) у всех станет
  одна: всё, что меряется обстановкой, станет непроходимо по построению.
  Вдобавок общий разговор растит контекст: поздние ходы отвечали бы из него, а
  не из памяти, и замер мерил бы длину контекста, а не память.

  **Задача второй сессии ставится оттуда же, где сказали.** Выдача взвешивается
  уместностью (ADR 0009): запись, снятая в другом месте, порогу не проходит.
  Ставь задачу из нейтрального каталога — и уместность срежет ровно всё, а
  прогон покажет «нашла, срезали» на каждой паре, ничего не измерив.

  **Первый этап идёт без оболочки.** Ходу разрешены правки файлов и чтение,
  Bash — нет. Реплики набора взяты из настоящей работы и просят настоящих
  команд; давать их песочнице нельзя, а притворяться, что дали, — врать.

  **Дорогое отделено от дешёвого.** Проигрывателя два. `claude` — настоящий
  агент, им снимается цифра. `replay` — проигрыватель без модели: он пишет ход
  в архив в том же виде, в каком его пишет харнесс, и отдаёт его тем же хукам.
  Он бесплатный и повторяемый, им отлаживают саму команду. **Цифру им снимать
  нельзя**: во второй сессии за агента отвечает выдача памяти, то есть меряется
  доставка, а не применение. Отчёт всегда называет проигрывателя.

  Потолок трат у настоящего прогона задаётся `--budget` на ход, `--limit`
  укорачивает набор, `--model` выбирает модель подешевле.

Чего этот прогон не обещает. Два запуска подряд дают одно число только на
`replay`: живая модель недетерминирована, и повторяемость здесь — свойство
обвязки (чистая база, порядок, сброс сессии), а не ответа агента.

Разговоры прогона пишет сам харнесс, и пишет он их в архив пользователя
(`~/.claude/projects`). Прогон забирает их оттуда к себе в песочницу в конце и
не оставляет ни одного. Пока прогон идёт, его разговоры в архиве лежат — этого
не обойти, не отобрав у агента учётные данные вместе с каталогом настроек.

Запуск:

    python3 -m eval.live                          нынешний набор, через мост
    python3 -m eval.live --pairs своё.json        свой набор пар
    python3 -m eval.live --limit 5                укороченный, для отладки
    python3 -m eval.live --player replay          без модели и без трат
"""
import argparse, fcntl, hashlib, json, os, re, shutil, signal, subprocess, sys, time, uuid
from collections import Counter, OrderedDict, namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archive.extract import NOT_CODE
from archive.transcripts import TRANSCRIPTS
from domain import ledger, marks
from eval import evaluate, pairs
from storage import db

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"

# Песочницы прогонов. Не `/tmp` и не `~/.local/state` намеренно: оба попадают
# под отсев служебных путей (`extract.NOT_CODE`), и правка файла в таком
# каталоге фактом не станет. Прогон в такой песочнице показал бы честный ноль,
# не назвав причины, — набор непроходим по построению, а выглядит как слабая
# память. Проверка стоит в `Sandbox.check`.
DEFAULT_ROOT = Path.home() / ".local" / "share" / "memory-encoder-eval"

# Живое состояние. Ни один прогон не имеет права его открыть.
LIVE_STATE = Path.home() / ".local" / "state" / "memory-encoder"

# Три точки, те же и в том же порядке, что человек занимает по SETUP.md.
POINTS = {
    "UserPromptSubmit": ["on_prompt_queue.sh", "on_prompt_read.sh"],
    "Stop": ["on_stop.sh"],
    "MessageDisplay": ["on_message_display.sh"],
}

# Исходы. Итог обязан раскладываться по ним целиком: иначе «прошло 24 из 100»
# снова становится единственным числом, а где потерялось — догадкой.
APPLIED = "применила"
# Ответ сошёлся, а память промолчала. Это не её работа: вопрос угадался сам.
# Держим отдельной корзиной, иначе цифра растёт от угадываемости набора, а
# «ничего не нашла» пустеет ровно на эти случаи.
COINCIDED = "прошло без подсказки"
NOT_FOUND = "память ничего не нашла"
CUT = "нашла, срезали"
UNUSED = "отдала, не применил"
INTRUDED = "приплела не по делу"
BROKEN = "ход упал"

# Исходы, под которые место оставлено, но которых мы пока не меряем. Печатаются
# прочерком и с причиной: молча отсутствующий исход неотличим от нулевого, и
# «выдуманных фактов ноль» читалось бы как достижение, а не как непроверенное.
RESERVED = OrderedDict([
    ("переспросил известное",
     "нужен разбор ответа: агент спросил человека о том, что память ему уже сказала"),
    ("применила устаревшее",
     "нужен срок годности в самой паре: без него «устарело» неотличимо от «неверно»"),
    ("сослалась на выдуманное",
     "нужна сверка ответа с выдачей: утверждение, которого в выдаче не было"),
])

OUTCOMES = (APPLIED, COINCIDED, NOT_FOUND, CUT, UNUSED, INTRUDED, BROKEN)
BUCKETS = OUTCOMES + tuple(RESERVED)

# Молчания, за которыми что-то нашлось. Порог и потолок — очевидные; ложная
# находка сюда же: выдача была, её убрал отсев, и это потеря на нашей стороне,
# а не пустая память.
CUT_REASONS = ("below_threshold", "over_budget", "incidental")

Reply = namedtuple("Reply", "text session_id cost error")


class UnsafeRun(RuntimeError):
    """Прогон встал бы туда, где испортил бы чужое или измерил бы не то."""


# --- разбивка ---------------------------------------------------------------

def bucket(row):
    """Исход одной пары. Ровно один на строку, порядок ветвей и есть правило.

    Упавший ход применением не считается и в потери не записывается: это не
    поведение памяти, а поломка прогона, и складывать их — завышать одно за
    счёт другого.

    Запрещённое в ответе разбирается раньше удачи: пара, где нужное сказано, а
    заодно приплетено лишнее, удачей не является. Иначе отрицательные случаи
    ничего не стоили бы.

    Удача делится надвое по тому, говорила ли память. Сошедшийся ответ на
    молчании памяти — совпадение, а не её работа, и цифра, куда его записали,
    меряет угадываемость набора.
    """
    if row.get("error"):
        return BROKEN
    if row.get("intruded"):
        return INTRUDED
    if row.get("ok"):
        # Удача памяти — только когда память говорила. Сошедшийся ответ на
        # молчании это совпадение: на первом живом прогоне агент написал в
        # список покупок овсянку просто потому, что овсянка обычный завтрак.
        return APPLIED if row.get("injected") else COINCIDED
    if row.get("injected"):
        return UNUSED
    if row.get("reason") in CUT_REASONS:
        return CUT
    return NOT_FOUND


def tally(rows):
    """Счёт по исходам. Пустые исходы тоже названы: ноль это тоже ответ."""
    counted = Counter({name: 0 for name in BUCKETS})
    counted.update(bucket(row) for row in rows)
    return counted


# --- песочница --------------------------------------------------------------

PROBE = r"""
import json, sys
sys.path.insert(0, %r)
from pipeline import associate, display, drain, save, suggest, understand
from infra import config, locks, telemetry
from domain import ledger
from storage import db
from eval import evaluate
print(json.dumps({
    "config": str(config.state_dir()), "db": str(db.path()),
    "queue": str(drain.QUEUE), "lock": str(locks.PASS),
    "ledger": str(ledger.LOG), "trace": str(telemetry.LOG),
    "save": str(save.STATE), "understand": str(understand.STATE),
    "associate": str(associate.STATE), "display": str(display.STATE),
    "suggest": str(suggest.LOG), "results": str(evaluate.RESULTS),
}))
""" % str(ROOT)


def paths_in(env):
    """Куда сходят все пути состояния при этом окружении. Спрашиваем модули.

    Своим списком здесь не обойтись: он разъедется с кодом в первый же день,
    когда кто-нибудь заведёт новую книжку учёта. Спрашиваем в отдельном
    процессе, потому что константы модулей считаются при импорте.
    """
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=str(ROOT),
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise UnsafeRun("не удалось спросить модули о путях: %s" % out.stderr[-400:])
    return {name: Path(where) for name, where in json.loads(out.stdout).items()}


class Sandbox:
    """Каталог одного прогона: база, состояние, каталоги ходов, настройки хуков.

    Всё, что прогон пишет, лежит здесь и уходит вместе с ним. Живое состояние
    не открывается: путь к нему проверяется до первого действия, а не после.
    """

    def __init__(self, root=None, live_hooks=True):
        self.root = Path(root or DEFAULT_ROOT / uuid.uuid4().hex[:12]).expanduser()
        self.state = self.root / "state"
        self.db = self.root / "memory.db"
        self.places = self.root / "places"
        self.archive = self.root / "archive"
        self.settings = self.root / "settings.json"
        # Настроек двое. На первом этапе заняты все три точки — память
        # наполняется ходом. На втором конец хода не занят вовсе: задача
        # ставится из того же места, где сказали, и её текст вместе с ответом
        # лёг бы в ту самую базу, которую мы меряем. Пара N подсказывала бы паре
        # N+1, а цифра ползла бы от порядка пар.
        self.asking = self.root / "settings-asking.json"
        self.live_hooks = live_hooks
        self.talks = []              # разговоры, заведённые этим прогоном
        self.groups = set()          # группы процессов, порождённые прогоном
        self.opened = False

    # --- проверки до первого действия ---

    def check(self):
        here = self.root.resolve() if self.root.exists() else self.root
        if (here == LIVE_STATE or LIVE_STATE in here.parents
                or here in LIVE_STATE.parents):
            raise UnsafeRun("песочница %s задевает живое состояние %s — прогон отказан"
                            % (here, LIVE_STATE))
        ground = "%s/" % self.places
        bad = [mark for mark in NOT_CODE if mark in ground]
        if bad:
            raise UnsafeRun(
                "каталог ходов %s попадает под отсев служебных путей (%s): правки "
                "файлов не станут фактами, и прогон показал бы ноль без причины"
                % (self.places, ", ".join(bad)))

    def open(self):
        self.check()
        for where in (self.root, self.state, self.places, self.archive):
            where.mkdir(parents=True, exist_ok=True)
        for where, asking in ((self.settings, False), (self.asking, True)):
            where.write_text(json.dumps(self.wiring(asking=asking),
                                        ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        self.opened = True
        return self

    def wiring(self, asking=False):
        """Настройки агента: те же точки, что занимает человек по SETUP.md.

        На вопросах второго этапа конец хода снят: он бы дописал в базу сам
        вопрос и ответ на него, и следующая пара получила бы подсказку из
        предыдущей. Чтение остаётся — им подсказка и приходит.
        """
        points = {name: hooks for name, hooks in POINTS.items()
                  if not (asking and name == "Stop")}
        return {"hooks": {
            event: [{"hooks": [{"type": "command", "command": str(HOOKS / name)}
                               for name in names]}]
            for event, names in points.items()}}

    def env(self, base=None):
        """Окружение ходов. Всё своё названо явно, чужое не наследуется.

        Ключи, которые прогон обязан назвать сам, даже если они уже есть в
        окружении: `XMEM_QUEUE_PATH`, `XMEM_LEDGER`, `MEM_TRACE_LOG` сильнее
        каталога состояния, и унаследованный от пользователя увёл бы половину
        прогона обратно в живое.
        """
        got = dict(os.environ if base is None else base)
        got.update({
            "PYTHONPATH": str(ROOT),
            "XMEM_STATE_DIR": str(self.state),
            "XMEM_LOCAL_PATH": str(self.db),
            "XMEM_QUEUE_PATH": str(self.state / "queue.jsonl"),
            "XMEM_LEDGER": str(self.state / "ledger.jsonl"),
            "MEM_TRACE_LOG": str(self.state / "trace.jsonl"),
            "XMEM_BACKEND": "local",
            "XMEM_DISABLED": "",
            "XMEM_LIVE": "1" if self.live_hooks else "0",
            # Умолчания прогона названы вслух: рубильник, оставшийся в профиле
            # пользователя, менял бы цифру молча.
            "XMEM_MARKS": "",
            "XMEM_MEMORY": "",
            "XMEM_HIDE_MARKS": "hide",
            # Граница обхода архива. Разговоры прогона пишет харнесс, и пишет
            # он их в архив пользователя: увести их оттуда нечем, не отобрав у
            # агента учётные данные. Проход по связям обходит архив целиком —
            # так задумано, вес карточки это число наблюдений, — и без границы
            # сложил бы в базу замера карточки из чужой переписки. Границей
            # берём уплощённый каталог ходов: он общий у всех мест прогона и
            # ничей больше.
            "XMEM_ONLY": flat(self.places),
            # Цепочка конца хода идёт на глазах прогона, а не в фоне. Ждать её
            # по тишине нельзя: она стартует питон и читает архив, ничего не
            # записывая, и тихое окно наступает раньше первой записи.
            "XMEM_SYNC": "1",
        })
        return got

    # --- дети прогона ---

    def spawn(self, cmd, **kw):
        """Ребёнок прогона — в своей группе процессов, чтобы её можно было погасить.

        Конец хода уходит в фон и переживает и хук, и агента: `nohup … &`
        отвязывает проход от вызывающего, но не от группы. Не заведи мы группу
        свою — на обрыве фоновая цепочка осталась бы дописывать в песочницу,
        которую мы только что удалили, и создала бы её заново. Ровно так
        оборванный прогон и оставлял за собой каталог.
        """
        proc = subprocess.Popen(cmd, start_new_session=True, **kw)
        self.groups.add(proc.pid)
        return proc

    def wiring_for(self, turn):
        """Какими настройками играть ход: с концом хода или без него.

        Ход первого этапа приходит с описанием реплики, вопрос второго — без
        него. Признак тот же, по которому проигрыватель решает, чем отвечать,
        и заводить второй незачем.
        """
        return self.settings if turn is not None else self.asking

    def hush(self):
        """Погасить всё, что прогон породил. Живых сессий за собой не оставляем."""
        for group in sorted(self.groups):
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:
                pass                 # группа уже разошлась — так и надо
        self.groups.clear()

    # --- уборка ---

    def harvest(self, talk):
        """Разговор прогона — к себе в песочницу, из архива пользователя.

        Забираем в конце прогона, а не после каждого хода, и это не лень. Мера
        факта считает повторы по архиву: унеси разговор сразу — и второй ход про
        то же самое не найдёт первого, у факта навсегда останется одно
        вхождение, и он не переживёт порога. Первый прогон так и показал ноль
        при исправном конвейере.
        """
        moved = []
        for path in sorted(TRANSCRIPTS.rglob("%s.jsonl" % talk)):
            target = self.archive / path.parent.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(target))
                moved.append(target)
            except OSError:
                pass
            try:
                path.parent.rmdir()          # каталог опустел — унесём и его
            except OSError:
                pass
        return moved

    def close(self, keep=False):
        """Погасить, забрать разговоры, снести каталог. Именно в этом порядке.

        Снести раньше, чем погасить, значит снести и тут же получить каталог
        обратно: фоновая половина хода заводит его сама при первой же записи.
        Поэтому удаление ещё и повторяется, пока каталога не станет.
        """
        self.hush()
        for talk in self.talks:
            self.harvest(talk)
        if keep or not self.opened:
            return
        for _ in range(10):
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                return
            time.sleep(0.3)

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()
        return False


# --- ожидание фоновой половины хода -----------------------------------------

STEP = 0.25


def busy(lock):
    """Занят ли общий замок прохода. Занят значит «фон ещё пишет»."""
    if not lock.exists():
        return False
    try:
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def shape(where, extra=()):
    """Отпечаток каталога состояния: имя, размер, время правки."""
    out = []
    for path in sorted(list(Path(where).rglob("*")) + [Path(p) for p in extra]):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append((str(path), stat.st_size, int(stat.st_mtime_ns)))
    return out


def settled(state, extra=(), quiet=2.0, timeout=180.0):
    """Дождаться, пока конец хода допишет. Отдаёт (когда, вышел ли срок).

    Конец хода уходит в фон: очередь, понимание, связи, отметки, забывание и
    свёртка идут цепочкой уже после того, как ход закончился. Спроси память
    сразу — спросишь пустую базу, и цифра будет про скорость диска, а не про
    память.

    Ждём по двум признакам разом: общий замок свободен и каталог состояния
    перестал меняться. Одного замка мало — между звеньями цепочки он свободен;
    одного отпечатка мало — питон стартует дольше, чем длится тихая секунда.
    """
    end = time.time() + timeout
    calm, last = 0.0, None
    while time.time() < end:
        now = shape(state, extra)
        if not busy(Path(state) / "save.lock") and last == now:
            calm += STEP
            if calm >= quiet:
                return time.time(), False
        else:
            calm = 0.0
        last = now
        time.sleep(STEP)
    return time.time(), True


# --- каталог, в котором проигрывается ход -----------------------------------

SAFE = re.compile(r"[^\w.-]+", re.UNICODE)
MARK = re.compile(r"^[\w][\w./-]*$", re.UNICODE)


def ground(box, turn):
    """Каталог хода: место из набора, его пометка, названные им файлы.

    Чужого дерева здесь не строится — файлы кладутся плоско, по именам: набор
    спрашивает про имя, а полный путь с чужой машины в песочнице всё равно был
    бы выдумкой.
    """
    plain = (turn.get("place") or "").strip()
    # Хвост из букв и цифр обязателен: харнесс уплощает путь, выбрасывая всё
    # не-латинское, и два кириллических имени одной длины дали бы один каталог
    # архива. Хвост берём от самого имени, чтобы одно место всегда давало один
    # каталог и прогон был повторяем.
    tail = hashlib.sha1(plain.encode("utf-8")).hexdigest()[:8]
    named = "%s-%s" % (SAFE.sub("-", plain) or "здесь", tail)
    where = box.places / named
    fresh = not where.exists()
    where.mkdir(parents=True, exist_ok=True)
    mark = (turn.get("mark") or "").strip()
    if not MARK.match(mark) or mark == "HEAD":
        mark = "main"
    if fresh or not (where / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", mark], cwd=str(where),
                       capture_output=True)
        # Пустой репозиторий ветки ещё не имеет: `rev-parse --abbrev-ref HEAD`
        # на нём падает, и пометка обстановки уходит в архив пустой — у всех
        # ходов сразу, молча. А пометка это взвешенное поле поиска, то есть
        # целое измерение уместности. Один пустой коммит это чинит.
        for cmd in (["git", "config", "user.email", "eval@local"],
                    ["git", "config", "user.name", "eval"],
                    ["git", "commit", "-q", "--allow-empty", "-m", "начало"]):
            subprocess.run(cmd, cwd=str(where), capture_output=True)
    for name in turn.get("touched") or []:
        target = where / Path(name).name
        if not target.exists():
            target.write_text("", encoding="utf-8")
    return where


def key_of(turn):
    """Чем два хода первой сессии считаются одним и тем же.

    Один и тот же ход кормит несколько пар: набор, собранный из архива, ссылает
    на один эпизод по несколько случаев. Сыграй его столько раз, сколько на него
    ссылок, — и повторяемость знания вырастет от сборки набора, а не от работы.

    Говорит об этом сам набор, полем `ref`, а не стенд догадкой по содержанию.
    Догадка по тексту склеила бы и честный повтор: человек, сказавший одно и то
    же дважды, повторил это дважды, и повторяемость у знания настоящая.
    """
    ref = (turn.get("ref") or "").strip()
    return ("ref", ref) if ref else ("one", id(turn))


def script_of(items):
    """Первые сессии всех пар, по одному разу и в порядке появления."""
    out = OrderedDict()
    for pair in items:
        for turn in pair.get("tell") or []:
            out.setdefault(key_of(turn), turn)
    return list(out.values())


def asked_from(box, pair, places):
    """Каталог, из которого ставится задача второй сессии.

    Выдача взвешивается уместностью (`suggest.place`, ADR 0009): запись,
    снятая в другом месте, порогу не проходит. Ставь задачу из нейтрального
    каталога — и уместность срежет ровно всё, а прогон покажет «нашла,
    срезали» на каждой паре, ничего не измерив. Человек так и не спрашивает:
    он сидит там же, где сказал.

    Место спрашиваем в таком порядке: названное самой задачей, потом место
    первой сессии, потом общий каталог. Первое существеннее, чем кажется:
    отрицательная пара своей первой сессии может не иметь вовсе, и без права
    назвать место она задавалась бы из ниоткуда — то есть на молчании, то есть
    ничего не проверяя.
    """
    named = (pair.get("task") or {}).get("place")
    if named:
        return ground(box, {"place": named})
    for turn in pair.get("tell") or []:
        where = places.get(key_of(turn))
        if where:
            return where
    for turn in pair.get("tell") or []:
        if turn.get("place"):
            return ground(box, {"place": turn["place"]})
    return box.places


# --- проигрыватели ----------------------------------------------------------

def hooks_of(settings, event):
    """Команды точки — из тех же настроек, что читает агент.

    Свой список означал бы, что проигрыватель без модели зовёт не то, что зовёт
    настоящий ход, и расхождение вылезло бы цифрой, а не ошибкой.
    """
    body = json.loads(Path(settings).read_text(encoding="utf-8"))
    return [hook["command"]
            for group in (body.get("hooks") or {}).get(event) or []
            for hook in group.get("hooks") or []]


class Replay:
    """Проигрыватель без модели: ход пишется в архив и отдаётся тем же хукам.

    Нужен, чтобы отлаживать саму команду: он бесплатный, быстрый и
    повторяемый. Цифру набора им снимать нельзя — во второй сессии он
    «отвечает» тем, что отдала память, то есть меряет доставку, а не
    применение. Отчёт называет проигрывателя ровно поэтому.
    """

    name = "replay"

    def __init__(self, box):
        self.box = box

    def play(self, prompt, cwd, talk, turn=None, at=None, tools=None):
        env = self.box.env()
        target = self.transcript(cwd, talk)
        when = at or datetime.now(timezone.utc)
        self.write(target, talk, cwd, when, "user",
                   [{"type": "text", "text": prompt}])
        payload = {"session_id": talk, "transcript_path": str(target),
                   "cwd": str(cwd), "prompt": prompt,
                   "permission_mode": "default",
                   "hook_event_name": "UserPromptSubmit"}
        said = ""
        settings = self.box.wiring_for(turn)
        for command in hooks_of(settings, "UserPromptSubmit"):
            got = self.call(command, payload, env, cwd)
            said = said or got
        blocks = []
        for name in (turn or {}).get("touched") or []:
            blocks.append({"type": "tool_use", "name": "Write",
                           "input": {"file_path": str(Path(cwd) / Path(name).name)}})
        blocks.append({"type": "text", "text": self.answer(said, turn)})
        self.write(target, talk, cwd, when + timedelta(seconds=1), "assistant", blocks)
        stop = {"session_id": talk, "transcript_path": str(target),
                "cwd": str(cwd), "hook_event_name": "Stop"}
        for command in hooks_of(settings, "Stop"):
            self.call(command, stop, env, cwd)
        return Reply(text=blocks[-1]["text"], session_id=talk, cost=0.0, error=None)

    def answer(self, said, turn):
        """Без модели ответить нечем.

        На первой сессии ход говорит, чего коснулся; на второй — тем, что ему
        подставила память. Второе и есть причина, по которой этим
        проигрывателем нельзя снимать цифру.
        """
        if turn is not None:
            names = ", ".join(Path(n).name for n in (turn.get("touched") or []))
            return "Готово: %s" % names if names else "Готово."
        return said or "Ничего про это не помню."

    def transcript(self, cwd, talk):
        """Тот же адрес, по которому разговор кладёт харнесс."""
        return TRANSCRIPTS / flat(cwd) / ("%s.jsonl" % talk)

    def write(self, target, talk, cwd, when, kind, blocks):
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {"type": kind, "sessionId": talk, "cwd": str(cwd),
               "gitBranch": mark_of(cwd),
               "timestamp": when.isoformat().replace("+00:00", "Z"),
               "message": {"role": kind, "content": blocks}}
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def call(self, command, payload, env, cwd):
        proc = self.box.spawn(["bash", command], env=env, cwd=str(cwd),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        said, _ = proc.communicate(json.dumps(payload))
        return (said or "").strip()


class Agent:
    """Настоящий агент в режиме печати: тот же харнесс, те же наши хуки.

    Настройки пользователя не читаются (`--setting-sources` пуст): наши хуки
    заняты у него в тех же точках, и без этого каждый ход сработал бы дважды —
    один раз в песочницу, один раз в живую базу.
    """

    name = "claude"
    BINARY = "claude"

    def __init__(self, box, model=None, budget=None, minutes=20.0):
        self.box = box
        self.model = model
        self.budget = budget
        self.seconds = minutes * 60

    def play(self, prompt, cwd, talk, turn=None, at=None, tools=None):
        cmd = [self.BINARY, "-p", prompt, "--output-format", "json",
               "--session-id", talk, "--settings", str(self.box.wiring_for(turn)),
               "--setting-sources", "", "--strict-mcp-config",
               "--permission-mode", "acceptEdits",
               "--disallowed-tools", tools or NO_SHELL]
        if self.model:
            cmd += ["--model", self.model]
        if self.budget:
            cmd += ["--max-budget-usd", str(self.budget)]
        said = missed = ""
        try:
            proc = self.box.spawn(cmd, cwd=str(cwd), env=self.box.env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
            said, missed = proc.communicate(timeout=self.seconds)
        except (OSError, subprocess.SubprocessError) as bad:
            return Reply("", talk, 0.0, "%s: %s" % (type(bad).__name__, bad))
        try:
            body = json.loads(said)
        except ValueError:
            return Reply("", talk, 0.0,
                         "агент ответил не json: %s" % (said or missed)[-300:])
        return Reply(text=str(body.get("result") or ""), session_id=talk,
                     cost=float(body.get("total_cost_usd") or 0.0),
                     error=body.get("error") if body.get("is_error") else None)


# Первая сессия: правки и чтение можно, оболочку нельзя — см. шапку модуля.
NO_SHELL = ("Bash Task WebSearch WebFetch KillShell BashOutput "
            "SlashCommand NotebookEdit")
# Вторая сессия: ответ должен прийти из памяти и головы агента, а не из осмотра
# песочницы. Иначе прогон мерил бы, умеет ли агент читать каталог.
NO_TOOLS = NO_SHELL + " Read Edit Write Glob Grep MultiEdit"

PLAYERS = {Replay.name: Replay, Agent.name: Agent}


def flat(where):
    """Имя каталога архива по пути: харнесс уплощает путь ровно так.

    Кириллица здесь схлопывается — каждая буква становится чертой, — и два
    разных места одной длины дают один каталог. Поэтому имя каталога хода несёт
    хвост из букв и цифр (`ground`): без него `альфа` и `гамма` встретились бы
    в одном каталоге архива, и разбор, суженный до каталога, склеил бы
    разговоры, которые прогон нарочно держит порознь.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(where))


def mark_of(cwd):
    got = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=str(cwd), capture_output=True, text=True)
    return got.stdout.strip() if got.returncode == 0 else ""


# --- что память сказала в этот разговор -------------------------------------

def verdict_of(box, talk):
    """Исход захода подсказки по ленте: вбросили или промолчали и почему.

    Спрашиваем ленту, а не догадываемся по ответу агента: имена причин ставит
    та ступень, где выдача опустела, и снаружи все они выглядят одинаково
    пусто.
    """
    rows = ledger.rows(box.state / "ledger.jsonl")
    mine = [row for row in rows if row.get("session_id") == talk]
    injected = any(row.get("event") == "injected" for row in mine)
    reason = next((row.get("reason") for row in reversed(mine)
                   if row.get("event") == "silent"), None)
    return injected, reason


def given_to(box, talk):
    """Что именно память отдала в этот разговор. Пусто — значит промолчала."""
    repo = db.Repository(box.db)
    try:
        return "\n".join(row.get("injected_content") or ""
                         for row in repo.injections(talk))
    finally:
        repo.close()


# --- отчёт ------------------------------------------------------------------

class Report:
    """Итог прогона: цифра, разбивка и след, по которому видно порядок."""

    def __init__(self, box, player, items):
        self.root = box.root
        self.player = player
        self.pairs = items
        self.played, self.asked, self.trail = [], [], []
        self.settled_at = None
        self.stalled = []
        self.cost = 0.0

    @property
    def total(self):
        return len(self.asked)

    @property
    def passed(self):
        return sum(1 for row in self.asked if bucket(row) == APPLIED)

    def note(self, stage, row):
        row["stage"] = stage
        (self.played if stage == "play" else self.asked).append(row)
        self.trail.append(row)

    def text(self):
        counted = tally(self.asked)
        aside = "" if self.player == Agent.name else (
            "  (отладочный: во второй сессии отвечает выдача памяти, "
            "цифру им снимать нельзя)")
        lines = ["проигрыватель: %s%s" % (self.player, aside),
                 "сыграно реплик: %d, задач поставлено: %d"
                 % (len(self.played), self.total),
                 "",
                 "итог: %d из %d" % (self.passed, self.total),
                 ""]
        for name in OUTCOMES:
            lines.append("%-26s %4d" % (name, counted[name]))
        for name, why in RESERVED.items():
            lines.append("%-26s %4s  не меряем: %s" % (name, "—", why))
        avoid = [row for row in self.asked if row.get("aim") == "avoid"]
        if avoid:
            alive = sum(1 for row in avoid if row.get("injected"))
            lines.append("")
            lines.append("отрицательных пар %d, из них с живой подсказкой %d "
                         "(остальные прошли на молчании и ничего не доказывают)"
                         % (len(avoid), alive))
        if self.cost:
            lines.append("")
            lines.append("потрачено: %.2f USD" % self.cost)
        if self.stalled:
            lines.append("не дождались фона на ходах: %s"
                         % ", ".join(str(n) for n in self.stalled))
        missed = [row for row in self.asked if bucket(row) == UNUSED]
        if missed:
            lines.append("")
            lines.append("отдала и не применил:")
            for row in missed[:10]:
                lines.append("  %-18s не хватило: %s"
                             % (row["id"], ", ".join(row["missed"]) or "—"))
        return "\n".join(lines)


# --- прогон -----------------------------------------------------------------

def talk_id():
    return str(uuid.uuid4())


def run(pairs=None, root=None, player="replay", limit=None, only=None,
        keep=False, live_hooks=True, model=None, budget=None, quiet=2.0,
        echo=None):
    """Обе сессии каждой пары подряд, в своей песочнице. Отдаёт отчёт."""
    say = echo or (lambda *_: None)
    items = list(pairs or [])
    if only:
        items = [pair for pair in items if only in pair["id"]]
    if limit:
        items = items[:limit]
    turns = script_of(items)

    box = Sandbox(root, live_hooks=live_hooks).open()
    made = PLAYERS[player](box, **({"model": model, "budget": budget}
                                   if player == Agent.name else {}))
    report = Report(box, player, items)
    try:
        say("--- сессии 1: %d реплик, проигрыватель %s ---" % (len(turns), player))
        places = {}
        for number, turn in enumerate(turns, 1):
            where = ground(box, turn)
            places[key_of(turn)] = where
            talk = talk_id()
            box.talks.append(talk)
            reply = made.play(turn["say"], where, talk, turn=turn, tools=NO_SHELL)
            when, stalled = settled(box.state, extra=[box.db], quiet=quiet)
            if stalled:
                report.stalled.append(number)
            report.cost += reply.cost
            report.note("play", {"session_id": talk, "at": when,
                                 "place": turn.get("place", ""),
                                 "error": reply.error})
            say("  %3d/%-3d %-22s %s" % (number, len(turns), turn.get("place", ""),
                                         reply.error or "сыграно"))

        # Сброс между этапами: база остаётся, сессия обнуляется. Ждём здесь ещё
        # раз, потому что последний ход осел, а цепочка конца хода могла успеть
        # взять замок только что.
        report.settled_at, stalled = settled(box.state, extra=[box.db], quiet=quiet)
        if stalled:
            report.stalled.append(0)

        say("")
        say("--- сессии 2: %d задач, каждая со своей сессии ---" % len(items))
        for number, pair in enumerate(items, 1):
            talk = talk_id()
            box.talks.append(talk)
            where = asked_from(box, pair, places)
            where.mkdir(parents=True, exist_ok=True)
            reply = made.play(pair["task"]["say"], where, talk, tools=NO_TOOLS)
            report.cost += reply.cost
            report.note("ask", judge_one(box, pair, reply))
            say("  %3d/%-3d %-18s %s"
                % (number, len(items), pair["id"], bucket(report.asked[-1])))
    finally:
        box.close(keep=keep)
    return report


def judge_one(box, pair, reply):
    """Разбор одной пары. Правило исхода — то же, что у прежней оценки.

    `evaluate.judge` берётся целиком и нарочно: своя копия правил разъехалась бы
    с прежней цифрой, и сравнивать стало бы нечего. Из его вердикта здесь не
    используется `found_in_answer` — он про ответ памяти, а отвечает теперь
    агент; «нашла или не нашла» говорит лента, по имени причины.
    """
    said = marks.strip(reply.text or "")
    injected, reason = verdict_of(box, reply.session_id)
    known = given_to(box, reply.session_id)
    verdict = evaluate.judge(pair, said, known, reply.error, raw=known)
    return {"id": pair["id"], "kind": pair.get("kind", ""),
            "aim": pair.get("aim", "apply"),
            "session_id": reply.session_id, "at": time.time(),
            "task": pair["task"]["say"], "ok": verdict["ok"],
            "injected": injected, "reason": reason,
            "intruded": bool(verdict["false_hits"]),
            "hits": verdict["hits"], "missed": verdict["missed"],
            "false_hits": verdict["false_hits"], "answer": said,
            "error": reply.error}


# --- команда ----------------------------------------------------------------

def parser():
    ap = argparse.ArgumentParser(prog="eval.live",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--pairs",
                    help="набор пар сессий; без него — нынешний набор через мост")
    ap.add_argument("--cases", default=str(pairs.LEGACY_CASES),
                    help="случаи прежнего набора, если идём через мост")
    ap.add_argument("--script", default=str(pairs.LEGACY_SCRIPT),
                    help="реплики прежнего набора, если идём через мост")
    ap.add_argument("--player", default=Agent.name, choices=sorted(PLAYERS),
                    help="claude — настоящий агент; replay — без модели, для отладки")
    ap.add_argument("--limit", type=int, help="взять только первые N пар")
    ap.add_argument("--only", help="только пары, чей id содержит эту строку")
    ap.add_argument("--model", help="модель хода, например haiku")
    ap.add_argument("--budget", type=float, help="потолок трат на один ход, USD")
    ap.add_argument("--root", help="каталог песочницы; по умолчанию свой на прогон")
    ap.add_argument("--keep", action="store_true", help="не убирать песочницу")
    ap.add_argument("--out", help="куда сложить построчный итог")
    return ap


def set_of(args):
    """Набор пар: свой файл, иначе нынешний набор через мост."""
    if args.pairs:
        return pairs.load(args.pairs)[1]
    return pairs.from_files(args.cases, args.script)


def main(argv=None):
    args = parser().parse_args(argv)

    def stop(signum, frame):
        raise KeyboardInterrupt()

    was = signal.signal(signal.SIGTERM, stop)
    try:
        try:
            items = set_of(args)
        except (pairs.PairSetError, OSError, ValueError) as bad:
            print(bad, file=sys.stderr)
            return 1
        try:
            report = run(pairs=items, root=args.root, player=args.player,
                         limit=args.limit, only=args.only, keep=args.keep,
                         model=args.model, budget=args.budget,
                         echo=lambda line: print(line, flush=True))
        except UnsafeRun as bad:
            print(bad, file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\nпрогон оборван, песочница убрана", file=sys.stderr)
            return 130
    finally:
        signal.signal(signal.SIGTERM, was)

    print()
    print(report.text())
    if args.out:
        Path(args.out).write_text(
            json.dumps(report.asked, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\nпострочный итог -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
