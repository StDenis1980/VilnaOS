<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Cursor для Linux як Flatpak з extra-data

Обгортка, щоб офіційний редактор коду Cursor від Anysphere можна було поставити на Vilna з магазину, як будь-яку програму Flatpak. Зроблено так само, як [Claude](../claude-desktop/README.md).

## Як це працює

На Flathub Cursor немає. Anysphere випускає для Linux `.deb`, `.rpm` і AppImage (x64 і ARM64), а для amd64 ще й підписане сховище apt (`downloads.cursor.com/aptrepo`).

Cursor — невільна програма, і [умови Cursor](https://cursor.com/terms-of-service) (розд. 1.5) забороняють її відтворювати й змінювати. Тому програми Anysphere **немає ні в цьому репозиторії, ні в пакунку Flatpak, ні у сховищі Vilna**. Пакунок `io.github.stdenis1980.Cursor` містить лише наші файли: запуск, `.desktop`, метадані AppStream і скрипт розпакування.

Під час встановлення на комп'ютері людини:

1. Flatpak завантажує `.deb` прямо з `downloads.cursor.com` (джерело `extra-data`) і звіряє розмір і SHA-256 із закріпленими в маніфесті.
2. [`../electron-deb/apply_extra.sh`](../electron-deb/apply_extra.sh) без мережі розпаковує програму в `/app/extra/app`, прибирає лише `chrome-sandbox` (у Flatpak його замінює zypak) і бере іконку Anysphere з того самого пакета.
3. `cursor.sh` запускає програму через `zypak-wrapper`, спершу на Wayland.

Закріплене оновлює `tools/cursor_apps.py` (спільний з [Grok Bot](../grok-bot/README.md)):

```sh
python3 tools/cursor_apps.py check cursor           # закріплене має правильний вигляд (без мережі)
python3 tools/cursor_apps.py latest cursor          # чи закріплене — найновіша версія
python3 tools/cursor_apps.py latest --write cursor  # перенести закріплене на найновішу версію
```

Версію й адресу `.deb` дає служба оновлень Anysphere (`api2.cursor.sh`), з якої беруть файли й офіційні сторінки завантаження. Хешів вона не дає, тож `latest` завантажує `.deb` через HTTPS і рахує хеш сам. Для amd64 цей хеш ще й має збігтися з підписаним індексом apt Anysphere (відбиток ключа закріплено в `tools/cursor_apps.py`); для ARM64 сховища apt немає, там лише HTTPS.

CI (`.github/workflows/cursor-apps.yml`) щотижня дивиться, чи не вийшла нова версія, збирає обгортку й перевіряє встановлення.

## Дозволи

Редакторові коду потрібні проєкти й інструменти людини, тож доступ такий самий, як у Visual Studio Code на Flathub (рішення Denys 27.11.2026):

- домашня тека, `/media` і `/run/media`;
- `org.freedesktop.Flatpak`: термінал і агент Cursor запускають команди в системі через `flatpak-spawn --host` (`host-shell.sh`, у змінній `SHELL`), з git, компіляторами й іншими програмами людини;
- мережа, Wayland, GPU, звук, сповіщення, сховище секретів, `login1`.

GNOME Software позначає такий пакунок як «Потенційно небезпечна». Це правда: агент Cursor може змінювати файли й запускати команди від імені людини, і Herkos цього не бачить.

## Що ще не зроблено

- **Хеші.** Контейнер, у якому писали пакунок, не бачить `downloads.cursor.com`, тож перше закріплення береться з журналу CI.
- **Публікація.** Як і Claude, пакунок не потрапляє у сховище Vilna: CI лише збирає його й перевіряє встановлення. Додати можна лише за окремою згодою Denys, через OSTree (`flatpak build-commit-from`), бо бандл не несе extra-data.
- **«Рекомендовано Vilna»** — лише вільні програми (ADR 0012), тож Cursor у `picks.toml` немає.
- **Назва й іконка.** Ідентифікатор наш (`io.github.stdenis1980.*`), опис прямо каже, що пакунок зробила не Anysphere; іконку бере з пакета вже комп'ютер людини.
