# vilna-mcp-calendar

MCP-сервер «календар» для Gloxi ([ADR 0023](../../docs/adr/0023-gloxi-calendar-settings-hotkey.md)), **лише читання**: список календарів, події за період і пошук подій. Джерело — сховище календарів GNOME, Evolution Data Server (рішення Denys 2026-11-09): туди потрапляють календарі з «Календаря» GNOME і облікових записів GNOME (Google, Nextcloud, Microsoft, CalDAV).

- **Період рахує сервер**, а не модель: `list_events` бере слово (`today`, `tomorrow`, `this_week`, `next_week`, `this_month`, `next_month`) або дати ISO 8601 (`start`, `end`, до 366 днів). Час — у поясі людини; якщо в події свій пояс, він іде в `event_timezone`.
- Повторювані події розгортає EDS. Межі: до 200 подій за запит (`truncated`), назва й місце до 500 символів, опис до 2000; з одного календаря читається не більше 5000 повторів.
- Показуються лише календарі, увімкнені в GNOME; приховані — тільки якщо їх назвати в `calendars`.
- Усе, що повертає сервер, — чужий текст: Herkos позначає його для Keros (маніфест `herkos/src/herkos/manifests/org.vilna.mcp.calendar.toml`), а шлюз приватності маскує перед хмарою.

```sh
uv run vilna-mcp-calendar          # календарі EDS (потрібні PyGObject і typelib ECal 2.0)
uv run vilna-mcp-calendar --demo   # два вигадані календарі, для тестів і показу
```

Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
