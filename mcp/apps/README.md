# vilna-mcp-apps

MCP-сервер «програми» для Gloxi ([ADR 0012](../../docs/adr/0012-recommended-apps-and-install-via-gloxi.md), розд. 3–6): пошук програм на Flathub і у сховищі Vilna, опис програми, список встановлених, встановлення, оновлення і видалення Flatpak-програм. Поруч — малий сервер `vilna-mcp-launcher`, який відкриває встановлену програму.

У Vilna обидва запускає лише системний Herkos (маніфести `herkos/src/herkos/manifests/org.vilna.mcp.apps.toml` і `org.vilna.mcp.launcher.toml`), а не Gloxi. У Herkos-lite (Gloxi як Flatpak на іншому Linux) їх немає: встановлення — дія R3.

| Операція | Рівень | Що робить |
| --- | --- | --- |
| `search` | R0 | Програми з каталогів AppStream Flathub і Vilna; «Рекомендовано Vilna» (`pick`) спершу, потім підтверджені на Flathub |
| `describe` | R0 | Розробник, підтвердження, чи є в добірці, ліцензія, розмір, дозволи Flatpak понад пісочницю (`facts` — рядки для діалогу Herkos) |
| `list_installed` | R0 | Встановлені програми |
| `install` | R3 | Встановити з Flathub чи Vilna; `undo_arguments` — видалити знову |
| `update` | R3 | Оновити одну чи всі програми |
| `uninstall` | R3 | Видалити програму; дані в `~/.var/app` лишаються; `undo_arguments` — поставити знову |
| `restore` | R3 | Скасувати встановлення чи видалення |
| `launch` (`vilna-mcp-launcher`) | R1 | Відкрити встановлену програму в сеансі людини |

Перед кожним `install`, `update` і `uninstall` Herkos сам викликає `describe` з тими самими аргументами і показує людині його `facts` у діалозі (`preview` у маніфесті).

## Чого цей сервер не вміє, навмисно

- Додати джерело програм (Flatpak remote), встановити з файлу `.flatpakref` чи `.flatpak`: таких операцій немає, у маніфесті вони `forbidden`, а правило polkit ([`data/50-vilna-apps.rules`](data/50-vilna-apps.rules)) не дає користувачу `vilna-apps` дій `configure-remote`, `configure` і `install-bundle`.
- Брати програми з будь-якого джерела, крім `flathub` і `vilna`, навіть якщо людина додала інше: те вона ставить сама в «Програмному забезпеченні».
- Видаляти дані програм: це R4 і не його справа.

## Як він працює в образі

`vilna-mcp-apps@<uid>.service` запускає сервер від системного користувача `vilna-apps`, а не від людини і не від Gloxi: встановлення Flatpak для всього комп'ютера йде через `flatpak-system-helper`, який питає polkit, і polkit дозволяє `vilna-apps` лише встановлення, оновлення і видалення з `flathub` і `vilna`. `vilna-mcp-launcher@<uid>.service` працює від людини і просить її менеджер systemd запустити `flatpak run <id>` окремим юнітом, тож програма відкривається у своїй пісочниці Flatpak, а не в пісочниці інструмента.

«Рекомендовано Vilna» сервер читає з `/usr/share/swcatalog/xml/vilna-software-picks.xml` (пакет `vilna-software-picks`) і `/var/lib/swcatalog/xml/` — ті самі файли, з яких GNOME Software показує блок на головній сторінці.

```sh
uv run vilna-mcp-apps --memory       # малий каталог у пам'яті, для тестів і показу
uv run vilna-mcp-apps                # справжній Flatpak (потрібні PyGObject і typelib Flatpak-1.0)
uv run pytest
```

Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
