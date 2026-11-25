<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Claude для Linux як Flatpak з extra-data

Обгортка, щоб офіційну програму Claude від Anthropic (бета для Linux) можна було поставити на Vilna з магазину, як будь-яку програму Flatpak.

## Як це працює

Anthropic випускає Claude для Linux лише як `.deb` у власному сховищі apt (`downloads.claude.ai/claude-desktop/apt/stable`, amd64 і arm64). Офіційних rpm, Flatpak чи AppImage немає.

Сама програма невільна, і умови Anthropic ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms), розд. 3 і 10) не дають права її поширювати. Тому програми Anthropic **немає ні в цьому репозиторії, ні в пакунку Flatpak, ні у сховищі Vilna**. Пакунок `io.github.stdenis1980.ClaudeDesktop` містить лише наші файли: запуск, `.desktop`, метадані AppStream і скрипт розпакування. Так само на Flathub влаштовані Spotify, Zoom і Chrome.

Під час встановлення на комп'ютері людини:

1. Flatpak завантажує `.deb` прямо з `downloads.claude.ai` (джерело `extra-data` у маніфесті) і звіряє розмір і SHA-256 з тим, що закріплено в маніфесті.
2. `apply_extra.sh` без мережі розпаковує програму в `/app/extra/claude`, прибирає `chrome-sandbox` (у Flatpak його замінює zypak з `org.electronjs.Electron2.BaseApp`) і бере іконку Anthropic з того самого пакета (`/app/extra/export`).
3. `claude-desktop.sh` запускає програму через `zypak-wrapper`, спершу на Wayland.

Хеші беруться з підписаного сховища apt Anthropic, тобто це рівно ті файли, які прийняв би `apt install claude-desktop` на Debian чи Ubuntu:

```sh
python3 tools/claude_desktop.py check            # закріплене має правильний вигляд (без мережі)
python3 tools/claude_desktop.py latest           # чи закріплене — найновіша версія
python3 tools/claude_desktop.py latest --write   # перенести закріплене на найновішу версію
```

`latest` перевіряє підпис `InRelease` ключем Anthropic (відбиток закріплено в `tools/claude_desktop.py`), звіряє `Packages` з `InRelease` і читає звідти найновіший `claude-desktop`.

CI (`.github/workflows/claude-desktop.yml`) щотижня дивиться, чи не вийшла нова версія: Anthropic може прибрати старий `.deb`, і тоді встановлення зламається, доки закріплене не перенесуть.

## Дозволи

`network`, `wayland` (з `fallback-x11`), `dri`, `pulseaudio` (голос), сповіщення, сховище секретів (без нього Electron зберігає вхід відкритим текстом), `login1` на системній шині (сон і пробудження) і тека «Завантаження». Інші файли — лише через портал вибору файлів. Значка в треї немає: у GNOME Vilna немає StatusNotifierWatcher (розширення appindicator), тож закрита програма лишається у «Фонових програмах» GNOME.

Відомі обмеження, які треба перевірити у VM на iapc:

- Claude Code і локальні сервери MCP у програмі запускають команди на комп'ютері; у пісочниці Flatpak вони бачать лише пісочницю. Чи вистачить порталу і `xdg-download`, чи потрібен ширший доступ (і як це показує Herkos), вирішуємо після перевірки.
- Швидкий виклик гарячою клавішею в Anthropic поки надійно працює лише на X11.
- Перевірено у VM на iapc 25.11 (без входу в обліковий запис): вікно на Wayland, `claude://` передається запущеній програмі, «Claude Safe Storage» у зв'язці login, у GNOME Software мітки «Закритий код» і «Потенційно небезпечна». GNOME Software показує розмір встановлення без extra-data (36,8 МБ замість 655 МБ).

## Що ще не зроблено

- **Публікація.** Пакунок не потрапляє у сховище Vilna і не публікується: CI лише збирає його й перевіряє встановлення. Додати його у сховище (`flatpak.yml`) можна лише за окремою згодою Denys. Технічно туди не підійде шлях Tinta через `build-import-bundle`: однофайловий бандл не несе extra-data, тож пакунок треба переносити зі сховища OSTree, яке пише flatpak-builder (наприклад `flatpak build-commit-from`).
- **«Рекомендовано Vilna».** За рішенням Denys від 2026-11-05 у добірці лише вільні програми, тож Claude у `picks.toml` не додано.
- **Назва й іконка.** Умови Anthropic (розд. 12 «Use of our brand») не дозволяють без письмового дозволу вживати її назву, логотипи й торговельні марки так, щоб це натякало на зв'язок чи схвалення. Тому ідентифікатор наш (`io.github.stdenis1980.*`, а не `com.anthropic.*`), а опис прямо каже, що пакунок зробила не Anthropic. Іконку ми не поширюємо: її бере з пакета Anthropic вже комп'ютер людини, тож до встановлення магазин показує програму без неї. Перед публікацією варто попросити дозволу в Anthropic (marketing@anthropic.com).
