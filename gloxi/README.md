# Gloxi

Gloxi («Глоксі», від *glaux* — сова Афіни) — AI-асистент Vilna.

Тут буде:

- системний сервіс Gloxi (працює під окремим користувачем, без прямого доступу до системи);
- маршрутизатор моделей: приватне й просте — локально (мала модель 1–3B), складне — у хмару;
- клієнт MCP, що підключає інструменти;
- окремо — прототип Gloxi як застосунку для звичайного Linux.

Усі дії Gloxi виконує лише через шлюз дозволів Herkos — див. [`../herkos/docs/permissions.md`](../herkos/docs/permissions.md).

## Стан

Каркас Gloxi 0.1 preview (див. [roadmap](../docs/roadmap.md), розд. 2.3–2.4): чат у терміналі з локальною моделлю через `llama-server`. Маршрутизатора, шлюзу приватності, Herkos-lite і вікна GTK ще немає.

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
```

Змінні середовища `GLOXI_LOCAL_URL`, `GLOXI_LOCAL_MODEL`, `GLOXI_LOCAL_TIMEOUT` мають пріоритет над файлом.

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
| `gloxi/chat.py` | Розмова: системна підказка й історія, обрізана до останніх реплік |
| `gloxi/cli.py` | Чат у терміналі (інструмент розробника; вікно GTK використовуватиме ту саму `Conversation`) |
