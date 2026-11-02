# vilna-mcp-files

MCP-сервер «файли» для Gloxi: список, пошук, читання, створення, зміна й видалення текстових файлів у дозволених папках. Перед кожною зміною зберігає резервну копію, тож зміну можна скасувати (`restore`).

У Vilna сервер запускає лише Herkos, а не Gloxi: Herkos вирішує, чи можна виконати дію (маніфест `herkos/src/herkos/manifests/org.vilna.mcp.files.toml`), а сервер ще раз перевіряє, що шлях усередині дозволених папок.

```sh
uv run vilna-mcp-files --root ~/Documents --backup-dir ~/.local/state/herkos/backups/files
```

Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
