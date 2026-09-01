# xmemory — механика сервиса по публичной документации + проверка на живом API

Дата: 2026-08-25. CLI `xmemcli 0.0.11` на `work`. API `https://api.xmemory.ai`, MCP `https://mcp.xmemory.ai`.

Проверочный инстанс: `7d7010b4-bb25-4e6a-b0e6-85595cd9f4f0` (`probe-semantics`, объект `Fact`, поля `subject`/`content`, `primary_key: [subject]`).
Боевой `fe1e2af9-…` — только чтение схемы, ничего не писалось.

## Источники

- https://xmemory.ai/integration-overview/ — как устроен сервис
- https://xmemory.ai/api/ — полный REST-справочник (главный источник)
- https://xmemory.ai/xmd/ — формат схемы XMD, семантика `primary_key`
- https://xmemory.ai/mcp/ — MCP-инструменты, внутренности write-пайплайна
- https://xmemory.ai/python/ — SDK, формулировка «translates them into SQL»
- https://xmemory.ai/cli/ — движок предложений «from read traffic»
- https://xmemory.ai/schema-evolution/ — продуктовое описание эволюции схемы
- https://xmemory.ai/pricing-deployment/ — тарифы и квоты
- https://github.com/xmemory-ai/claude-code-plugin — список MCP-инструментов, в т.ч. admin
- https://github.com/xmemory-ai/xmemory_client_py — исходники Python-клиента
- Отдельного `docs.xmemory.ai` нет — документация живёт на `xmemory.ai/<раздел>/`. Sitemap: https://xmemory.ai/sitemap.xml

---

## 1. Чтение

### Что это на самом деле

xmemory — не векторное хранилище. Инстанс — это **реляционная БД**, сгенерированная из XMD-схемы
(объект → таблица, поле → колонка, отношение → junction-таблица). Чтение — **text-to-SQL**.

Прямая цитата из документации Python-SDK (https://xmemory.ai/python/, раздел Reading):

> «Ask questions in natural language. xmemory **translates them into SQL** against the knowledge graph
> and returns a formatted answer.»

Косвенные подтверждения из API-доков: параметр `return_sql`, поле ответа `sql`, режим `raw-tables`
(«Raw SQL results»), а миграции схемы выдают DDL вида `ALTER TABLE person RENAME COLUMN mail TO email`.

**Проверено живьём.** На `probe-semantics`:

| Запрос | `--read-mode raw` вернул |
|---|---|
| «сколько всего фактов сохранено» | `columns: [{name: "count", type: "integer"}], rows: [[4]]` |
| «какая была валюта» | одна колонка `content` (не обе) |
| «что известно про животных» | 1 строка из 4 — «Кот у соседа» |
| «что известно про рептилий» | `reader_result: null` |

Возврат колонки `count` типа `integer`, которой нет в схеме, и переменный набор колонок между
запросами — это доказательство генерации SQL под каждый запрос, а не выборки из индекса.

### Ранжирования нет

Ни в одном документе не встречаются слова *rank*, *relevance*, *score*, *embedding*, *vector*,
*top-k*, *similarity*, *candidates* применительно к чтению (проверено grep'ом по всем скачанным
страницам docs). Модель отбора — **предикат в WHERE**, бинарный: строка либо попала в результат,
либо нет. Поэтому:

- **оценок релевантности API не отдаёт** — их не существует как сущности;
- **«сколько кандидатов рассматривается»** — вопрос неприменим: рассматривается ровно то, что вернул SQL;
- **списка кандидатов со скорингом получить нельзя**. Ближайшее — `raw-tables` (сырой result set) и
  `xresponse` (те же записи в виде объектов с `xuid`).

Семантика при этом всё же работает — но на этапе **сочинения SQL**, а не поиска: LLM видит схему и
формулирует предикат, пользуясь мировым знанием («животные» → искать «кот»). Это же объясняет
провалы: запрос «что известно про тесты в проектах» вернул только строку, где слово «тесты» было
буквально в `content`; записи `tests-alpha`/`tests-beta`, где после апдейта слова «тесты» не осталось,
не нашлись, хотя по смыслу подходили.

### Режимы

| `mode` (API) | `--read-mode` (CLI) | `reader_result` |
|---|---|---|
| `single-answer` (по умолчанию) | `single` | `{"answer": "..."}` — синтезированный текст |
| `raw-tables` | `raw` | `{"columns": [{name,type}], "rows": [[…]]}`; `null`, если ноль строк |
| `xresponse` | `xresponse` | `{"objects": [...], "relations": [...]}` — записи с `identifier` (xuid) и полями |

Общие поля ответа: `sql` (только при `return_sql: true` в HTTP API; MCP и CLI его не отдают —
у `xmemcli --verbose` явно написано «Never shows SQL, diff plans, or other server internals»),
`pending_suggestions`, `trace_id`, `console_url`.

### Scoped reads

`scope: {objects: [{type, key}], relations_scope: "no_relations"|"all_relations"}` — жёстко ограничить
чтение конкретными записями, адресуемыми по `{"xuid": …}` или по пользовательскому первичному ключу.
Всё остальное для чтения невидимо; неизвестный ключ — ошибка, а не молчаливое расширение.

В CLI: `--scope 'Fact:subject=User'`, составной — `'Order:customer=Alice,sku=ABC'`, плюс
`--scope-relations`.

Проверено: `--scope 'Fact:subject=User'` вернул содержимое именно этой записи;
`--scope 'Fact:subject=nope-does-not-exist'` → HTTP 400,
`VALIDATION_ERROR: No 'fact' object matches the provided primary key.`

### Composite questions

Вопрос из нескольких независимых частей сервер сам режет на подвопросы, отвечает на каждый в одном
снапшоте и добавляет в ответ `reader_results` — по записи `{sub_query, reader_result, error}` на часть.
Для односоставного вопроса ключа просто нет (проверять наличие, а не длину). Частичный успех возможен.

---

## 2. Запись

### Пайплайн

Из https://xmemory.ai/mcp/ (раздел `write`, Details):

> «Internally, the server runs a **two-phase pipeline**: an LLM extracts structured objects according
> to your instance's schema, then a **diff engine** compares them against existing data and applies
> inserts, updates, and deletes.»

Отсюда состояния async-записи: `queued → processing → extracting → extracted → applying → completed | failed`.

Извлечением управляют **descriptions в XMD** — они не комментарии, а исполняемая инструкция экстрактору
(«Descriptions control extraction», https://xmemory.ai/xmd/). Три уровня: словарь домена, политика
извлечения (границы объекта, выбор поля, исключения, форматы, решения по enum) и grounded inference
(выводимые, а не буквально написанные значения).

`extraction_logic`: `fast` (по умолчанию) | `deep`. Есть `use_diff_engine` — переопределение
настройки diff-движка инстанса на конкретную запись (только для текстовых записей); что именно
меняется при его отключении, в открытой документации **не описано**.

### Кто назначает первичный ключ

При записи текстом — **экстрактор (LLM)**. Он заполняет все поля объекта, включая те, что входят в
`primary_key`. Никакого отдельного механизма назначения ключа нет: ключ — это обычные поля, просто
перечисленные в `primary_key`.

### Дедупликация и коллизия ключа

`primary_key` — это и есть правило дедупликации. Из https://xmemory.ai/xmd/ («Primary keys and identity»):

> «`primary_key` … determines whether a new mention **updates an existing record or creates a new one**.»

> «**Missing key values are meaningful**: matching uses primary-key values found in the text, and an
> omitted key component is treated as empty. **Repeated writes that omit the same declared key can
> therefore collapse onto the same record and overwrite it.**»

Это ровно описание наблюдаемой проблемы: экстрактор не находит в тексте значения для `subject`,
подставляет generic-заглушку `User`, и все факты садятся на одну строку.

**Поведение при коллизии — проверено живьём, три разных случая:**

| Путь записи | Ключ уже есть | Что происходит |
|---|---|---|
| Текст (`write`, извлечение) | да | **UPDATE**: неключевые поля перезаписываются новыми значениями. Слияния/дописывания нет. |
| `structured_mutations` → `create` | да | **ОТКАЗ**: `{"error": "A 'Fact' with this primary key already exists."}` |
| `structured_mutations` → `update` | да | UPDATE с отчётом `changes.updated[].fields[].old_value/new_value` |

Проверка текстового пути: две записи подряд с `subject: tests-alpha` — вторая заменила `content`
первой (`«Правил тесты в проекте alpha — падала кодировка.»` → `«Второй факт про alpha — тайм-ауты в CI.»`),
строка осталась одна.

Отдельно: `null` в `values` структурного `update` **очищает** поле (а не «оставить как было»).

### Как не схлопывать факты

Документация даёт прямое указание: если надёжного доменного ключа нет — ставить `primary_key: []`.

> «If no safe, referenceable domain key exists, use `primary_key: []`; xmemory will still assign
> internal identifiers and **treat mentions as separate records**. Such objects can participate
> normally in relations.»

То есть отсутствие первичного ключа — не деградация: связи строятся по внутреннему `xuid`, а
искусственный ключ/UUID специально для участия в relation добавлять запрещено («Do not invent a key…»).

### Async

`write_async` → `write_id` → опрос `write_status`. Читать сразу после async-записи нельзя.
`write` (sync) блокируется до коммита — после него read консистентен.

---

## 3. Можно ли задать поля явно — ДА

Это главный ответ. Есть **два независимых механизма**, оба проверены на живом API.

### 3.1. `structured_mutations` — детерминированная запись без LLM

`POST /instances/{id}/write` (и `write_async`) принимает **либо** `text`, **либо**
`structured_mutations` — ровно одно из двух, иначе запрос отклоняется.

> «When you already know exactly what to store, **skip extraction entirely**… Each mutation is applied
> deterministically, **with no LLM involved**, making structured writes fast and exactly repeatable.»

Формат:

```json
{"structured_mutations": [
  {"object_mutation": {"object_type": "Fact",
     "create": {"key": {"subject": "tests-beta"},
                "values": {"content": "…"}}}},
  {"relation_mutation": {"relation_type": "works_at",
     "create": {"endpoints": [
        {"object_name": "person",  "key": {"email": "alice@acme.com"}},
        {"object_name": "company", "key": {"name": "Acme Corp"}}]}}}
]}
```

- `key` — **пользовательские поля первичного ключа**; для `update`/`delete` можно вместо них
  `{"xuid": "..."}`. У `create` `xuid` брать нельзя — он генерируется сервером.
- Объекты: `create` (`key` + `values`), `update` (`key` + `values`, `null` очищает поле), `delete` (`key`).
- Отношения: `create` по `endpoints`, `delete` по `endpoints` (допустим поднабор) или `key`;
  удаление более одной строки требует `"allow_bulk_delete": true`.
- Мутации применяются **по порядку списка**; поздняя может ссылаться на созданный ранее в том же батче объект.
  Совместимые последовательности склеиваются (create+update, update+delete), противоречивые
  (create→delete, что угодно после delete) **отклоняются** с требованием разбить на отдельные записи.
- Ответ — та же форма, что у текстовой записи: `changes.created/updated/deleted` с per-field old/new.
- Может быть **выключено на уровне деплоймента**: тогда ошибка
  «Structured writes are not enabled on this deployment.»

**На вашем деплойменте включено — проверено.** `tools/list` на `mcp.xmemory.ai` через
`xmemcli mcp <instance>` показывает у инструментов `write` и `write_async` параметры
`['scope', 'session_id', 'structured_mutations', 'text']`. Выполненный `create` с
`key={"subject":"tests-beta"}` создал строку ровно с этим ключом; повторный такой же `create`
дал ошибку дубликата; `update` по этому ключу вернул честный diff old/new.

**Где этого нет:** в `xmemcli 0.0.11`. `xmemcli help write` знает только `<text>`, `--extraction`,
`--no-wait`, `--scope`. Структурные записи доступны через HTTP API, Python/TS SDK и MCP-инструмент
`write`/`write_async` — но не через CLI.

### 3.2. `scope` при текстовой записи — привязка к конкретной записи

`--scope 'Fact:subject=User'` (CLI) / `scope: [{type, key}]` (MCP). Из документации MCP:

> «Their current values **steer the extractor to update them instead of duplicating them**, and the
> write is confined to the scope: it may only modify or delete these records and create new ones
> linked to them, so touching anything else fails.»

Требует `extraction fast` и права на чтение (текущие значения показываются экстрактору).
Не комбинируется с `structured_mutations`. Ключ должен существовать — иначе 400.

### 3.3. Обходной путь через CLI без структурных записей

Проверено: если написать текст в форме явного присваивания полей, экстрактор её уважает.

```
xmemcli write "subject: tests-alpha
content: Правил тесты в проекте alpha — падала кодировка."
```

→ создалась строка `subject='tests-alpha'`, а не очередной `User`. Это не документированный контракт,
а наблюдаемое поведение LLM-экстрактора — гарантий нет, в отличие от `structured_mutations`.

### 3.4. XMD-форма записи данных

Записать данные в форме XMD **нельзя** — XMD это язык описания **схемы**, не данных. Структурная
форма данных — только `structured_mutations` (JSON).

---

## 4. Движок предложений схемы

Сигнал — **read-gap**: вопросы, на которые схема ответить не может.

> «When agents ask questions the schema cannot answer, those misses become signal.» (https://xmemory.ai/cli/)

> «Every question your agents ask is direct evidence of what they need, and when a question reaches for
> something the schema doesn't yet hold, that gap is precise signal — captured for free, without ever
> slowing the read down.» (https://xmemory.ai/schema-evolution/)

Блог добавляет второй источник — write-path: «recurring ambiguity, or values that don't quite fit».
В API-справочнике зафиксирован только read-путь (`skip_suggestion_capture` — параметр `/read`).

Что предлагается: конкретные операции миграции — `add_field`, новый объект, новое отношение.
Каждый item несёт `item_fingerprint`, `op`, `rationale` (напр. `"queried but missing"`), `frequency`,
`depends_on`, `evidence_query_samples` (реальные тексты запросов).

Механика — один **скользящий консолидированный proposal на инстанс**, вычисляемый по запросу:
`POST /suggestions/review` → `decide` (accept/reject/defer, батчем) → `apply` (одна миграция).
Конкурентность через токен `proposal_version` (`stale_proposal_version` при рассинхроне).
Счётчик `pending_suggestions` возвращается в каждом ответе `/read`.
`POST /pending-feedback/discard` очищает накопленный backlog (идемпотентно).

Отдельно из блога: **data replay** — при добавлении поля/объекта/отношения история переигрывается
через ту же проверенную экстракцию и новая структура бэкфиллится; переигрывается только затронутая
часть истории. Это противоречит формулировке в API-разделе Limits: «No backfill of historical data
into new fields». Считать блог маркетингом, а Limits — контрактом.

Проверено на probe: `schema suggestions review` вернул пустой proposal
(`proposal_version: "556f631682e19cd3"`, `item_count: 0`) — read'ы по полностью покрытой схеме
сигналов не породили.

Важно: по тарифам **Schema evolution доступна только с плана Team ($499/мес)**.

---

## 5. Лимиты, цены, удаление инстанса

### Тарифы (https://xmemory.ai/pricing-deployment/)

| | Free | Developer | Team | Business |
|---|---|---|---|---|
| Цена/мес | $0 | $49 | $499 | Custom |
| Месячная квота, тыс. токенов | 70 | 200 | 1 000 | 3 000+ |
| Дневная квота (burst), тыс. | 35 | 70 | 200 | 1 000+ |
| **Инстансов** | **5** | **50** | **100** | **1000+** |
| Хранение логов | 1 день | 7 дней | 30 дней | до 1 года |
| Schema evolution | – | – | ✓ | ✓ |
| Object-level RBAC / SSO | – | – | RBAC (soon) | RBAC+SSO (soon) |

Единица тарификации — «xmemory tokens», зависит от схемы и сложности запроса:
**чтение обычно 20–60 токенов, запись 40–120+**.

Деплойменты: SaaS / zero-retention (ваша БД — RDS, Azure, GCP) / on-premise (Docker Compose, свои LLM-ключи).
SOC 2 Type I пройден, Type II в процессе; ISO 27001 в процессе.

### Ошибки квот

- **402 `QUOTA_EXCEEDED`** — квота плана исчерпана. `details.kind` = `daily_quota_exceeded` |
  `monthly_quota_exceeded`, `details.retry_after_seconds` (или `null`, если окно не сбрасывается).
  **Не ретраить** — только после сброса окна.
- **429 `RATE_LIMITED`** — реальный rate limit, ретраить с backoff по `Retry-After`.
- Ветвиться нужно по `errors[0].code`, а не по HTTP-статусу.

### Удаление инстанса

**Есть.** `DELETE /instances/{instance_id}` — «Delete an instance», возвращает список удалённых id
(https://xmemory.ai/api/, раздел Instances).

Также есть MCP-инструменты `admin_delete_instance` / `admin_delete_instance_by_id` на сервере
`xmemory-admin` (помечены «**Destructive** — permanently delete an instance»,
https://github.com/xmemory-ai/claude-code-plugin — раздел Tools).

В `xmemcli 0.0.11` команды удаления **нет** — весь список команд:
write / write-status / read / org list {clusters,instances,keys} / instance {get,create,setup,instructions} /
schema {get,update,dry-run,migrations,suggestions} / xmd {generate,enhance,validate,validate-yaml,validate-json,convert} /
binding {list,add,remove} / context / auth {login,logout,status} / version / status / mcp.

### Прочие лимиты, что нашлись

- `agent_owner_instructions` — max 2000 символов; `agent_engagement_hints` — не больше 16, каждый ≤200 символов.
- `GET /migrations?limit=` — 1..200, по умолчанию 50.
- XMD-имена: `^[A-Za-z][A-Za-z0-9_]*$`, ≤63 символа; префикс `xmemory_` и имя `instance_config` зарезервированы.
- Limits раздела схемной эволюции: нет бэкфилла исторических данных в новые поля; нет отката
  закоммиченной миграции (гарантия — атомарный abort при сбое); ужесточение идентичности
  (`change_object.new_primary_key`, `change_relation.new_keys`) — **только в сторону ослабления**.
  То есть **сделать ключ строже задним числом нельзя** — только расширить/ослабить.

---

## Чего в открытом доступе найти не удалось

1. **Максимальный размер `text` в одной записи** и максимальный размер батча `structured_mutations`
   — числовых лимитов нет ни на одной странице.
2. **Ограничение на число строк в результате чтения** (LIMIT в генерируемом SQL) — не описано.
3. **Что именно делает `use_diff_engine: false`** — параметр документирован одной строкой
   («Override the instance's diff-engine setting for this write»), семантика отключения не раскрыта.
4. **Правила diff-движка при слиянии полей** — сказано только «compares them against existing data and
   applies inserts, updates, and deletes»; как он решает конфликт «в тексте поля нет» vs «в базе есть
   значение» (затирать null'ом или сохранять), не описано и мной не проверялось.
5. **Freshness/conflict policy и TTL.** Обзорная страница обещает «Configure what counts as
   non-conflicting state, how facts should update, and when memories expire with TTL», но ни в
   API-справочнике, ни в XMD, ни в CLI **нет ни одного параметра TTL или conflict policy**. Либо не
   выпущено, либо не документировано.
6. **Разница `fast` vs `deep` extraction** — только названия, никакого описания что меняется
   (модель? число проходов? стоимость?).
7. **Устройство text-to-SQL слоя**: видит ли генератор SQL примеры значений из данных, сколько
   попыток делает, есть ли self-correction при пустом результате. Не документировано; вопрос сервису
   о его собственном алгоритме бесполезен — reader отвечает только по содержимому инстанса.
8. **Object-level RBAC** — помечен «Coming soon», API-поверхности нет.
9. **Числа бенчмарков** (97.10% F1 @ 99.15% precision, 95.2% accuracy) взяты из блога и whitepaper;
   ни методики, ни датасета в открытом доступе не приложено. `github.com/xmemory-ai/datasets`
   существует, но его содержимое не проверялось.

---

## Практический вывод для вашей проблемы

Схема боевого инстанса `fe1e2af9` (прочитана только на чтение):
`Fact` имеет составной `primary_key: [fact_type, subject, scope]`. Экстрактор, не находя в тексте
опоры для `subject`, ставит generic `User` — и при одинаковых `fact_type`/`scope` все такие факты
садятся на одну строку и затирают друг друга. Это ровно тот сценарий, который документация XMD
называет прямым текстом: «Repeated writes that omit the same declared key can therefore collapse
onto the same record and overwrite it».

Три выхода, по убыванию надёжности:

1. **Писать через `structured_mutations`** — HTTP API, Python/TS SDK или MCP-инструмент `write`.
   Ключ задаётся явно, LLM в записи не участвует вообще, результат детерминирован и повторяем.
   На вашем деплойменте включено (проверено). Через `xmemcli` — недоступно.
2. **Сменить `primary_key` на `[]`** для объектов, где каждое упоминание — отдельный факт.
   Внутренний `xuid` продолжит работать, отношения не сломаются. Но: ужесточить ключ обратно потом
   будет нельзя (identity tightening — relax-only), а ослабление проходит.
3. **Переписать `description` полей ключа** так, чтобы экстрактор был обязан выводить конкретное
   значение, и запретить generic-заглушки. Самый дешёвый шаг, но гарантий не даёт — это по-прежнему
   LLM.

Как временный костыль в CLI работает форма `subject: <значение>\ncontent: <текст>` (проверено),
но это наблюдаемое поведение экстрактора, а не контракт.
