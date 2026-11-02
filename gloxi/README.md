# Gloxi

Gloxi («Глоксі», від *glaux* — сова Афіни) — AI-асистент Vilna.

Тут буде:

- системний сервіс Gloxi (працює під окремим користувачем, без прямого доступу до системи);
- маршрутизатор моделей: приватне й просте — локально (мала модель 1–3B), складне — у хмару;
- клієнт MCP, що підключає інструменти;
- окремо — прототип Gloxi як застосунку для звичайного Linux.

Усі дії Gloxi виконує лише через шлюз дозволів Herkos — див. [`../herkos/docs/permissions.md`](../herkos/docs/permissions.md).

## Стан

Каркас Gloxi 0.1 preview (див. [roadmap](../docs/roadmap.md), розд. 2.3–2.4): чат у терміналі з локальною моделлю через `llama-server` і маршрутизатор «локально / хмара за ключем користувача». Шлюзу приватності, Herkos-lite і вікна GTK ще немає, тому **хмара поки отримує повідомлення без маскування**, і Gloxi про це попереджає.

## Запуск для розробки

Потрібні Python ≥ 3.11 і [uv](https://docs.astral.sh/uv/).

1. Запустіть локальну модель через [llama.cpp](https://github.com/ggml-org/llama.cpp) ([ADR 0004](../docs/adr/0004-local-model-engine.md)). Підійде будь-яка мала instruct-модель у форматі GGUF; конкретну модель за замовчуванням оберемо на замірах:

   ```sh
   llama-server -m ~/models/model-q4_k_m.gguf --port 8080
   ```

2. Поговоріть з Gloxi:

   ```sh
   cd gloxi
   uv sync
   uv run gloxi            # інтерактивний чат: /reset, /exit
   uv run gloxi ask "Що вмієш?"
   ```

Gloxi говорить з моделлю лише через OpenAI-сумісний API, тож замість `llama-server` можна вказати Ollama чи інший сервер: `--url http://127.0.0.1:11434/v1 --model qwen2.5:1.5b` або змінна `GLOXI_LOCAL_URL`.

### Налаштування

`~/.config/gloxi/config.toml` (необов'язково):

```toml
[local]
url = "http://127.0.0.1:8080/v1"
model = "local"
timeout = 120

[cloud]                      # необов'язково: свій провайдер і свій ключ
url = "https://api.mistral.ai/v1"
model = "mistral-small-latest"
# api_key = "..."            # краще змінна GLOXI_CLOUD_API_KEY

[router]
mode = "auto"                # auto | local | cloud
cloud_allowed = false        # хмара вимкнена, доки користувач її не ввімкне
long_message_chars = 1500    # auto: довші повідомлення йдуть у хмару
```

Змінні середовища мають пріоритет над файлом: `GLOXI_LOCAL_URL`, `GLOXI_LOCAL_MODEL`, `GLOXI_LOCAL_TIMEOUT`, `GLOXI_CLOUD_URL`, `GLOXI_CLOUD_MODEL`, `GLOXI_CLOUD_API_KEY`, `GLOXI_CLOUD_ALLOWED`, `GLOXI_ROUTER_MODE`.

### Маршрутизатор

1. Без `cloud_allowed = true` і налаштованого `[cloud]` нічого не залишає комп'ютер.
2. `/local <текст>` або `/cloud <текст>` обирає модель для одного повідомлення.
3. `mode = "local"` або `"cloud"` (чи `--mode`) закріплює модель для всіх повідомлень.
4. `mode = "auto"`: прості повідомлення — локально, довші за `long_message_chars` — у хмару. Це стартове правило, його уточнимо на замірах.

Якщо локальна модель не відповідає, Gloxi **не** переходить у хмару сам, а показує помилку. Перед кожною відповіддю видно, куди пішло повідомлення: `[local]` або `[cloud: api.mistral.ai, not masked]`.

### Тести і перевірки

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Тести не потребують справжньої моделі: `tests/fake_server.py` імітує API `llama-server`.

## Структура

| Модуль | Що робить |
| --- | --- |
| `gloxi/config.py` | Налаштування: типові значення, файл TOML, змінні середовища |
| `gloxi/llm.py` | Клієнт OpenAI-сумісного API (звичайна і потокова відповідь) — спільний для локальної моделі й хмари |
| `gloxi/router.py` | Маршрутизатор: куди йде повідомлення (локально чи в хмару) і чому; місце для шлюзу приватності перед хмарою |
| `gloxi/chat.py` | Розмова: системна підказка й історія, обрізана до останніх реплік |
| `gloxi/cli.py` | Чат у терміналі (інструмент розробника; вікно GTK використовуватиме ту саму `Conversation`) |
