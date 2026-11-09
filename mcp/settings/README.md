# vilna-mcp-settings

MCP-сервер «налаштування» для Gloxi ([ADR 0023](../../docs/adr/0023-gloxi-calendar-settings-hotkey.md)): світла чи темна тема, колір акценту, розмір тексту, нічне світло (увімкнено, розклад, теплота) і «Не турбувати». Змінює лише ключі GSettings зі свого списку (`settings.py`, `KEYS`), з перевіркою типу й меж; «записати будь-який ключ» немає.

Кожна зміна повертає `undo_arguments` — значення до зміни. Herkos записує їх у журнал Mitos, а «Скасувати» викликає `restore` з ними. Зміна або вся, або ніяка: якщо GNOME відмовив на середині, вже записане повертається.

У Vilna сервер запускає лише Herkos (маніфест `herkos/src/herkos/manifests/org.vilna.mcp.settings.toml`), а не Gloxi.

```sh
uv run vilna-mcp-settings            # справжні налаштування через GSettings (потрібен PyGObject)
uv run vilna-mcp-settings --memory   # у пам'яті, для тестів і показу
```

Якщо в середовищі (наприклад, у runtime Flatpak) немає схеми нічного світла з gnome-settings-daemon, `VILNA_MCP_SETTINGS_SCHEMA_DIR` вказує теку зі скомпільованими схемами.

Звук (гучність, вихід) — наступний крок, через PipeWire. Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
