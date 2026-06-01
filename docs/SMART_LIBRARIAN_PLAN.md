# Smart Librarian — План интеграции FreeLLMAPI

> Ветка: `feature/smart-librarian`
> Цель: авто-тегирование, саммаризация старых логов и умный поиск по архиву
> ИИ-движок: [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) — self-hosted OpenAI-совместимый шлюз-агрегатор (~1.7 млрд токенов/мес)

---

## 0. Что такое FreeLLMAPI (реальная картина из репозитория)

- Это **локальный шлюз** (Node.js / Docker Compose), который поднимается на `http://localhost:3001`.
- Ты добавляешь в его дашборд свои бесплатные ключи провайдеров (Google Gemini, Groq Llama/Qwen, Mistral и т.д. — 16 провайдеров).
- Он отдаёт **единый OpenAI-совместимый эндпоинт** `http://localhost:3001/v1` с одним ключом вида `freellmapi-...`.
- Запрос — стандартный `chat.completions`; `model="auto"` отдаёт выбор модели роутеру.
- Лимиты: дашборд показывает RPM/RPD/TPM/TPD на ключ; суммарно ~1.7B токенов/мес.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:3001/v1", api_key="freellmapi-...")
resp = client.chat.completions.create(model="auto",
        messages=[{"role": "user", "content": "..."}])
```

**Вывод:** для нас это просто HTTP+JSON эндпоинт. Значит можно НЕ тащить зависимость `openai` — см. §2.

---

## 1. Архитектурный обзор

Встраиваемся, **не трогая** рабочее ядро (скрейпер, SHA256-дедуп, импортёры, FTS). Три новых слоя:

```
processor/
  llm_client.py        ← НОВЫЙ: тонкий клиент к FreeLLMAPI (stdlib urllib)
  librarian.py         ← НОВЫЙ: пакетная обработка (теги + саммари), CLI + вызов из сервера
  parse_and_index.py   ← +миграция схемы (новые колонки/таблицы), без изменения импорта
viewer/
  serve.py             ← +эндпоинты /api/librarian/* и /api/ask (паттерн scrape-all)
  index.html           ← +фильтр по тегам, показ саммари, строка «Спросить архив»
processor/
  _test_librarian.py   ← НОВЫЙ: тесты с замоканным LLM (без трат токенов)
librarian_config.json  ← gitignored: base_url + ключ (НЕ коммитим)
```

---

## 2. LLM-клиент — `processor/llm_client.py`  *(архитектурное решение)*

**Решение (принято):** ✅ **Вариант A — тонкая обёртка на stdlib `urllib.request`**, без пакета `openai`.

| Вариант | За | Против |
|---|---|---|
| **A. stdlib `urllib` (рекомендую)** | сохраняет «zero-dependency core» проекта; эндпоинт всё равно OpenAI-совместимый JSON; легко мокать в тестах | ~40 строк своего кода |
| B. пакет `openai` | меньше кода, ретраи «из коробки» | первая внешняя зависимость в ядре; тяжёлый транзитивный граф |

Обёртка инкапсулирует: чтение конфига (env `FREELLMAPI_BASE_URL` / `FREELLMAPI_KEY` → fallback на `librarian_config.json`), POST на `/chat/completions`, разбор ответа, **ретраи с экспоненциальным backoff на 429/5xx** (free-tier провайдеры быстро упираются в RPM/TPM), таймауты, мягкая деградация (нет ключа → клиент «выключен», фича просто не активируется).

```python
# набросок API
class LLMClient:
    def __init__(self, base_url=None, api_key=None, model="auto"): ...
    @property
    def enabled(self) -> bool: ...          # есть ли ключ
    def complete(self, system: str, user: str,
                 *, json_mode=False, max_retries=4) -> str: ...
```

---

## 3. Изменения схемы БД  *(тот же паттерн миграции, что и `source`)*

В `init_db()` после существующего блока миграции — `PRAGMA table_info` + `ALTER`/`CREATE IF NOT EXISTS` (идемпотентно, старые базы доезжают сами):

```sql
-- саммари живёт прямо в conversations
ALTER TABLE conversations ADD COLUMN summary        TEXT;
ALTER TABLE conversations ADD COLUMN summary_model   TEXT;   -- какой моделью
ALTER TABLE conversations ADD COLUMN summarized_at    TEXT;   -- ISO, NULL = не обработан
ALTER TABLE conversations ADD COLUMN tagged_at        TEXT;   -- ISO, NULL = не обработан

-- теги — нормализованно (для фильтра и поиска)
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    tag_id          INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (conversation_id, tag_id)
);
```

Колонки `summarized_at` / `tagged_at` — это **чекпоинты идемпотентности**: библиотекарь пропускает уже обработанные чаты и не тратит токены повторно.

---

## 4. Три под-фичи

### 4.1 Авто-тегирование
- Для каждого необработанного чата: собираем `title + первые/последние N сообщений` (обрезка по бюджету токенов).
- Промпт: «верни 3–6 кратких тематических тегов в JSON-массиве, на языке чата».
- `json_mode` → парсим массив → upsert в `tags` + `conversation_tags` → ставим `tagged_at`.
- Нормализация тегов (lower/trim, схлопывание дублей) на стороне Python.

### 4.2 Саммаризация старых логов
- Выборка: чаты с `summarized_at IS NULL`, по умолчанию — самые старые/длинные первыми (настраивается).
- Промпт: «сжатое саммари 2–4 предложения, тот же язык, без преамбул».
- Пишем в `conversations.summary` + `summary_model` + `summarized_at`.
- В UI: саммари показывается над диалогом и в карточке списка (свернуто).

### 4.3 Умный поиск — «Спросить архив» (RAG-lite)
Поэтапно, чтобы не строить вектор-БД на старте:
- **Фаза 1 (сразу):** существующий **FTS5 достаёт кандидатов** → LLM получает их саммари/фрагменты и (а) переранжирует, (б) отвечает на вопрос со ссылками на конкретные чаты. То есть «спроси у архива» поверх уже готового FTS.
- **Фаза 2 (опционально):** если шлюз отдаёт embeddings-эндпоинт — добавим таблицу эмбеддингов и косинусную близость для семантики. Помечено как отдельная веха, не блокирует фазу 1.

---

## 5. Пакетная обработка и контроль расходов

Переиспользуем **существующий паттерн** `_handle_scrape_all`:
- `processor/librarian.py` — CLI: `python librarian.py --tag --summarize [--limit N] [--log <file>]`, пишет прогресс в `.librarian_log.txt`.
- В `serve.py`: `POST /api/librarian/run` (Popen подпроцесса) + `GET /api/librarian/status` (опрос лога) + `POST /api/librarian/stop`.
- **Идемпотентность:** чекпоинт в БД после каждого чата (`tagged_at`/`summarized_at`) → краш/стоп не теряет прогресс и не пережигает токены.
- **Rate-limit:** backoff на 429/TPM в `llm_client`, обработка чанками, пауза между запросами.

---

## 6. Приватность (критично — это ядро ценности Vault)

- Фича **строго opt-in**: без настроенного ключа библиотекарь не активен, кнопки скрыты.
- В UI и README — явное предупреждение: «содержимое чатов уходит через локальный шлюз во внешних провайдеров».
- `librarian_config.json` и `.librarian_log.txt` → в `.gitignore`. Ключ `freellmapi-...` НИКОГДА не коммитим.
- Никаких личных данных в тестах — мок-клиент.

---

## 7. Интеграция во вьювер (`index.html`)

Зеркалим уже существующие паттерны (фильтр источников, фильтр по году — клиентские):
- **Фильтр по тегам** в сайдбаре (мульти-выбор чипами), как `#source-filter`.
- **Саммари** над диалогом + сворачиваемое в карточке списка.
- Строка **«Спросить архив»** → `POST /api/ask` → ответ + ссылки на чаты.
- i18n: ru/en ключи для новых подписей.

---

## 8. Тестирование

`processor/_test_librarian.py` (стиль существующих `_test_*`):
- Монки-патчим `LLMClient.complete` детерминированной заглушкой → **офлайн, без трат токенов**.
- Проверяем: миграция схемы; тегирование пишет `tags`/`conversation_tags` + `tagged_at`; саммари пишет `summary`+`summarized_at`; повторный прогон идемпотентен (ничего не дублирует, не дёргает LLM).

---

## 9. Порядок реализации

1. `llm_client.py` + мок и юнит-тест клиента.
2. Миграция схемы в `parse_and_index.py` (+ тест миграции).
3. `librarian.py` (теги → саммари) + `_test_librarian.py`.
4. Эндпоинты `serve.py` (`/api/librarian/run|status|stop`).
5. UI: фильтр тегов + показ саммари.
6. Поиск «Спросить архив» (фаза 1, поверх FTS).
7. README: раздел про FreeLLMAPI, приватность, настройку ключа.

Каждый шаг — отдельный коммит; влияния на рабочее ядро (скрейпер/дедуп/импорт) нет.
