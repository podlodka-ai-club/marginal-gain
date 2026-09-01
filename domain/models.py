#!/usr/bin/env python3
"""Семь объектов схемы XMD как питоновские записи.

До сих пор объекты жили только в хранилище, а код собирал их текстом и отдавал
экстрактору. Экстрактор угадывает ключ, и при промахе записи схлопываются на
одну строку. Здесь ключ задан явно, так что запись через структурные мутации
детерминирована: что положили, то и легло.

Схема — версия 8, инстанс «вторая память». Списки значений и обязательность
полей повторяют XMD; расхождение с хранилищем ловится проверкой validate().

Версия 8 добавила срок факта (`Fact.valid_until`) и отложенное (`LapsedFact`).
Версия 9 — свёртку: `Fact.merged_from` на замене и `LapsedFact.merged_into` на
том, что в неё свернули.
Локальная база выводит таблицы отсюда и потому знает их сразу; хранилище за
сетью надо обновить отдельно, иначе структурная запись туда упрётся в поле,
которого та схема не знает.
"""
from dataclasses import dataclass, fields
from typing import ClassVar

from infra.scrub import redact

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
OUTCOMES = ("done", "abandoned", "blocked")
FACT_TYPES = ("user", "preference", "project_state", "external_resource")
SCOPES = ("project", "global")
EVENT_TYPES = ("user_message", "agent_response", "tool_call", "tool_result",
               "artifact_found", "state_change")
CUES = ("same_episode", "same_file", "same_project", "adjacent_in_time", "error_then_fix")
INJECTION_OUTCOMES = ("done", "abandoned", "blocked", "unknown")


class SchemaError(ValueError):
    """Запись не сходится со схемой. Лучше упасть тут, чем затереть строку."""


def _clean(value):
    """Учётные данные не должны уезжать в хранилище ни одним из путей."""
    return redact(value) if isinstance(value, str) else value


@dataclass
class Record:
    """Общая часть: ключ, значения, проверка. Поля объявляют наследники."""

    OBJECT: ClassVar[str] = ""
    KEY: ClassVar[tuple] = ()
    REQUIRED: ClassVar[tuple] = ()
    ENUMS: ClassVar[dict] = {}

    # Поля, которые при повторной записи не двигаются вперёд, а держат самое
    # раннее из виденного. Разговор разложен по нескольким файлам архива, и
    # запись собирается из того куска, что попался в пачку: обычное затирание
    # уводило бы начало сессии на время последнего дописанного события.
    EARLIEST: ClassVar[tuple] = ()

    # Поля, которые движутся только вперёд. Зеркало EARLIEST и по той же
    # причине: срок факта продлевают с разных сторон — разбор архива и
    # обращение, — и режим памяти у них может отличаться. Обычное затирание
    # означало бы, что показ факта под коротким режимом укорачивает жизнь
    # тому, кому долгий срок уже назначили.
    LATEST: ClassVar[tuple] = ()

    def key(self):
        """Поля первичного ключа. По ним хранилище находит строку.

        Учётные данные вычищаются и здесь: подмена детерминирована, поэтому
        строка находится по тому же ключу, каким её записали.
        """
        return {name: _clean(getattr(self, name)) for name in self.KEY}

    def values(self):
        """Всё, кроме ключа. Пустые поля не шлём — они затрут то, что лежит.

        Пустое это и None, и пустая строка. Раньше отсекалось только None, и
        строка, из которой вычистка вынесла всё содержимое, затирала лежащий
        в хранилище факт вместо того, чтобы быть пропущенной.

        Вычистка секретов стоит здесь, а не у вызывающего: текстовый путь
        закрыт `redact` в трёх местах, и структурный не должен стать дырой,
        которую надо помнить (коммит 9c2d7fb — боевой ключ уехал в хранилище).
        """
        return {f.name: _clean(getattr(self, f.name)) for f in fields(self)
                if f.name not in self.KEY and getattr(self, f.name) not in (None, "")}

    def _validate_key(self):
        """Проверка для операций, которые адресуют строку одним ключом."""
        for name in self.KEY:
            if getattr(self, name) in (None, ""):
                raise SchemaError("%s: пустое поле ключа %s" % (self.OBJECT, name))
        self._validate_enums()
        return self

    def _validate_enums(self):
        for name, allowed in self.ENUMS.items():
            got = getattr(self, name)
            if got is not None and got not in allowed:
                raise SchemaError("%s.%s: %r нет в схеме, допустимо %s"
                                  % (self.OBJECT, name, got, ", ".join(allowed)))

    def validate(self):
        """Полная проверка. Нужна там, где запись создаётся целиком."""
        self._validate_key()
        for name in self.REQUIRED:
            if getattr(self, name) is None:
                raise SchemaError("%s: не задано обязательное поле %s" % (self.OBJECT, name))
        return self

    def mutation(self, op="create"):
        """Структурная мутация в форме, которую принимает сервис.

        Удаление адресует строку одним ключом, поэтому обязательные поля с него
        не спрашиваются: иначе пришлось бы выдумывать их или читать запись
        целиком ради того, чтобы её стереть.
        """
        if op not in ("create", "update", "delete"):
            raise SchemaError("неизвестная операция: %s" % op)
        if op == "delete":
            self._validate_key()
            return {"object_mutation": {"object_type": self.OBJECT,
                                        op: {"key": self.key()}}}
        if op == "update":
            # Обновление адресует лежащую строку и несёт только то, что
            # меняется. Спрашивать с него обязательные поля значило бы читать
            # запись целиком ради того, чтобы сдвинуть ей одно значение, —
            # а на горячем пути читателя может и не быть.
            self._validate_key()
            return {"object_mutation": {"object_type": self.OBJECT,
                                        op: {"key": self.key(),
                                             "values": self.values()}}}
        self.validate()
        return {"object_mutation": {"object_type": self.OBJECT,
                                    op: {"key": self.key(), "values": self.values()}}}


@dataclass
class Session(Record):
    session_id: str = ""
    project: str = None
    working_directory: str = None
    git_branch: str = None
    started_at: str = None
    ended_at: str = None
    duration_seconds: float = None
    total_tokens: int = None
    harness_version: str = None

    OBJECT: ClassVar[str] = "Session"
    KEY: ClassVar[tuple] = ("session_id",)
    EARLIEST: ClassVar[tuple] = ("started_at",)
    REQUIRED: ClassVar[tuple] = ("session_id",)


@dataclass
class Episode(Record):
    session_id: str = ""
    episode_number: int = None
    title: str = None
    summary: str = None
    outcome: str = None
    project: str = None
    working_directory: str = None
    git_branch: str = None
    started_at: str = None
    ended_at: str = None
    hour_of_day: int = None
    day_of_week: str = None

    OBJECT: ClassVar[str] = "Episode"
    KEY: ClassVar[tuple] = ("session_id", "episode_number")
    REQUIRED: ClassVar[tuple] = ("session_id", "episode_number", "title", "outcome")
    ENUMS: ClassVar[dict] = {"outcome": OUTCOMES, "day_of_week": DAYS}


@dataclass
class Event(Record):
    session_id: str = ""
    sequence_number: int = None
    event_type: str = None
    content: str = None
    tool_name: str = None
    occurred_at: str = None
    duration_seconds: float = None
    tokens: int = None
    project: str = None
    working_directory: str = None
    git_branch: str = None
    hour_of_day: int = None
    day_of_week: str = None

    OBJECT: ClassVar[str] = "Event"
    KEY: ClassVar[tuple] = ("session_id", "sequence_number")
    REQUIRED: ClassVar[tuple] = ("session_id", "sequence_number", "event_type", "content")
    ENUMS: ClassVar[dict] = {"event_type": EVENT_TYPES, "day_of_week": DAYS}


@dataclass
class Fact(Record):
    fact_type: str = ""
    subject: str = ""
    scope: str = ""
    content: str = None
    project: str = None
    updated_at: str = None
    # До каких пор факт считается верным. Ставит `domain.lifespan.until` по
    # режиму памяти, продлевает обращение. Пустое значение значит «не
    # протухает»: забывать не по чему, и выбрасывать такое молча нельзя.
    valid_until: str = None
    # Подписи записей, которые свернулись в эту. Строками через перевод строки:
    # подпись сама содержит `|`, и разделитель обязан с ней не совпадать.
    # Пустое значит «ничего не сворачивали». Поле стоит на замене, а не только
    # обратной пометкой на отставном: «из чего собрана» спрашивают у той
    # записи, которую видят в выдаче, а не у той, которой в ней нет.
    merged_from: str = None

    OBJECT: ClassVar[str] = "Fact"
    KEY: ClassVar[tuple] = ("fact_type", "subject", "scope")
    LATEST: ClassVar[tuple] = ("valid_until",)
    REQUIRED: ClassVar[tuple] = ("fact_type", "subject", "scope", "content")
    ENUMS: ClassVar[dict] = {"fact_type": FACT_TYPES, "scope": SCOPES}

    def identity(self):
        """Ключ одной строкой — в этой форме его хранит Association."""
        k = self.key()
        return "%s|%s|%s" % (k["fact_type"], k["subject"], k["scope"])

    @classmethod
    def of_identity(cls, key):
        """Обратно из строки в запись. Тема может содержать разделитель.

        Режем с краёв, а не слева направо: вид и охват — закрытые перечисления
        схемы, а тема это свободный текст (у размеченного факта её пишет
        модель, и `|` там вполне реален). Разбор слева уводил хвост темы в
        охват, схема браковала значение, и падал весь проход.
        """
        fact_type, rest = (key.split("|", 1) + [""])[:2]
        subject, _, scope = rest.rpartition("|")
        return cls(fact_type=fact_type, subject=subject, scope=scope)


@dataclass
class LapsedFact(Fact):
    """Факт, у которого вышел срок. Отдельный объект, а не флаг на факте.

    Флаг оставил бы запись в той же выборке, и «первую выдачу» пришлось бы
    отличать порогом на каждом чтении — то есть помнить про срок в каждом
    месте, где память кого-нибудь спрашивают. Отдельное место убирает запись из
    выборки один раз и навсегда, а достать её оттуда можно только нарочно,
    глубоким чтением.

    Поля наследуются от `Fact` целиком и нарочно: считай их здесь заново — они
    разъедутся молча, и переклад начнёт терять колонки, которых в копии нет.
    """

    lapsed_at: str = None
    # Подпись замены, в которую свернули эту запись. Пусто — значит запись
    # выбыла по сроку, а не свернулась: одно отставное место на два повода,
    # и различает их эта пометка. По ней же разворот находит свою группу.
    merged_into: str = None

    OBJECT: ClassVar[str] = "LapsedFact"


@dataclass
class Association(Record):
    source_key: str = ""
    target_key: str = ""
    cue: str = ""
    weight: float = None
    observed_at: str = None
    first_seen_at: str = None

    OBJECT: ClassVar[str] = "Association"
    KEY: ClassVar[tuple] = ("source_key", "target_key", "cue")
    # Когда повод увидели впервые — самое раннее из виденного, а не последнее
    # записанное: связь накапливается проходами, и «впервые» назад не двигается.
    EARLIEST: ClassVar[tuple] = ("first_seen_at",)
    REQUIRED: ClassVar[tuple] = ("source_key", "target_key", "cue", "weight")
    ENUMS: ClassVar[dict] = {"cue": CUES}


@dataclass
class MemoryInjection(Record):
    session_id: str = ""
    injected_at: str = ""
    injected_content: str = None
    helped: bool = None
    session_outcome: str = None
    notes: str = None

    OBJECT: ClassVar[str] = "MemoryInjection"
    KEY: ClassVar[tuple] = ("session_id", "injected_at")
    REQUIRED: ClassVar[tuple] = ("session_id", "injected_at")
    ENUMS: ClassVar[dict] = {"session_outcome": INJECTION_OUTCOMES}


OBJECTS = {c.OBJECT: c for c in
           (Session, Episode, Event, Fact, LapsedFact, Association, MemoryInjection)}

# Связи схемы: имя -> роль в связи -> объект на этом конце.
RELATIONS = {
    "session_episodes": {"session": "Session", "episode": "Episode"},
    "session_events": {"session": "Session", "event": "Event"},
    "episode_events": {"episode": "Episode", "event": "Event"},
    "episode_facts": {"episode": "Episode", "fact": "Fact"},
    "injection_target_session": {"memory_injection": "MemoryInjection", "session": "Session"},
    "injection_source_episode": {"memory_injection": "MemoryInjection", "episode": "Episode"},
    "injection_source_fact": {"memory_injection": "MemoryInjection", "fact": "Fact"},
    "association_fact_link": {"association": "Association",
                              "source_fact": "Fact", "target_fact": "Fact"},
}


def link(relation, **ends):
    """Мутация связи. Роли и типы концов сверяются со схемой до отправки.

    Конец связи — ссылка по первичному ключу, поэтому обязательные поля
    объектов здесь не спрашиваются: связывают уже существующие строки.
    """
    roles = RELATIONS.get(relation)
    if roles is None:
        raise SchemaError("нет такой связи: %s" % relation)
    if set(ends) != set(roles):
        raise SchemaError("связь %s ждёт концы %s, получила %s"
                          % (relation, ", ".join(sorted(roles)), ", ".join(sorted(ends))))
    endpoints = []
    for role, record in ends.items():
        if record.OBJECT != roles[role]:
            raise SchemaError("связь %s, конец %s: ждали %s, получили %s"
                              % (relation, role, roles[role], record.OBJECT))
        record._validate_key()
        endpoints.append({"object_name": role, "key": record.key()})
    return {"relation_mutation": {"relation_type": relation, "create": {"endpoints": endpoints}}}
