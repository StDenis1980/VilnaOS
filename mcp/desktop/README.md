# vilna-mcp-desktop

Три MCP-сервери Herkos для стільниці GNOME ([ADR 0031](../../docs/adr/0031-herkos-portals-dbus-atspi.md)). Запускає їх лише системний Herkos, від імені людини, і кожен сидить за фільтром D-Bus, зробленим з його маніфесту Herkos (`herkos/src/herkos/manifests/`): виклик, якого немає в маніфесті, шина відкидає, навіть якщо код сервера зламано.

| Програма | Маніфест | Що робить |
| --- | --- | --- |
| `vilna-mcp-desktop` | `org.vilna.mcp.desktop` | заряд батареї (UPower, R0), сповіщення від Gloxi (R1, лише текст: без посилань і кнопок), що грають програвачі й керування ними (MPRIS, R1), заблокувати екран (R1) |
| `vilna-mcp-portals` | `org.vilna.mcp.portals` | що портали Flatpak дозволяють кожній програмі: камера, мікрофон, динаміки, місцезнаходження, сповіщення, фонова робота, знімки екрана. Подивитися — R1, забрати — R2 зі скасуванням, дати — R3 |
| `vilna-mcp-atspi` | `org.vilna.mcp.atspi` | вікна інших програм через доступність (AT-SPI): перелік і читання (R1, зовнішній вміст), натиснути (R3: не скасовується), замінити текст поля (R2 зі скасуванням) |

`vilna-mcp-atspi` ніколи не показує, не читає й не чіпає захищених програм ([`protect.py`](src/vilna_mcp_desktop/protect.py)): безпека й паролі (GNOME Shell з вікнами polkit і екраном блокування, Seahorse, KeePassXC, Bitwarden…), власні вікна Vilna (Herkos, Gloxi, установник, «Копії»), «Налаштування», «Програми», Flatseal, Диски, термінали. Програму впізнає за процесом (файл програми, рядок запуску, id Flatpak із cgroup), а не за назвою, яку вона каже про себе. Текст полів пароля не читає й не змінює, дій із буфером обміну не робить, клавіш і миші не синтезує. Елемент задано id з `read_window` і назвою, яку Gloxi там бачив: якщо вікно змінилося, нічого не робиться. Перед натисканням Herkos показує людині програму, вікно й кнопку з `describe`.

Зміни повертають `undo_arguments` (попереднє значення дозволу чи текст поля), і «Скасувати» в Herkos викликає з ними `restore`.

```sh
uv sync
uv run pytest      # без PyGObject частина тестів пропускається
# Усі тести, з фільтром xdg-dbus-proxy і вікнами GTK 4 під Xvfb (як у CI):
uv venv --python /usr/bin/python3 --system-site-packages && uv sync
VILNA_MCP_DESKTOP_LIVE=1 uv run --no-sync pytest
```

Для живих тестів потрібні `dbus-daemon`, `xdg-dbus-proxy`, `Xvfb`, `at-spi2-core` і PyGObject з GTK 4 та Atspi.

Транспорт — MCP через stdio (JSON-RPC 2.0, одне повідомлення на рядок). Ліцензія Apache-2.0 (див. [`../LICENSE`](../LICENSE)).
