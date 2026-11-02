# vilna-mcp-backup

MCP-сервер «копії» для Gloxi. Дає асистентові лише дії рівнів R0–R2 ([`backup/docs/design.md`](../../backup/docs/design.md), розд. 5): стан копій, список копій, розміри папок і чи вміщається вибір у ліміт, що і як часто зберігати, «зробити копію зараз», відновлення в нову порожню папку.

Чого тут немає і не буде: налаштування місця, ключ відновлення, відновлення поверх файлів, видалення копій, зміна ключів. Це робить людина сама (`vilna-backup` у терміналі, згодом вікно Herkos).

Сервер не робить копій сам: він запускає програму `vilna-backup --json` ([`backup/`](../../backup/)) окремим процесом. Пароль сховища сервер не бачить: restic бере його зі сховища ключів сам. У Vilna сервер запускає лише Herkos (маніфест `herkos/src/herkos/manifests/org.vilna.mcp.backup.toml`).

```sh
uv run vilna-mcp-backup               # знайде vilna-backup сам
uv run vilna-mcp-backup --command "uv run --project ../../backup vilna-backup"
```

Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
