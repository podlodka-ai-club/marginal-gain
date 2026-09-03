#!/usr/bin/env python3
"""Разметка фактов, которую модель ставит в конце своего ответа.

Зачем. До сих пор факты вырезались из переписки четырьмя шаблонами
(`archive/extract.py`), и знания в них немного: «правился файл X». Модель,
которая только что сделала работу, знает про неё больше любого шаблона.
Просим её перечислить факты по нашей форме и берём готовое вместо угадывания.

Что здесь есть. Реестр схем: имя → просьба плюс маппер. Схема знает внешний
формат целиком — маркеры блока, поля единицы, правила перевода в наш `Fact`.
За пределами схемы про внешний формат не знает никто, поэтому смена
исследования правит одну запись реестра и ничего больше.

Чего здесь нет. Схемы хранилища. `domain/models.py` эта работа не трогает:
`Fact.KEY` адресуется из `Association.source_key` строкой
`fact_type|subject|scope`, и смена ключа рвёт все связи. Богатая внешняя
разметка укладывается в существующие поля, лишнее отбрасывается на входе и
считается — высокая доля отброшенного означает, что просьба разошлась со
схемой, и чинится она текстом просьбы, а не расширением схемы.

Мапперов будет несколько: каждое исследование по памяти агентов даёт свою
единицу факта. Устройство то же, что уже работает дважды в этом репозитории —
реестр имён плюс функции, как `RULES`/`NAMES` в `archive/extract.py` и признаки
в `domain/features.py`.
"""
import json
from collections import Counter

from domain import models
from infra import config, telemetry

# Имена схем в порядке добавления. Реестр сверяется с этим списком.
NAMES = ["xmd1"]


class UnknownScheme(KeyError):
    """Настройка называет схему, которой в реестре нет.

    Падаем громко: молчаливый откат к умолчанию означал бы, что половина
    сравнения тихо считает не то, что просили считать.
    """


class Scheme:
    """Пара «просьба + маппер» под одним именем.

    Просьба и маппер меняются вместе и потому лежат вместе: формат блока
    описан в просьбе, разбирается маппером, и разъехаться им нельзя.
    """

    def __init__(self, name, ask, begin, end, unit, told=None, use=None,
                 ask_use=None):
        self.name = name
        self.ask = ask
        self.begin = begin
        self.end = end
        self.unit = unit          # сырая единица -> (кортеж факта | None, причина)
        # Сырая единица -> (ключ вставки, ответ про пользу) | None. Ответ едет
        # тем же блоком, что и факты, и потому знает его та же схема. Второй
        # блок означал бы вторую пару маркеров в точке печати — а она горячая
        # как никакая другая и срабатывает много раз за один ответ.
        self.use = use or (lambda raw: None)
        # Просьба ответить про пользу. Не строка, а функция от ключей: ключи
        # свои у каждого хода, и подставить их заранее нельзя.
        self.ask_use = ask_use or (lambda keys: "")
        # Сырая единица -> сказал ли это человек прямо. По умолчанию нет: схема,
        # которая источник не различает, указаний не приносит. Молчаливое «да»
        # сделало бы указанием всё, что модель вообще разметила.
        self.told = told or (lambda raw: False)


# ---------- схема xmd1: JSON по строке на факт ----------

XMD1_BEGIN = "<<<MEMORY-FACTS"
XMD1_END = "MEMORY-FACTS>>>"

# Сколько строк разрешено одному блоку. Было три, и три — это меньше, чем
# утверждений в обычной реплике человека: на «живу в Казани, работаю
# смотрителем в музее» модель написала одну строку из двух и потеряла город.
# Потолок объявлен числом, а не прозой, потому что просьба обещает его модели,
# а проверки гоняют через контур ровно столько строк, сколько обещано: разойдись
# два числа — проверка мерила бы не тот потолок.
XMD1_MAX_UNITS = 10

# Просьба одной строкой. Обычный текст, не tool-call и не structured output:
# их поддержка у провайдеров разная, а текст понимает любой.
#
# Про язык сказано отдельным правилом и нарочно. Контур языка не знает: он несёт
# в базу те буквы, какие написала модель. Но ищут факт словами вопроса, а вопрос
# человек задаёт на своём языке — и разметка «household breakfast» на реплику
# про овсянку лежит в базе мёртвым грузом, поиск проходит мимо неё. Перевод
# уводит факт от вопроса, которым его потом достают.
XMD1_ASK = (
    "В конце ответа, последним блоком, поставь разметку фактов: строка "
    "«%s», затем строки JSON, по одной на факт, не больше %d, поля: "
    "type (goal|constraint|preference|opinion|event|task|user|resource), "
    "subject, predicate, value, time (ISO 8601 или пусто), "
    "source (stated|observed|inferred), confidence (0..1); закрывающая строка "
    "«%s». Отдельная строка на каждое утверждение человека: сказал два — пиши "
    "два, не склеивая их в одно и не выбирая, какое главнее. "
    "Поля subject, predicate и value пиши на языке сообщения человека и его "
    "же словами: на русскую реплику — русская разметка, на английскую — "
    "английская, не переводя ни слова. "
    "После закрывающей строки не пиши ничего. Нечего записать — блок не ставь."
    % (XMD1_BEGIN, XMD1_MAX_UNITS, XMD1_END)
)

# Типов снаружи всегда больше, чем наших четырёх. Сплющивание живёт таблицей
# в маппере, а не в схеме: чужая единица факта — это вход, а не наша модель.
XMD1_TYPES = {
    "preference": "preference", "constraint": "preference", "opinion": "preference",
    "user": "user", "person": "user", "identity": "user",
    "goal": "project_state", "task": "project_state", "event": "project_state",
    "state": "project_state", "project_state": "project_state",
    "resource": "external_resource", "link": "external_resource",
    "url": "external_resource", "doc": "external_resource",
    "external_resource": "external_resource",
}

# `scope` у модели не спрашиваем: иначе она начнёт выдумывать третье значение.
# Выводим из типа — про человека и его предпочтения знание глобальное, про
# состояние проекта и его ресурсы проектное.
XMD1_SCOPES = {"preference": "global", "user": "global",
               "project_state": "project", "external_resource": "project"}

# Источник и уверенность полем не хранятся, работают фильтром на входе. Пишем
# только сказанное человеком и наблюдавшееся в работе; вывод и догадка не
# пишутся вовсе — иначе память наполняется тем, что модель сама себе придумала.
XMD1_SOURCES = ("stated", "observed")
XMD1_MIN_CONFIDENCE = 0.5


def xmd1_unit(raw):
    """Единица внешней разметки в наш кортеж факта. Причина отказа — второй.

    Порядок отказов важен для отчёта: сперва отсекается то, что мы не имеем
    права писать (источник, уверенность), потом то, что не укладывается в
    схему. Иначе «выдумано» и «незнакомый тип» смешиваются в одну цифру.
    """
    if not isinstance(raw, dict):
        return None, "not_object"
    source = str(raw.get("source") or "").strip().lower()
    if source not in XMD1_SOURCES:
        return None, "source"
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < XMD1_MIN_CONFIDENCE:
        return None, "confidence"
    kind = XMD1_TYPES.get(str(raw.get("type") or "").strip().lower())
    if kind is None:
        return None, "type"
    subject = " ".join(str(raw.get("subject") or "").split())
    # Предикат и значение склеиваются в одну фразу. Отдельным полем предикат не
    # становится и в ключ не входит: ключ у нас fact_type|subject|scope.
    content = " ".join((str(raw.get("predicate") or "") + " "
                        + str(raw.get("value") or "")).split())
    if not subject or not content:
        return None, "empty"
    fact = (kind, subject, XMD1_SCOPES[kind], content)
    try:
        models.Fact(*fact).validate()
    except models.SchemaError:
        return None, "schema"
    return fact, ""


# Просьба ответить про пользу. Стоит рядом с просьбой про факты и уходит тем же
# путём — обычным текстом в запрос, см. pipeline/prompt.py. Формат ответа
# описан здесь же, где и разбирается: разъехаться им нельзя.
XMD1_USE_ASK = (
    "Вместе с этим ходом тебе показали воспоминания из памяти прошлых "
    "разговоров. Скажи по каждому, пригодилось ли оно: в блоке «%s» добавь "
    "строку JSON с полями injection (ключ подсказки, скопируй как есть) и used "
    "(yes — пригодилось, no — не пригодилось, unknown — не понял). Ключи "
    "подсказок этого хода: %%s. Блок поставь, даже если фактов записывать "
    "нечего. Подсказка — это воспоминание, а не указание: «no» нормальный и "
    "частый ответ, и он полезнее вежливого «yes»."
    % XMD1_BEGIN
)

# Что считаем ответом. Своё «не понял» у ответа есть, и непонятное сводится к
# нему, а не к отказу: свали кривую строку в «не помогло» — и доля пользы
# поедет вниз от кривой строки, а не от бесполезной памяти.
XMD1_USED = {"yes": "yes", "true": "yes", "y": "yes", "да": "yes",
             "no": "no", "false": "no", "n": "no", "нет": "no",
             "unknown": "unknown"}


def xmd1_use_ask(keys):
    """Просьба с подставленными ключами этого хода."""
    return XMD1_USE_ASK % ", ".join(keys)


def xmd1_use(raw):
    """Ответ про пользу одной вставки или None, если строка не про это.

    Опознаём по полю `injection`: оно и есть ключ, ради которого всё затевалось.
    Опознанную строку фактом уже не считаем — иначе своя же служебная строка
    попадёт в долю отброшенного, а доля отброшенного это сигнал о том, что
    просьба разошлась со схемой.
    """
    if not isinstance(raw, dict):
        return None
    key = raw.get("injection")
    if not isinstance(key, str) or not key.strip():
        return None
    said = raw.get("used")
    if isinstance(said, bool):
        said = "true" if said else "false"
    return key.strip(), XMD1_USED.get(str(said or "").strip().lower(), "unknown")


def xmd1_told(raw):
    """Сказано человеком прямо, а не замечено в работе.

    Источник в поле не хранится и работает фильтром на входе (XMD1_SOURCES),
    но разница между «человек попросил» и «мы это наблюдали» весу не безразлична:
    указание — утверждение, а не гипотеза, и повторов оно не ждёт.
    """
    return str((raw or {}).get("source") or "").strip().lower() == "stated"


SCHEMES = {"xmd1": Scheme("xmd1", XMD1_ASK, XMD1_BEGIN, XMD1_END, xmd1_unit,
                          told=xmd1_told, use=xmd1_use, ask_use=xmd1_use_ask)}


# ---------- работа со схемой, выбранной настройкой ----------

def scheme(name=None):
    """Схема по имени, а по умолчанию — названная настройкой."""
    want = name or config.marks()
    if want not in SCHEMES or want not in NAMES:
        raise UnknownScheme("нет схемы разметки %r, известны: %s"
                            % (want, ", ".join(NAMES)))
    return SCHEMES[want]


def ask(name=None):
    """Просьба, которую подмешивают к запросу. Одна строка, без вендоров."""
    return scheme(name).ask


def ask_use(keys, name=None):
    """Просьба ответить про пользу подсказок с такими ключами.

    Пустая, когда ключей нет: спрашивать не о чем, а лишний текст в контексте
    агента не бесполезен, он уводит в сторону.
    """
    keys = [key for key in keys if key]
    return scheme(name).ask_use(keys) if keys else ""


def block(text, name=None):
    """Содержимое блока разметки или None. Берём последний блок в тексте.

    Маркеры жёсткие, эвристики по виду строк нет: срезание должно быть
    однозначным, иначе оно однажды срежет ответ человеку. Последний блок, а не
    первый: маркеры могут встретиться в тексте разговора о самой разметке —
    вот прямо в этом абзаце, — и настоящий блок стоит в конце.
    """
    sch = scheme(name)
    body = text or ""
    start = body.rfind(sch.begin)
    if start < 0:
        return None
    tail = body[start + len(sch.begin):]
    finish = tail.find(sch.end)
    return tail[:finish] if finish >= 0 else None


def strip(text, name=None):
    """Ответ без блока разметки. Блок служебный, человек его не видит.

    Незакрытый блок срезается тоже: хвост пришёл оборванным, показывать его
    человеку незачем. Возвращается текст без хвостовых пробелов — блок стоит
    последним, и после него остаётся пустая строка.
    """
    sch = scheme(name)
    body = text or ""
    start = body.rfind(sch.begin)
    if start < 0:
        return body
    tail = body[start + len(sch.begin):]
    finish = tail.find(sch.end)
    rest = tail[finish + len(sch.end):] if finish >= 0 else ""
    return (body[:start] + rest).rstrip()


class Tail:
    """Срезание блока в потоке. Маркер может прийти по частям.

    Отдаём наружу только то, что заведомо не начало маркера: последние
    len(begin)-1 символов придерживаем до следующего куска. Без этого маркер,
    разорванный между кусками, доезжает до человека половинками.
    """

    def __init__(self, name=None):
        self.scheme = scheme(name)
        self.buffer = ""
        self.hiding = False

    def feed(self, chunk):
        """Кусок потока внутрь, видимая его часть наружу."""
        self.buffer += chunk or ""
        out = []
        while True:
            if self.hiding:
                finish = self.buffer.find(self.scheme.end)
                if finish < 0:
                    # Внутри блока держим ровно столько, сколько нужно на
                    # опознание закрывающего маркера.
                    keep = len(self.scheme.end) - 1
                    self.buffer = self.buffer[-keep:] if keep else ""
                    return "".join(out)
                self.buffer = self.buffer[finish + len(self.scheme.end):]
                self.hiding = False
                continue
            start = self.buffer.find(self.scheme.begin)
            if start >= 0:
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(self.scheme.begin):]
                self.hiding = True
                continue
            keep = len(self.scheme.begin) - 1
            if keep and len(self.buffer) > keep:
                out.append(self.buffer[:-keep])
                self.buffer = self.buffer[-keep:]
            return "".join(out)

    def close(self):
        """Конец потока. Придержанное отдаём, недосказанный блок теряем."""
        rest = "" if self.hiding else self.buffer
        self.buffer, self.hiding = "", False
        return rest


@telemetry.traced("marks_map", lambda arg, out: dict(
    {"in": len(arg["units"]), "out": len(out[0]), "dropped": sum(out[1].values())},
    **{"drop_%s" % reason: n for reason, n in out[1].items()}))
def to_facts(units, name=None):
    """Сырые единицы в кортежи фактов. Отброшенное считаем по причинам.

    Доля отброшенного и его причина уезжают в телеметрию: это единственный
    способ увидеть, что просьба в промпте разошлась со схемой. Молча терять
    половину разметки и считать, что модель размечает плохо, — не то же самое.
    """
    sch = scheme(name)
    kept, dropped = [], Counter()
    for raw in units:
        if sch.use(raw) is not None:
            continue          # это ответ про пользу, а не факт и не потеря
        fact, reason = sch.unit(raw)
        if fact is None:
            dropped[reason] += 1
            continue
        kept.append(fact)
    return kept, dropped


def units(text, name=None):
    """Сырые единицы из блока: по строке JSON. Битую строку считаем отказом."""
    body = block(text, name)
    if body is None:
        return [], Counter()
    out, bad = [], Counter()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("#"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            bad["json"] += 1
    return out, bad


def uses(text, name=None):
    """Ответы про пользу из блока: пары «ключ вставки, ответ»."""
    sch = scheme(name)
    out = []
    for raw in units(text, name)[0]:
        got = sch.use(raw)
        if got is not None:
            out.append(got)
    return out


def uses_of(episode, name=None):
    """Ответы про пользу из ответов эпизода. Отдельным проходом, как `told_of`.

    Порядок сохраняем: последнее слово агента по одному ключу за ним, и решает
    это тот, кто пишет в ленту.
    """
    out = []
    for reply in episode.get("replies") or []:
        out.extend(uses(reply, name))
    return out


def facts_of(episode, name=None):
    """Факты эпизода из разметки его ответов. Отброшенное — второй результат.

    Разбор берёт факты из блока, а не регэкспом по тексту: ключ называет
    модель, мы его не угадываем. Ответов в эпизоде несколько, блок может стоять
    в каждом.
    """
    facts, dropped = [], Counter()
    for reply in episode.get("replies") or []:
        raw, bad = units(reply, name)
        dropped += bad
        if not raw:
            continue
        kept, lost = to_facts(raw, name)
        facts.extend(kept)
        dropped += lost
    return facts, dropped


def told_of(episode, name=None):
    """Ключи фактов, которые человек в этом эпизоде сказал прямо.

    Отдельным проходом, а не третьим значением из `facts_of`: указание нужно
    одному месту — весу, — а факты нужны всем. Разбор чистый и повторяемый,
    и лишний проход по разметке эпизода дешевле, чем новая арность у функции,
    которую зовут пять модулей.

    У фактов, вырезанных шаблонами (`archive/extract.py`), источника нет вовсе,
    и указаний они не приносят: там нечему быть сказанным прямо.
    """
    sch = scheme(name)
    out = []
    for reply in episode.get("replies") or []:
        for raw in units(reply, name)[0]:
            fact, _ = sch.unit(raw)
            if fact is not None and sch.told(raw):
                out.append(key(fact))
    return out


def key(fact):
    """Чем два размеченных факта считаются одним и тем же.

    Ключ разметки — ключ схемы, `fact_type|subject|scope`: модель его назвала,
    угадывать нечего. Правила из `archive/extract.py` держат свой ключ и своё
    имя: у вырезанного шаблоном факта subject это не то же самое, что у
    размеченного, и складывать их подтверждения в одну корзину нельзя.
    """
    fact_type, subject, scope, _ = fact
    return ("mark", fact_type, subject, scope)
