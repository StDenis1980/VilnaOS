<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Devin Desktop (колишній Windsurf) для Linux як Flatpak з extra-data

Обгортка, щоб редактор коду Devin Desktop від Cognition можна було поставити на Vilna з магазину. Зроблено так само, як [Claude](../claude-desktop/README.md) і [Cursor](../cursor/README.md).

## Що це за програма

Windsurf у листопаді 2026 перейменували на Devin Desktop ([оголошення на сторінці завантаження](https://devin.ai/download?os=linux)): той самий редактор на основі VS Code, з агентами Devin. На Flathub його немає. Для Linux Cognition дає архів tar і підписані сховища apt і yum ([інструкція](https://docs.devin.ai/desktop/install.md)). Адреси сховищ досі з назвою `windsurf`, а пакет зветься `devin-desktop`. Сховище apt має лише amd64, тож і пакунок лише для x86_64.

Програма невільна. [Умови Cognition](https://cognition.com/legal/platform-terms-of-service) (розд. 2.3) забороняють копіювати, відтворювати й змінювати її. Тому пакунок `io.github.stdenis1980.DevinDesktop` містить лише наші файли. `.deb` завантажує Flatpak на комп'ютері людини й звіряє з розміром і SHA-256 у маніфесті, а [`../electron-deb/apply_extra.sh`](../electron-deb/apply_extra.sh) розпаковує його.

```sh
python3 tools/devin_desktop.py check
python3 tools/devin_desktop.py latest           # підпис InRelease ключем Cognition, найновіший devin-desktop
python3 tools/devin_desktop.py latest --write
```

`tools/devin_desktop.py` — це `tools/claude_desktop.py` зі сховищем, ключем і пакетом Cognition. Відбиток ключа Cognition ніде окремо не опубліковано, тож його закріплюємо з першого запуску CI, як `apt` (довіра з першого використання), а далі інший ключ — це помилка.

CI: `.github/workflows/devin-desktop.yml`.

## Дозволи

Як у Cursor (рішення Denys 27.11.2026): домашня тека, `/media`, а термінал і агенти працюють через `flatpak-spawn --host` ([`host-shell.sh`](../electron-deb/host-shell.sh)). GNOME Software позначає пакунок як «Потенційно небезпечна».

## Вхід

Пакет реєструє схеми `devin://` і `windsurf://` (`--open-url`), їх узято з `.desktop` пакета 3.15.14; ключ apt і хеш перевірено в CI.

## Що ще не зроблено

- **Публікація, «Рекомендовано Vilna», назва й іконка** — як у [Cursor](../cursor/README.md#що-ще-не-зроблено).
