#!/usr/bin/env python3
"""Локальная вторая память: та же схема XMD, но в SQLite рядом с проектом.

Зачем. Хранилище за сетью стоит ключа, задержки и квоты. Квота кончается ровно
тогда, когда идёт замер, и работа встаёт. Локальная база снимает все три
ограничения, а взамен теряет читателя: своей головы у неё нет, она умеет только
хранить и искать по словам.

Таблицы руками не выписаны. Они выводятся из `models` — единственного места,
где схема описана. Иначе локальная база разошлась бы со структурной записью, и
расхождение всплыло бы на записи, а не на проверке.

Путь к файлу задаётся переменной XMEM_LOCAL_PATH.
"""
import dataclasses
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from domain import context, folding, lifespan, models
from infra import config
# Слово вопроса — правило одно на поиск и на отсев, см. domain/query.py.
# Оттуда же сведение к основе: обе стороны сравнения размечает одна функция.
from domain.query import key as normal, words

DEFAULT_PATH = config.state_dir() / "memory.db"

# Питоновский тип поля -> тип колонки. Чего нет в таблице, кладём текстом:
# SQLite к типам относится свободно, а терять значение нельзя.
TYPES = {str: "TEXT", int: "INTEGER", float: "REAL", bool: "INTEGER"}

# Где искать слова запроса. Число задаёт вес: попадание в тему сильнее
# попадания в текст, потому что тема это то, о чём запись, а текст — как сказан.
# Здесь должны быть все объекты, в которые пишет продукт. Пока Session и Event
# отсутствовали, разговор ложился в базу и не находился ни одним запросом.
SEARCH = {
    "Fact": (("subject", 3), ("project", 2), ("content", 1)),
    "Episode": (("title", 3), ("project", 2), ("git_branch", 2), ("summary", 1)),
    "Event": (("tool_name", 3), ("project", 2), ("git_branch", 2), ("content", 1)),
    "Session": (("project", 3), ("git_branch", 2), ("working_directory", 1)),
}

# Потолок строк-кандидатов на объект. Отбор идёт запросом, а не перебором всей
# таблицы: событий в архиве десятки тысяч, а чтение стоит в горячем пути.
CANDIDATES = 300

# Вес объекта. Факт это добытое знание, эпизод — рассказ о работе, событие и
# разговор — сырьё и метаданные. Без этого веса на вопрос «какие файлы
# правились» первыми шли строки разговоров: у них попаданий больше, а пользы
# меньше.
PRIORITY = {"Fact": 4, "LapsedFact": 4, "Episode": 3, "Event": 2, "Session": 1}

# Сколько строк каждого вида берётся в выдачу. Приоритет выше был множителем, а
# множитель не гарантирует места: событий в базе 45 тысяч против 466 фактов, и
# десяток длинных команд закрывал собой всю десятку. Квота даёт факту место, но
# событие не запрещает: недобранное одним видом достаётся остальным.
QUOTA = {"Fact": 6, "LapsedFact": 6, "Episode": 3, "Event": 3, "Session": 1}

# Отложенное ищется только глубоким чтением. В первой выдаче ему не место —
# ровно ради этого его туда и переложили; отличать его порогом на чтении
# значило бы помнить про срок в каждом месте, где память спрашивают.
DEEP = {"LapsedFact": (("subject", 3), ("project", 2), ("content", 1))}

# Отбор по моменту: запись жива, если срок не вышел к названному моменту.
# Пустой срок не выходит никогда — забывать не по чему (ADR 0007).
LIVE_AT = ' AND ("valid_until" IS NULL OR "valid_until" = \'\' OR "valid_until" >= ?)'

# Отставное место одно на два повода: срок и свёртка. Момент отменяет только
# первое. Свёрнутое содержание несёт замена, и поднимать исходники значило бы
# показать одно утверждение дважды (ADR 0013).
NOT_MERGED = ' AND ("merged_into" IS NULL OR "merged_into" = \'\')'


def path():
    return Path(os.environ.get("XMEM_LOCAL_PATH") or DEFAULT_PATH)


def _table(object_type):
    return object_type.lower()


def _ddl(cls):
    """Таблица под объект схемы: колонка на поле, первичный ключ как в XMD."""
    cols = ['"%s" %s' % (f.name, TYPES.get(f.type, "TEXT"))
            for f in dataclasses.fields(cls)]
    keys = ", ".join('"%s"' % name for name in cls.KEY)
    return 'CREATE TABLE IF NOT EXISTS "%s" (%s, PRIMARY KEY (%s))' % (
        _table(cls.OBJECT), ", ".join(cols), keys)


def _v1(conn):
    for cls in models.OBJECTS.values():
        conn.execute(_ddl(cls))
    # Связь хранится обобщённо: у восьми связей схемы разное число концов,
    # и таблица на каждую дала бы восемь почти одинаковых таблиц.
    conn.execute("""CREATE TABLE IF NOT EXISTS links (
        relation TEXT NOT NULL, link_id TEXT NOT NULL, role TEXT NOT NULL,
        object_type TEXT NOT NULL, object_key TEXT NOT NULL,
        PRIMARY KEY (relation, link_id, role))""")
    # Текст, который не разобрался в запись. Экстрактора у локальной базы нет,
    # но молча терять вход нельзя: потерянное не отличить от ненаписанного.
    conn.execute("""CREATE TABLE IF NOT EXISTS raw_text (
        digest TEXT PRIMARY KEY, body TEXT NOT NULL, written_at TEXT)""")
    conn.execute('CREATE INDEX IF NOT EXISTS fact_subject ON fact (subject)')
    conn.execute('CREATE INDEX IF NOT EXISTS fact_project ON fact (project)')
    conn.execute('CREATE INDEX IF NOT EXISTS episode_project ON episode (project)')


def _v2(conn):
    """Срок факта и отложенное.

    Пустая база получает и то и другое от `_v1`: таблицы он выводит из `models`,
    а там уже и колонка, и объект. Поэтому шаг обязан быть терпимым к тому, что
    всё на месте, — иначе новая база не заведётся вовсе.
    """
    conn.execute(_ddl(models.LapsedFact))
    have = {row[1] for row in conn.execute('PRAGMA table_info("fact")')}
    if "valid_until" not in have:
        conn.execute('ALTER TABLE fact ADD COLUMN "valid_until" TEXT')
    # Переклад отбирает строки по сроку и делает это в фоне после каждого хода.
    conn.execute('CREATE INDEX IF NOT EXISTS fact_valid_until ON fact (valid_until)')
    conn.execute('CREATE INDEX IF NOT EXISTS lapsedfact_subject ON lapsedfact (subject)')


def _v3(conn):
    """Срок задним числом тем фактам, что записаны до появления срока.

    Без этого шага забывание включено вхолостую. Поле есть, но у лежащих фактов
    оно пусто, а пустой срок значит «не протухает никогда» — выбыть не может ни
    один. Само это не рассосётся: старые записи не переписываются, их дополняют
    по ключу, и срок появился бы только у тех, кого тронули заново.

    Считаем от «когда видели», а не от «сейчас». От «сейчас» все получили бы
    один день смерти и выбыли бы разом, а годовой факт прожил бы столько же,
    сколько вчерашний. Отметки, которой нет, `lifespan.until` подставляет
    «сейчас» — решение записано в ADR 0007: срок от неточного начала лучше, чем
    факт, который не выбудет никогда.

    Трогаем только пустое. Назначить срок впервые шаг вправе — он знает строку
    целиком, в отличие от продления, которое знает лишь ключ; переписать уже
    назначенное — нет, иначе он нарушил бы то же правило с другой стороны.

    Подпись факта (`fact_type|subject|scope`) не двигается: ею связь адресует
    факт, и смена подписи оборвала бы граф молча.
    """
    rows = conn.execute(
        'SELECT "fact_type", "subject", "scope", "updated_at" FROM "fact" '
        'WHERE "valid_until" IS NULL OR "valid_until" = \'\'').fetchall()
    for row in rows:
        conn.execute('UPDATE "fact" SET "valid_until" = ? WHERE "fact_type" = ? '
                     'AND "subject" = ? AND "scope" = ?',
                     (lifespan.until(row[3]), row[0], row[1], row[2]))


def _v4(conn):
    """Связь читается с обоих концов: от эпизода к факту и от факта к эпизоду.

    Первичный ключ связи это (relation, link_id, role) — по нему находится
    вторая роль той же карточки, но не находится сама карточка по концу.
    Обстановку факта спрашивают ровно в обратную сторону, и без этого указателя
    каждый вопрос читал бы таблицу связей целиком. Хук чтения стоит в горячем
    пути под сроком в 10 секунд.
    """
    conn.execute('CREATE INDEX IF NOT EXISTS links_end ON links '
                 '(relation, role, object_key)')


def _v5(conn):
    """Свёртка: пометка на замене и обратная пометка на отставном.

    Пустая база получает обе от `_v1` — таблицы он выводит из `models`, а там
    уже и колонки. Поэтому шаг обязан быть терпимым к тому, что всё на месте.

    Указатель по обратной пометке нужен развороту: он ищет группу по замене, и
    без него каждый разворот читал бы таблицу отставного целиком.
    """
    for table, column in (("fact", "merged_from"), ("lapsedfact", "merged_from"),
                          ("lapsedfact", "merged_into")):
        have = {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}
        if column not in have:
            conn.execute('ALTER TABLE "%s" ADD COLUMN "%s" TEXT' % (table, column))
    conn.execute('CREATE INDEX IF NOT EXISTS lapsedfact_merged_into '
                 'ON lapsedfact (merged_into)')


def _v6(conn):
    """Журнал аудита: строка на каждое действие конвейера, в этой же базе.

    Отдельная таблица, а не поле на существующих объектах схемы: строка аудита
    несёт вход и выход шага, а не состояние записи, и не привязана первичным
    ключом ни к одной из них — один факт проходит несколько шагов (разметка,
    запись, забывание), и у каждого свой вход и выход, свой успех или отказ.
    См. `domain.audit`.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, run_id TEXT, session_id TEXT, step TEXT NOT NULL,
        ok INTEGER NOT NULL, input TEXT, output TEXT)""")
    conn.execute('CREATE INDEX IF NOT EXISTS audit_run ON audit (run_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS audit_session ON audit (session_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS audit_step ON audit (step)')


MIGRATIONS = (_v1, _v2, _v3, _v4, _v5, _v6)


# Связь, которой факт достаёт свою обстановку. У самого факта в полях один
# `project`; ветка, каталог и время лежат у эпизода. Схему факта ради этого не
# трогаем: обстановок у факта несколько (он встречался не раз), и колонка
# оставила бы от них последнюю, затирая остальные молча.
CONTEXT_RELATION = "episode_facts"


def _key_json(key):
    """Ключ строкой ровно так, как его записала связь. Одно правило на оба конца.

    Пиши связь одним способом, а ищи другим — и поиск не найдёт ничего, не
    сказав об этом ни слова.
    """
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


def migrate(conn):
    """Шаги накатываются по номеру версии, записанному в самом файле."""
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS[have:], start=have + 1):
        step(conn)
        conn.execute("PRAGMA user_version = %d" % number)
    conn.commit()
    return len(MIGRATIONS) - have


# Имя, под которым сведение к основе доступно из SQL. Своя функция, а не
# встроенный `lower()`: встроенный знает одну латиницу, и `'в Казани' LIKE
# '%казани%'` не совпадал ни с чем. Заодно она же режет знак на краю слова и
# сводит словоформу — тем самым правилом, каким размечен вопрос.
KEY_SQL = "xkey"

# Имя, под которым размеченное поле живёт внутри запроса. С подчёркиванием: имя
# колонки продукта так начинаться не может, и столкнуться им не с чем.
_KEY_AS = "_xkey_%d"


def connect(where=None):
    target = Path(where) if where else path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Соединение переживает смену потока: адаптер держит один репозиторий на
    # процесс, а замер ходит в него из потоков. Замок ниже сериализует доступ;
    # без этого флага SQLite отказал бы первому же чужому потоку.
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Функция ставится на соединение, а не на запрос: ей пользуется всякий, кто
    # ищет словами по этой базе, и второй способ разметки означал бы второе
    # правило поиска.
    conn.create_function(KEY_SQL, 1, normal, deterministic=True)
    return conn


def like(term):
    """Слово в образец для LIKE. `_` и `%` в вопросе — буквы, а не подстановка.

    WORD пропускает подчёркивание в слова, а в LIKE оно совпадает с любым
    одиночным символом: запрос про on_prompt.py находил заодно onXprompt.py.
    """
    for sign in ("\\", "%", "_"):
        term = term.replace(sign, "\\" + sign)
    return "%" + term + "%"


def _columns(object_type):
    """Колонки, которыми вид уходит в выдачу. Их же перечисляет запрос.

    Нужно там, где вид в выдаче не совпадает с таблицей: отложенное, живое на
    момент замера, идёт фактом, и отметка переклада на нём выдавала бы, что
    строка успела съездить в отставку. Заодно это единственная форма, в которой
    две таблицы складываются одним запросом.
    """
    known = models.OBJECTS.get(object_type)
    if known is None:
        return "*"
    return ", ".join('"%s"' % f.name for f in dataclasses.fields(known))


def _order(object_type):
    """Порядок, в котором режется потолок кандидатов. По ключу записи."""
    known = models.OBJECTS.get(object_type)
    if known is None:
        return "1"
    return ", ".join('"%s"' % name for name in known.KEY)


def _sign(object_type, record):
    """Подпись записи для развода равного веса и отсева повторов.

    Берём саму запись, а не ключ: у видов ключи разные, а сравнивать надо
    строки из разных таблиц. Одинаковое содержание — одна подпись, где бы оно
    ни лежало.
    """
    return "%s|%s" % (object_type, json.dumps(record, sort_keys=True,
                                              ensure_ascii=False, default=str))


class Repository:
    """Единственное место, где локальная память знает про SQL.

    Наружу отдаёт записи словарями — в той же форме, в какой их отдаёт сеть.
    Поэтому подсказка не различает, откуда пришёл ответ.
    """

    def __init__(self, where=None):
        self.conn = connect(where)
        self.lock = threading.RLock()
        migrate(self.conn)

    def close(self):
        self.conn.close()

    # --- запись -------------------------------------------------------------

    def upsert(self, object_type, key, values):
        """Кладём строку по первичному ключу схемы.

        Пустые поля в values не приходят (их отсекает `Record.values`), поэтому
        повторная запись дополняет строку, а не затирает её пустотой.
        """
        cls = models.OBJECTS.get(object_type)
        if cls is None:
            raise ValueError("нет такого объекта схемы: %s" % object_type)
        known = {f.name for f in dataclasses.fields(cls)}
        row = dict(key)
        row.update({k: v for k, v in values.items() if k in known})
        names = list(row)
        holes = ", ".join("?" * len(names))
        cols = ", ".join('"%s"' % n for n in names)
        # EARLIEST держит самое раннее из виденного, LATEST — самое позднее.
        # COALESCE с обеих сторон: пустое не должно выигрывать у заполненного,
        # а MIN(x, NULL) в SQLite это NULL. Перезапись знает факт целиком и
        # потому вправе назначить срок тому, у кого его ещё нет.
        earliest = set(getattr(cls, "EARLIEST", ()))
        latest = set(getattr(cls, "LATEST", ()))
        updates = ", ".join(
            ('"%s" = %s(COALESCE("%s", excluded."%s"), COALESCE(excluded."%s", "%s"))'
             % (n, "MIN" if n in earliest else "MAX", n, n, n, n))
            if n in earliest or n in latest else ('"%s" = excluded."%s"' % (n, n))
            for n in names if n not in cls.KEY)
        keys = ", ".join('"%s"' % n for n in cls.KEY)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s) ON CONFLICT (%s) DO %s' % (
            _table(object_type), cols, holes, keys,
            ("UPDATE SET " + updates) if updates else "NOTHING")
        with self.lock:
            self.conn.execute(sql, [_plain(row[n]) for n in names])

    def update(self, object_type, key, values):
        """Обновление адресует лежащую строку. Нет строки — нечего обновлять.

        Не upsert: обновление несёт только изменившееся, и вставка по нему
        завела бы строку с одним ключом и без содержимого — призрак, которого
        в архиве не было. Так продление срока по ключу из чужой выдачи
        воскрешало бы факты, давно уехавшие в отложенное.
        """
        cls = models.OBJECTS.get(object_type)
        if cls is None:
            raise ValueError("нет такого объекта схемы: %s" % object_type)
        known = {f.name for f in dataclasses.fields(cls)}
        row = {k: v for k, v in values.items() if k in known and k not in cls.KEY}
        if not row:
            return 0
        # Поле из LATEST обновление только отодвигает и только у того, у кого
        # оно уже есть. Пустой срок значит «не протухает»: продление не вправе
        # назначить конец тому, кому его не назначали, — оно знает лишь ключ.
        latest = set(getattr(cls, "LATEST", ()))
        names = list(row)
        sets = ", ".join(
            ('"%s" = CASE WHEN "%s" IS NULL OR "%s" = \'\' THEN "%s" '
             'ELSE MAX("%s", ?) END' % (n, n, n, n, n)) if n in latest
            else ('"%s" = ?' % n) for n in names)
        where = " AND ".join('"%s" = ?' % n for n in key)
        with self.lock:
            return self.conn.execute(
                'UPDATE "%s" SET %s WHERE %s' % (_table(object_type), sets, where),
                [_plain(row[n]) for n in names] + [_plain(v) for v in key.values()]
            ).rowcount

    def delete(self, object_type, key):
        where = " AND ".join('"%s" = ?' % n for n in key)
        with self.lock:
            self.conn.execute('DELETE FROM "%s" WHERE %s' % (_table(object_type), where),
                              [_plain(v) for v in key.values()])

    def link(self, relation, endpoints):
        """Связь ставится по ключам концов: объекты уже лежат в базе."""
        if relation not in models.RELATIONS:
            raise ValueError("нет такой связи: %s" % relation)
        stamp = json.dumps([[e["object_name"], e["key"]] for e in endpoints],
                           sort_keys=True, ensure_ascii=False)
        link_id = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16]
        for end in endpoints:
            with self.lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO links "
                    "(relation, link_id, role, object_type, object_key) VALUES (?, ?, ?, ?, ?)",
                    (relation, link_id, end["object_name"],
                     models.RELATIONS[relation][end["object_name"]],
                     _key_json(end["key"])))
        return link_id

    def apply(self, mutations):
        """Тот же список мутаций, что уходит в сеть. Форму задаёт `models`."""
        done = 0
        for item in mutations:
            if "object_mutation" in item:
                body = item["object_mutation"]
                object_type = body["object_type"]
                for op in ("create", "update", "delete"):
                    if op not in body:
                        continue
                    if op == "delete":
                        self.delete(object_type, body[op]["key"])
                    elif op == "update":
                        self.update(object_type, body[op]["key"],
                                    body[op].get("values") or {})
                    else:
                        self.upsert(object_type, body[op]["key"],
                                    body[op].get("values") or {})
                    done += 1
            elif "relation_mutation" in item:
                body = item["relation_mutation"]
                self.link(body["relation_type"], body["create"]["endpoints"])
                done += 1
            else:
                raise ValueError("неизвестная мутация: %s" % ", ".join(item))
        with self.lock:
            self.conn.commit()
        return done

    def put_text(self, text, written_at=None):
        """Текст, который не разобрался. Хранится, чтобы потерю было видно."""
        digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO raw_text (digest, body, written_at) "
                              "VALUES (?, ?, ?)", (digest, text, written_at))
            self.conn.commit()
        return digest

    # --- чтение -------------------------------------------------------------

    def facts_at(self, as_of=None):
        """Откуда читать факты. Одно место на все выборки, где факт участвует.

        Момент (`as_of`) отвечает на вопрос «что было живо тогда»: срок лежит на
        самой записи, переклад двигает её между таблицами и срока не трогает, —
        значит состав на любой момент восстанавливается сроком и только им.

        Отдаёт кусок SQL на место имени таблицы, а не список найденного. Читают
        факты четыре выборки — поиск, шаг по графу, обстановка, срез, — и
        обойди момент хоть одну, цифра замера снова поедет от того, когда в
        последний раз гоняли забывание.

        Не задан — то же имя таблицы, что было. Работа ходит так.
        """
        if not as_of:
            return '"fact"', []
        cols = _columns("Fact")
        return ('(SELECT %s FROM "fact" WHERE 1=1%s UNION ALL '
                'SELECT %s FROM "lapsedfact" WHERE 1=1%s%s)'
                % (cols, LIVE_AT, cols, LIVE_AT, NOT_MERGED), [as_of, as_of])

    def sources(self, deep=False, as_of=None):
        """Откуда берутся строки: вид в выдаче, поля с весами, выборка под ними.

        Поднятое из отложенного идёт видом Fact, а не своим, и приходит тем же
        запросом, что живые факты. Двумя запросами потолок кандидатов резал бы
        каждую таблицу отдельно, и после переклада под нож попадали бы другие
        строки: состав на момент тот же, а выдача другая — то есть цифра снова
        зависит от часов.

        Глубокое чтение и момент вместе не ходят. Глубокому отложенное нужно
        всё целиком, и добавь мы к нему поднятое — те же строки пришли бы
        дважды, под двумя видами.
        """
        out = []
        for object_type, fields in SEARCH.items():
            if object_type == "Fact" and not deep:
                source, params = self.facts_at(as_of)
            else:
                source, params = '"%s"' % _table(object_type), []
            out.append((object_type, fields, source, params))
        if deep:
            for object_type, fields in DEEP.items():
                out.append((object_type, fields, '"%s"' % _table(object_type), []))
        return out

    def search(self, query, limit=10, deep=False, as_of=None):
        """Поиск по словам вопроса. Головы у базы нет, есть совпадения.

        Вес складывается по полям: попадание в тему весит больше, чем в текст.
        Строки без единого попадания не возвращаются вовсе — молчание честнее
        случайной строки.

        Глубокое чтение (`deep`) добавляет к выборке отложенное. Обычное — нет,
        и в этом весь смысл переклада: просроченное выбывает из первой выдачи
        само, а не отсеивается порогом на каждом чтении.

        Момент (`as_of`) отвечает на вопрос «что было живо тогда», см.
        `facts_at`. Не задан — выдача ровно та, что была до момента.

        Равный вес разводится подписью записи, а не порядком строк в таблице:
        порядок строк меняется от переклада, от свёртки и от порядка записи, а
        замер обязан давать одну цифру дважды. По той же причине потолок
        кандидатов режет по ключу записи, а не по тому, как строки легли.
        """
        terms = words(query)
        if not terms:
            return []
        found = []
        for object_type, fields, source, source_params in self.sources(deep, as_of):
            names = [name for name, _ in fields]
            # Сравниваем ключ поля с основой слова вопроса, а не поле со словом.
            # Прямое сравнение спотыкалось трижды: заглавная русская буква,
            # точка на конце предложения и чужая словоформа. Ключ снимает все
            # три сразу и на обеих сторонах одинаково.
            #
            # Ключ считается раз на поле, а не раз на слово вопроса. Разметка —
            # питонья функция, и SQLite её вызовы не схлопывает: написанная в
            # лоб, она разбирала бы содержимое строки заново под каждое слово.
            # На промахе, где приходится прочесть всю таблицу, это была разница
            # в порядок — а промах в горячем пути случай обычный.
            keys = ", ".join('%s("%s") AS "%s"' % (KEY_SQL, name, _KEY_AS % number)
                             for number, name in enumerate(names))
            where = " OR ".join('"%s" LIKE ? ESCAPE \'\\\'' % (_KEY_AS % number)
                                for number in range(len(names)) for _ in terms)
            params = list(source_params) + [like(term) for _ in names for term in terms]
            with self.lock:
                rows = self.conn.execute(
                    'SELECT %s FROM (SELECT %s, %s FROM %s) '
                    'WHERE (%s) ORDER BY %s LIMIT %d'
                    % (_columns(object_type), _columns(object_type), keys, source,
                       where, _order(object_type), CANDIDATES), params).fetchall()
            for row in rows:
                score = 0
                for name, weight in fields:
                    value = normal(row[name]) if row[name] else ""
                    if not value:
                        continue
                    score += weight * sum(1 for t in terms if t in value)
                if score:
                    record = dict(row)
                    found.append((score * PRIORITY.get(object_type, 1),
                                  _sign(object_type, record), object_type, record))
        found.sort(key=lambda item: (-item[0], item[1]))
        out, taken, seen = [], {}, set()
        for score, sign, object_type, row in found:
            if len(out) >= limit:
                break
            # Одна и та же запись могла прийти из двух таблиц: переклад её
            # переложил, а свёртка вернула. Второй раз она бы только съела место.
            if sign in seen:
                continue
            if taken.get(object_type, 0) >= QUOTA.get(object_type, limit):
                continue
            taken[object_type] = taken.get(object_type, 0) + 1
            seen.add(sign)
            record = {k: v for k, v in row.items() if v not in (None, "")}
            record["object_type"] = object_type
            out.append(record)
        # Место, не занятое своим видом, отдаём остальным: квота гарантирует
        # место, а не отнимает его. Иначе выдача выходила бы короче потолка при
        # полной базе.
        if len(out) < limit:
            for score, sign, object_type, row in found:
                if len(out) >= limit:
                    break
                if sign in seen:
                    continue
                seen.add(sign)
                record = {k: v for k, v in row.items() if v not in (None, "")}
                record["object_type"] = object_type
                out.append(record)
        return out

    def state(self, as_of=None):
        """На каком состоянии базы сделан замер. Чем отвечают на «на чём считали».

        Три числа, а не одно: переклад переливает факты в отложенные, не меняя
        состава живого на момент. Видно оба берега и то, что между ними не
        течёт.
        """
        live = LIVE_AT if as_of else ""
        args = [as_of] if as_of else []
        with self.lock:
            facts = self.conn.execute('SELECT count(*) FROM "fact"').fetchone()[0]
            lapsed = self.conn.execute(
                'SELECT count(*) FROM "lapsedfact" WHERE 1=1%s' % NOT_MERGED).fetchone()[0]
            alive = self.conn.execute(
                'SELECT count(*) FROM "fact" WHERE 1=1%s' % live, args).fetchone()[0]
            if as_of:
                alive += self.conn.execute(
                    'SELECT count(*) FROM "lapsedfact" WHERE 1=1%s%s' % (live, NOT_MERGED),
                    args).fetchone()[0]
        return {"facts": facts, "lapsed": lapsed, "alive": alive,
                "as_of": as_of, "path": str(path())}

    def neighbours(self, keys, limit=10, as_of=None):
        """Факты, связанные карточкой с любым из названных.

        Обход по ключу, а не поиск словами: связь адресует факт строкой
        `fact_type|subject|scope`, и найти по ней строку можно только так.
        Возвращает пары (запись факта, вес связи), тяжёлые связи первыми.

        Сам факт-источник в соседи не попадает: он уже в выдаче, и второй раз
        он бы только съел потолок.
        """
        if not keys:
            return []
        holes = ", ".join("?" * len(keys))
        with self.lock:
            rows = self.conn.execute(
                "SELECT source_key, target_key, weight FROM association "
                "WHERE source_key IN (%s) OR target_key IN (%s) "
                "ORDER BY weight DESC LIMIT %d" % (holes, holes, limit * 4),
                list(keys) + list(keys)).fetchall()
        known, out = set(keys), []
        for source, target, weight in rows:
            other = target if source in known else source
            if other in known:
                continue
            known.add(other)
            # Разбор общий со схемой: тема факта может содержать разделитель,
            # и деление слева направо теряло бы такого соседа молча.
            end = models.Fact.of_identity(other)
            if not (end.fact_type and end.subject and end.scope):
                continue
            source, params = self.facts_at(as_of)
            with self.lock:
                found = self.conn.execute(
                    'SELECT %s FROM %s WHERE fact_type = ? AND subject = ? '
                    'AND scope = ?' % (_columns("Fact"), source),
                    params + [end.fact_type, end.subject, end.scope]).fetchone()
            if found is None:
                continue        # связь пережила факт: конец есть, строки нет
            record = {k: v for k, v in dict(found).items() if v not in (None, "")}
            record["object_type"] = "Fact"
            out.append((record, weight))
            if len(out) >= limit:
                break
        return out

    def lapse(self, now, dry=False):
        """Просроченные факты уезжают в отложенное. Переклад, а не удаление.

        Обе половины под одним замком и одной фиксацией: оборвись переклад
        посередине, запись пропала бы совсем — а вся затея в том, чтобы она
        осталась цела и её можно было достать глубоким чтением.

        Отбор идёт сравнением строк, поэтому формат срока обязан быть один; его
        держит `domain.lifespan.stamp`. Пустой срок не выходит никогда: факт без
        срока забывать не по чему, и выбрасывать его молча нельзя.
        """
        shared = [f.name for f in dataclasses.fields(models.Fact)]
        names = ", ".join('"%s"' % name for name in shared)
        where = ('"valid_until" IS NOT NULL AND "valid_until" != \'\' '
                 'AND "valid_until" < ?')
        with self.lock:
            if dry:
                return self.conn.execute(
                    'SELECT count(*) FROM "fact" WHERE %s' % where, (now,)).fetchone()[0]
            # Читаем то, что вот-вот перекладываем — иначе аудиту нечего было бы
            # сказать, кроме числа: «сколько» без «что именно» это та же немота,
            # которую задача и просит устранить.
            gone = [dict(row) for row in self.conn.execute(
                'SELECT %s FROM "fact" WHERE %s' % (names, where), (now,))]
            moved = self.conn.execute(
                'INSERT OR REPLACE INTO "lapsedfact" (%s, "lapsed_at") '
                'SELECT %s, ? FROM "fact" WHERE %s' % (names, names, where),
                (now, now)).rowcount
            self.conn.execute('DELETE FROM "fact" WHERE %s' % where, (now,))
            self.conn.commit()
        from domain import audit
        audit.record("forget", input={"now": now},
                     output={"moved": [{"fact_type": r["fact_type"],
                                        "subject": r["subject"], "scope": r["scope"],
                                        "content": r.get("content")} for r in gone]},
                     ok=True)
        return moved

    # --- свёртка ------------------------------------------------------------

    def _identity(self, row):
        return models.Fact(fact_type=row["fact_type"], subject=row["subject"],
                           scope=row["scope"]).identity()

    def fold(self, now, dry=False):
        """Группы записей про одно и то же сворачиваются в одну. Отдаёт, сколько ушло.

        Правило слияния и выбор замены лежат в `domain.folding` — здесь только
        SQL. Разложи их по двум местам, и «что считается одним фактом»
        разъедется с тем, что база сворачивает на самом деле.

        Всё под одним замком и одной фиксацией, как переклад по сроку: оборвись
        свёртка посередине, запись пропала бы совсем — а вся затея в том, чтобы
        она осталась цела и её можно было достать глубоким чтением.
        """
        shared = [f.name for f in dataclasses.fields(models.Fact)]
        names = ", ".join('"%s"' % name for name in shared)
        merges = []
        with self.lock:
            rows = [dict(row) for row in self.conn.execute('SELECT * FROM "fact"')]
            moved = 0
            for group in folding.groups(rows):
                keep = folding.survivor(group)
                others = [row for row in group if row is not keep]
                moved += len(others)
                if dry:
                    continue
                keep_key, dead = self._identity(keep), []
                for row in others:
                    dead.append(self._identity(row))
                    self.conn.execute(
                        'INSERT OR REPLACE INTO "lapsedfact" (%s, "lapsed_at", '
                        '"merged_into") VALUES (%s, ?, ?)'
                        % (names, ", ".join("?" * len(shared))),
                        [row.get(name) for name in shared] + [now, keep_key])
                    self.conn.execute(
                        'DELETE FROM "fact" WHERE "fact_type" = ? AND "subject" = ? '
                        'AND "scope" = ?',
                        (row["fact_type"], row["subject"], row["scope"]))
                self._mark(keep, dead)
                self._rewire(dead, keep_key)
                merges.append({"kept": keep_key, "merged": dead,
                              "content": keep.get("content")})
            if not dry:
                self.conn.commit()
        if not dry:
            from domain import audit
            audit.record("fold", input={"now": now}, output={"merges": merges},
                         ok=True)
        return moved

    def _mark(self, keep, dead):
        """На замене — подписи всех, кто в неё свернулся. Накапливая, не затирая.

        Свернуть в одну и ту же запись могут двумя заходами: дубли приезжают
        разбором архива и после первой свёртки. Затри пометку — и первая
        половина группы стала бы неотличима от выбывшей по сроку.
        """
        was = [line for line in (keep.get("merged_from") or "").splitlines() if line]
        self.conn.execute(
            'UPDATE "fact" SET "merged_from" = ? WHERE "fact_type" = ? '
            'AND "subject" = ? AND "scope" = ?',
            ("\n".join(sorted(set(was) | set(dead))), keep["fact_type"],
             keep["subject"], keep["scope"]))

    def _rewire(self, dead, keep_key):
        """Концы связей со свёрнутых записей переезжают на замену.

        Связь адресует факт подписью (ADR 0004), и снесённая подпись оставила
        бы связь висеть в пустоте: обход по графу такой конец молча пропускает,
        то есть сосед свёрнутого факта пропал бы вместе с ним.

        Веса при столкновении складываются: карточка считает наблюдения, а
        наблюдали обе половины. Связь, у которой оба конца свернулись в один,
        выбрасывается — сама на себя запись не ссылается.
        """
        if not dead:
            return
        holes = ", ".join("?" * len(dead))
        # Связь с эпизодом и вброс адресуют факт через таблицу links; там
        # подпись лежит записью ключа, и правило её записи одно на всех.
        for key in dead:
            self.conn.execute(
                "UPDATE links SET object_key = ? WHERE object_type = 'Fact' "
                "AND object_key = ?",
                (_key_json(models.Fact.of_identity(keep_key).key()),
                 _key_json(models.Fact.of_identity(key).key())))
        rows = [dict(row) for row in self.conn.execute(
            'SELECT * FROM "association" WHERE "source_key" IN (%s) '
            'OR "target_key" IN (%s)' % (holes, holes), list(dead) * 2)]
        for row in rows:
            self.conn.execute(
                'DELETE FROM "association" WHERE "source_key" = ? '
                'AND "target_key" = ? AND "cue" = ?',
                (row["source_key"], row["target_key"], row["cue"]))
        gone = set(dead)
        for row in rows:
            source = keep_key if row["source_key"] in gone else row["source_key"]
            target = keep_key if row["target_key"] in gone else row["target_key"]
            if source == target:
                continue
            self.conn.execute(
                'INSERT INTO "association" ("source_key", "target_key", "cue", '
                '"weight", "observed_at", "first_seen_at") VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT ("source_key", "target_key", "cue") DO UPDATE SET '
                '"weight" = COALESCE("weight", 0) + COALESCE(excluded."weight", 0), '
                '"observed_at" = MAX(COALESCE("observed_at", excluded."observed_at"), '
                'COALESCE(excluded."observed_at", "observed_at")), '
                '"first_seen_at" = MIN(COALESCE("first_seen_at", excluded."first_seen_at"), '
                'COALESCE(excluded."first_seen_at", "first_seen_at"))',
                (source, target, row["cue"], row["weight"], row["observed_at"],
                 row["first_seen_at"]))

    def folded(self, identity):
        """Записи, свернувшиеся в названную. То, из чего собрана замена."""
        with self.lock:
            rows = self.conn.execute(
                'SELECT * FROM "lapsedfact" WHERE "merged_into" = ?',
                (identity,)).fetchall()
        return [dict(row) for row in rows]

    def unfold(self, identity):
        """Разворот: исходные возвращаются в живую таблицу, пометка снимается.

        Трогает только своё — отставное с пометкой свёртки. Выбывшее по сроку
        разворот воскрешать не вправе: у него другой повод и другой способ
        вернуться, см. `pipeline.forget`.

        Строку, которая за это время завелась заново, не затираем: разбор
        архива мог записать тот же факт после свёртки, и он свежее нашей копии.
        Содержание у них совпадает — на том и стоит правило слияния.

        Связи назад не расходятся: они переехали на замену и там остаются.
        Обратимость обещана записям, а не графу, см. ADR 0013.
        """
        shared = [f.name for f in dataclasses.fields(models.Fact)]
        names = ", ".join('"%s"' % name for name in shared)
        end = models.Fact.of_identity(identity)
        with self.lock:
            rows = self.conn.execute(
                'SELECT * FROM "lapsedfact" WHERE "merged_into" = ?',
                (identity,)).fetchall()
            for row in rows:
                self.conn.execute(
                    'INSERT OR IGNORE INTO "fact" (%s) VALUES (%s)'
                    % (names, ", ".join("?" * len(shared))),
                    [row[name] for name in shared])
            self.conn.execute('DELETE FROM "lapsedfact" WHERE "merged_into" = ?',
                              (identity,))
            self.conn.execute(
                'UPDATE "fact" SET "merged_from" = NULL WHERE "fact_type" = ? '
                'AND "subject" = ? AND "scope" = ?',
                (end.fact_type, end.subject, end.scope))
            self.conn.commit()
        return len(rows)

    # --- обстановка и срезы по ней ------------------------------------------

    def _pairs(self, fact_keys=None):
        """Пары (ключ факта, ключ эпизода) связи `episode_facts`, строками links.

        Один запрос на оба случая — весь граф и его кусок по названным фактам.
        Разведи их на две выборки, и «факты вторника» разойдётся с «уместно во
        вторник», оставшись правым поодиночке.
        """
        sql = ("SELECT link_id, role, object_key FROM links WHERE relation = ?")
        params = [CONTEXT_RELATION]
        if fact_keys is not None:
            if not fact_keys:
                return []
            holes = ", ".join("?" * len(fact_keys))
            sql += (" AND link_id IN (SELECT link_id FROM links WHERE relation = ? "
                    "AND role = 'fact' AND object_key IN (%s))" % holes)
            params += [CONTEXT_RELATION] + list(fact_keys)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        cards = {}
        for link_id, role, object_key in rows:
            cards.setdefault(link_id, {})[role] = object_key
        return [(card["fact"], card["episode"]) for card in cards.values()
                if "fact" in card and "episode" in card]

    def _episodes(self, keys):
        """Строки эпизодов по их ключам в той же записи, что лежит в links."""
        want = {}
        for raw in keys:
            try:
                key = json.loads(raw)
            except ValueError:
                continue        # чужая строка в таблице связей — не наша забота
            want.setdefault((key.get("session_id"), key.get("episode_number")), raw)
        out = {}
        for (session_id, number), raw in want.items():
            with self.lock:
                row = self.conn.execute(
                    'SELECT * FROM "episode" WHERE "session_id" = ? '
                    'AND "episode_number" = ?', (session_id, number)).fetchone()
            if row is not None:
                out[raw] = dict(row)
        return out

    def _fact_rows(self, keys=None, as_of=None):
        """Строки фактов: {подпись: строка или None}. Ключи не заданы — все.

        Подпись (`fact_type|subject|scope`) — та же строка, какой факт
        адресует связь. Другой ключ здесь оборвал бы граф молча.
        """
        out = {}
        source, params = self.facts_at(as_of)
        if keys is None:
            with self.lock:
                found = self.conn.execute(
                    'SELECT %s FROM %s' % (_columns("Fact"), source), params).fetchall()
            for row in found:
                record = dict(row)
                out[models.Fact(fact_type=record["fact_type"],
                                subject=record["subject"],
                                scope=record["scope"]).identity()] = record
            return out
        for key in keys:
            end = models.Fact.of_identity(key)
            with self.lock:
                row = self.conn.execute(
                    'SELECT %s FROM %s WHERE "fact_type" = ? AND "subject" = ? '
                    'AND "scope" = ?' % (_columns("Fact"), source),
                    params + [end.fact_type, end.subject, end.scope]).fetchone()
            out[key] = dict(row) if row is not None else None
        return out

    def _situations(self, rows, whole=False):
        """Обстановки фактов по уже прочитанным строкам. Одно место на всех.

        Обстановок у факта несколько — он встречался в разных эпизодах, в
        разных ветках и в разные дни. Своя строка факта идёт в список всегда: в
        ней есть проект и отметка «когда видели», и это обстановка даже у
        факта, не связанного ни с одним эпизодом.
        """
        by_json = {_key_json(models.Fact.of_identity(key).key()): key for key in rows}
        pairs = self._pairs(None if whole else list(by_json))
        episodes = self._episodes({episode for _fact, episode in pairs})
        out = {key: [] for key in rows}
        # Глобальный факт — знание про человека, а не про место, и обстановки у
        # него нет никакой. Связь с эпизодом у него при этом есть: её ставит
        # разбор хода всем фактам эпизода без разбора. Возьми её обстановку — и
        # привычка человека окажется заперта в том проекте, где её впервые
        # заметили, ровно вопреки правилу `context.UNBOUND_SCOPE`.
        unbound = {key for key, row in rows.items()
                   if row and row.get("scope") == context.UNBOUND_SCOPE}
        for fact_key, episode_key in pairs:
            name = by_json.get(fact_key)
            row = episodes.get(episode_key)
            if name is None or row is None or name in unbound:
                continue        # связь пережила факт или эпизод: строки нет
            found = context.of(row)
            if found not in out[name]:
                out[name].append(found)
        for key, record in rows.items():
            if record is None:
                continue
            own = context.of(record)
            if any(value is not None for value in own.values()) and own not in out[key]:
                out[key].append(own)
        return out

    def situations(self, keys=None, as_of=None):
        """Обстановки фактов: {подпись факта: [обстановка, ...]}.

        `keys` не задан — отвечаем про всю базу; это спрашивает срез. Задан —
        только про названное; это спрашивает чтение в горячем пути.
        """
        return self._situations(self._fact_rows(keys, as_of), whole=keys is None)

    def contexts(self, keys, as_of=None):
        """Обстановки названных фактов. Тем же кодом, каким их читает срез."""
        return self.situations(list(keys), as_of) if keys else {}

    def slice(self, axes, limit=200, as_of=None):
        """Факты, у которых есть обстановка, сходящаяся по всем названным осям.

        Оси произвольны и комбинируются: «все факты проекта», «все факты
        вторника», «факты вторника и проекта» — это один механизм с разным
        набором осей, а не три готовых выборки.

        AND считается внутри одной обстановки, а не по разным: факт, который во
        вторник видели в одном проекте, а в среду в другом, в совместный срез
        не попадёт. Поэтому срез по двум осям вложен в пересечение срезов по
        каждой из них, но им не равен.
        """
        want = context.norm(axes)
        rows = self._fact_rows(as_of=as_of)
        found = self._situations(rows, whole=True)
        out = []
        for key, record in rows.items():
            if len(out) >= limit:
                break
            if want and not any(context.matches(one, want)
                                for one in found.get(key, [])):
                continue
            out.append({k: v for k, v in record.items() if v not in (None, "")})
        return out

    def injections(self, session_id):
        """Что память отдала в этот разговор, по порядку вставок.

        Спрашивает замер: разбирая, помогла подсказка или нет, надо знать не
        только «вбросили», но и что именно вбросили. Строку с этим держит
        `MemoryInjection`, и вычитывать её мимо этого модуля значило бы завести
        второе место, где память знает про SQL.
        """
        with self.lock:
            rows = self.conn.execute(
                'SELECT * FROM "memoryinjection" WHERE "session_id" = ? '
                'ORDER BY "injected_at"', (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def counts(self):
        out = {}
        with self.lock:
            for object_type in models.OBJECTS:
                out[object_type] = self.conn.execute(
                    'SELECT count(*) FROM "%s"' % _table(object_type)).fetchone()[0]
            out["links"] = self.conn.execute("SELECT count(*) FROM links").fetchone()[0]
            out["raw_text"] = self.conn.execute(
                "SELECT count(*) FROM raw_text").fetchone()[0]
        return out


def _plain(value):
    """SQLite не знает наших типов. Логическое кладём числом, остальное как есть."""
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (str, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False)


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Локальная вторая память",
        epilog="срез: python3 -m storage.db slice --project X --day tuesday")
    ap.add_argument("command", choices=["migrate", "counts", "search", "slice",
                                        "signals"])
    ap.add_argument("query", nargs="?")
    # Оси среза комбинируются: любое подмножество, а не список готовых выборок.
    ap.add_argument("--project")
    ap.add_argument("--dir", dest="working_directory")
    ap.add_argument("--branch", dest="git_branch")
    ap.add_argument("--day", dest="day_of_week")
    ap.add_argument("--hour", dest="hour_of_day", type=int)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()
    if args.command == "signals":
        # Полный список признаков уместности, сделанные и нет вместе.
        print(context.signals.table())
        return
    repo = Repository()
    if args.command == "migrate":
        print("база %s, схема версии %d" % (path(), len(MIGRATIONS)))
    elif args.command == "counts":
        for name, number in repo.counts().items():
            print("%-16s %6d" % (name, number))
    elif args.command == "slice":
        axes = {name: getattr(args, name) for name in context.AXES
                if getattr(args, name, None) is not None}
        print(json.dumps(repo.slice(axes, limit=args.limit),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(repo.search(args.query or ""), ensure_ascii=False, indent=2))
    repo.close()


if __name__ == "__main__":
    main()
