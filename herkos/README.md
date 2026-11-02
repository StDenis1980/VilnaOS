# Herkos

Herkos (гомерівська огорожа двору; внутрішня назва компонента) — шар безпеки Vilna. Через нього проходить кожна дія асистента Gloxi.

Складові:

- **Шлюз дозволів** («Монемвасія» — один вхід): єдина точка, де вирішується, чи можна виконати дію.
- **Keros** — фільтр prompt injection.
- **Mitos** — журнал дій і відкат через знімки btrfs.
- **Шлюз приватності** — псевдонімізація чутливих даних на пристрої перед відправкою в хмару.

Документи:

- Модель дозволів: [`docs/permissions.md`](docs/permissions.md) (чернетка v0.1).
- Пристрої людини: підключення, перелік і блокування: [`docs/devices.md`](docs/devices.md) (чернетка v0.1).
- Протокол Gloxi ↔ Herkos, журнал і формат маніфестів: [`docs/protocol.md`](docs/protocol.md) (v0.1).

## Herkos-lite (Gloxi 0.1 preview)

Python-реалізація шлюзу дозволів для сеансу користувача (ADR 0003, 0005). Залежностей під час роботи немає, лише стандартна бібліотека.

Стан: шлюз дозволів (рівні R0–R2, маніфести, модифікатори ризику), дозволи на клас дій, журнал Mitos з ланцюжком хешів, сокет JSON-RPC, MCP-клієнт і інструмент «файли» ([`../mcp/files/`](../mcp/files/)) зі скасуванням змін (`herkos undo`). Є також вікно підтвердження GTK (`herkos.dialog`) і клієнт для Gloxi (`herkos.client`). Gloxi вже викликає інструмент «файли» через цей клієнт (`gloxi/src/gloxi/tools.py`). Якщо встановлено [`../mcp/backup/`](../mcp/backup/), Herkos запускає й інструмент «копії» (ADR 0007). Ще немає: Keros. Herkos-lite працює під тим самим користувачем, що й Gloxi, і **не дає гарантій ізоляції** системного Herkos.

```sh
cd herkos
uv sync
uv run pytest
uv run herkos serve --confirm terminal   # питає дозволу в цьому терміналі
uv run herkos journal verify
uv run herkos permissions list
uv run herkos undo <action_id>           # скасувати зміну (id — з журналу)
```

Налаштування: `~/.config/herkos/config.toml` (див. [`src/herkos/config.py`](src/herkos/config.py)): `allowed_roots` (типово `~/Documents`), `bulk_threshold`, `confirm_timeout`.
