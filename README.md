# AI-оператор для сайта мобильного оператора связи

[![hexlet-check](https://github.com/goper/llm-developer-project-445/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/goper/llm-developer-project-445/actions)

Агентный чат для сайта мобильного оператора. Он отвечает абоненту на вопросы о тарифе, списаниях и правилах со ссылками на базу знаний, а смену тарифа, услуг и контактов проводит через карточку подтверждения. Модель в контуре только предлагает: план проверяет гейт, права — allowlist из JWT и два барьера, подтверждение и лимиты — политики Dogwood, исполнение вызывает код с ключом идемпотентности, каждый исход ложится в журнал.

Учебный проект Хекслета: https://ru.hexlet.io/programs/llm-developer
Как это должно работать: https://files.hexlet.app/a/i3355x

## Стек

Python 3.12, OpenAI Agents SDK, `mcp` (streamable HTTP), FastAPI, PostgreSQL 16 + pgvector, Dogwood (`dogwood-py`), JWT RS256 (PyJWT). Модель — любой OpenAI-совместимый API (LM Studio, облачный провайдер).

## Контур

```text
веб-чат :8100 ── бэкенд с оркестратором :8000 ── LLM (OpenAI-совместимый API)
                     │      │      │
              action-MCP :8220 │  guardrail :8300
                     │   PDP :9100 (политики Dogwood)
                     ▼
   стенд :8100: издатель JWT и JWKS, MCP-серверы чтения (crm, billing, kb), REST-моки, база знаний
                     ▼
   PostgreSQL :5434 (pgvector)
```

| Процесс | Порт | Что делает | Код |
|---|---|---|---|
| `make stand` | 8100 | издатель JWT, MCP чтения, REST-моки, веб-чат | готовый стенд |
| `make api` | 8000 | планировщик → гейт → доменные агенты → редактор, карточки, SSE | `app/` |
| `make actions` | 8220 | action-MCP: `prepare_*`, `execute_action`, `create_ticket`; субъект из JWT | `actions/` |
| `make pdp` | 9100 | PDP: политики «да — что да?» и лимит | `pdp/policies/tariff.dw` |
| `make guardrail` | 8300 | проверка текста input/output, RULE-движок, `/admin/down`, `/admin/up` | `guardrail/service.py` |

## Установка

```bash
git clone https://github.com/goper/llm-developer-project-445.git
cd llm-developer-project-445
python3.12 -m venv .venv && source .venv/bin/activate
make install
cp .env.example .env   # заполнить LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
```

## Запуск

```bash
make infra && make migrate && make seed   # Postgres + схема + демо-данные
make stand        # терминал 1
make api          # терминал 2
make actions      # терминал 3
make pdp          # терминал 4
make guardrail    # терминал 5
```

Веб-чат: http://127.0.0.1:8100 (пользователи `anna`, `boris`, `olga`), страница оператора: http://127.0.0.1:8100/operator.html.

## Проверки

```bash
make smoke        # 54 проверки без модели (группы 0–5); одна группа: make smoke G=4
make scenarios    # матрица диалогов на живой модели (data/dialogs/*.yaml)
make reset        # вернуть демо-данные моков в исходное состояние
```

## Использование

```text
Анна:   почему у меня списание 440 рублей?
Чат:    план: data → kb
        440 ₽ — ежемесячная плата за тариф «Базовый» (источник: tariff_bazovy),
        списывается 1-го числа за месяц вперёд (источник: charge_rules).

Анна:   переведи меня на семейный
Чат:    ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ: смена тарифа · с Базовый на Семейный · 440 ₽/мес → 890 ₽/мес
Анна:   да
Чат:    Готово, операция «смена тарифа» выполнена.
Анна:   да
Чат:    Эта операция уже исполнена — повтор не выполнялся.
```

Разбор хода по журналу: `GET http://127.0.0.1:8000/api/trace/<trace_id>` — события аудита, вызовы модели и инструментов одного хода по времени.

## Как устроены права и защита

- **Агенты по доменам** (`app/workers.py`): `data` — профиль, тариф, списания; `kb` — `search_kb`; `tariff` — только `prepare_*`; `tickets` — `create_ticket`. `execute_action` нет ни у одного агента.
- **Гейт плана** (`app/planner.py`): шаги неизвестных агентов отбрасываются, у агентов чтения снимается `action`.
- **JWT** (`actions/server.py`): сервер действий — OAuth 2.1 resource server; субъект из `get_access_token()`, аргумента `sub` в контрактах нет.
- **Два барьера** (`app/permissions.py`): `visible_tools` при сборке агента, `admit` перед каждым вызовом (`operation_not_allowed:*`, `object_from_arguments:*`).
- **Политика** (`pdp/policies/tariff.dw`): `Prepare` → `ConfirmCard` (formerly Prepare той же карточки) → `Execute` (formerly ConfirmCard), forbid на третью смену тарифа; каждый permit требует `guard_ok`.
- **Идемпотентность** (`app/idempotency.py`): ключ `{action}:{pending_id}`; неудача ключ не занимает.
- **Аудит** (`app/audit.py`): `audit_log`, `llm_calls`, `tool_calls` связаны `trace_id`; текст реплик не пишется.
- **Guardrail** (`guardrail/service.py`): проверка реплики до модели и ответа/карточки до показа; недоступен — чтение работает, действия закрыты (`guard_not_ok`).

---

<details>
<summary>Автоматические тесты Хекслета</summary>

Тесты запускаются на каждый коммит. За запуск отвечает файл `.github/workflows/hexlet-check.yml` — не удаляйте и не переименовывайте ни его, ни репозиторий.

</details>

## О Хекслете

[Хекслет](https://ru.hexlet.io/) — школа программирования: авторские программы обучения с практикой, поддержкой наставников и реальными проектами, которые остаются в резюме. Этот репозиторий — один из таких проектов.
