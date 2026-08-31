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
import dataclasses, hashlib, json, os, re, sqlite3, threading
from pathlib import Path

from domain import models

DEFAULT_PATH = Path.home() / ".local" / "state" / "memory-encoder" / "memory.db"

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
PRIORITY = {"Fact": 4, "Episode": 3, "Event": 2, "Session": 1}

# Сколько строк каждого вида берётся в выдачу. Приоритет выше был множителем, а
# множитель не гарантирует места: событий в базе 45 тысяч против 466 фактов, и
# десяток длинных команд закрывал собой всю десятку. Квота даёт факту место, но
# событие не запрещает: недобранное одним видом достаётся остальным.
QUOTA = {"Fact": 6, "Episode": 3, "Event": 3, "Session": 1}

WORD = re.compile(r"[\w./:-]{3,}", re.UNICODE)

# Служебные слова вопроса. Без отсева «какие» и «проект» тянут половину базы.
STOP = {"какие", "какой", "какая", "какое", "что", "где", "когда", "кто", "чем",
        "как", "почему", "зачем", "было", "были", "есть", "про", "для", "над",
        "файлы", "файл", "проекте", "проект", "проекта", "шла", "работа",
        "правились", "правился", "the", "what", "which", "was", "were", "did"}


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


MIGRATIONS = (_v1,)


def migrate(conn):
    """Шаги накатываются по номеру версии, записанному в самом файле."""
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS[have:], start=have + 1):
        step(conn)
        conn.execute("PRAGMA user_version = %d" % number)
    conn.commit()
    return len(MIGRATIONS) - have


def connect(where=None):
    target = Path(where) if where else path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Соединение переживает смену потока: адаптер держит один репозиторий на
    # процесс, а замер ходит в него из потоков. Замок ниже сериализует доступ;
    # без этого флага SQLite отказал бы первому же чужому потоку.
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def like(term):
    """Слово в образец для LIKE. `_` и `%` в вопросе — буквы, а не подстановка.

    WORD пропускает подчёркивание в слова, а в LIKE оно совпадает с любым
    одиночным символом: запрос про on_prompt.py находил заодно onXprompt.py.
    """
    for sign in ("\\", "%", "_"):
        term = term.replace(sign, "\\" + sign)
    return "%" + term + "%"


def words(text):
    return [w for w in (m.group(0).lower() for m in WORD.finditer(text or ""))
            if w not in STOP]


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
        # Поля из EARLIEST держат самое раннее из виденного, а не последнее
        # записанное. COALESCE с обеих сторон: пустое не должно выигрывать у
        # заполненного, а MIN(x, NULL) в SQLite это NULL.
        earliest = set(getattr(cls, "EARLIEST", ()))
        updates = ", ".join(
            ('"%s" = MIN(COALESCE("%s", excluded."%s"), COALESCE(excluded."%s", "%s"))'
             % (n, n, n, n, n)) if n in earliest else ('"%s" = excluded."%s"' % (n, n))
            for n in names if n not in cls.KEY)
        keys = ", ".join('"%s"' % n for n in cls.KEY)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s) ON CONFLICT (%s) DO %s' % (
            _table(object_type), cols, holes, keys,
            ("UPDATE SET " + updates) if updates else "NOTHING")
        with self.lock:
            self.conn.execute(sql, [_plain(row[n]) for n in names])

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
                     json.dumps(end["key"], sort_keys=True, ensure_ascii=False)))
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

    def search(self, query, limit=10):
        """Поиск по словам вопроса. Головы у базы нет, есть совпадения.

        Вес складывается по полям: попадание в тему весит больше, чем в текст.
        Строки без единого попадания не возвращаются вовсе — молчание честнее
        случайной строки.
        """
        terms = words(query)
        if not terms:
            return []
        found = []
        for object_type, fields in SEARCH.items():
            names = [name for name, _ in fields]
            where = " OR ".join('"%s" LIKE ? ESCAPE \'\\\'' % name
                                for name in names for _ in terms)
            params = [like(term) for _ in names for term in terms]
            with self.lock:
                rows = self.conn.execute(
                    'SELECT * FROM "%s" WHERE %s LIMIT %d'
                    % (_table(object_type), where, CANDIDATES), params).fetchall()
            for row in rows:
                score = 0
                for name, weight in fields:
                    value = (row[name] or "").lower() if row[name] else ""
                    if not value:
                        continue
                    score += weight * sum(1 for t in terms if t in value)
                if score:
                    found.append((score * PRIORITY.get(object_type, 1),
                                  object_type, dict(row)))
        found.sort(key=lambda item: item[0], reverse=True)
        out, taken = [], {}
        for score, object_type, row in found:
            if len(out) >= limit:
                break
            if taken.get(object_type, 0) >= QUOTA.get(object_type, limit):
                continue
            taken[object_type] = taken.get(object_type, 0) + 1
            record = {k: v for k, v in row.items() if v not in (None, "")}
            record["object_type"] = object_type
            out.append(record)
        # Место, не занятое своим видом, отдаём остальным: квота гарантирует
        # место, а не отнимает его. Иначе выдача выходила бы короче потолка при
        # полной базе.
        if len(out) < limit:
            for score, object_type, row in found:
                if len(out) >= limit:
                    break
                record = {k: v for k, v in row.items() if v not in (None, "")}
                record["object_type"] = object_type
                if any(r == record for r in out):
                    continue
                out.append(record)
        return out

    def neighbours(self, keys, limit=10):
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
            with self.lock:
                found = self.conn.execute(
                    'SELECT * FROM fact WHERE fact_type = ? AND subject = ? '
                    'AND scope = ?', (end.fact_type, end.subject, end.scope)).fetchone()
            if found is None:
                continue        # связь пережила факт: конец есть, строки нет
            record = {k: v for k, v in dict(found).items() if v not in (None, "")}
            record["object_type"] = "Fact"
            out.append((record, weight))
            if len(out) >= limit:
                break
        return out

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
    ap = argparse.ArgumentParser(description="Локальная вторая память")
    ap.add_argument("command", choices=["migrate", "counts", "search"])
    ap.add_argument("query", nargs="?")
    args = ap.parse_args()
    repo = Repository()
    if args.command == "migrate":
        print("база %s, схема версии %d" % (path(), len(MIGRATIONS)))
    elif args.command == "counts":
        for name, number in repo.counts().items():
            print("%-16s %6d" % (name, number))
    else:
        print(json.dumps(repo.search(args.query or ""), ensure_ascii=False, indent=2))
    repo.close()


if __name__ == "__main__":
    main()
