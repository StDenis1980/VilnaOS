# Встановлення Vilna

Установник Vilna як розмова з Gloxi ([`../image/docs/installer.md`](../image/docs/installer.md), [ADR 0006](../docs/adr/0006-conversational-installer.md)). Поки готовий лише **крок 0, перший екран** (макет 10.12 «Vilna Design v0.1»):

- привітання, знак і назва випуску Blavet на шпалерах Blavet (світлих або темних, як система);
- десять мов [ADR 0009](../docs/adr/0009-languages.md), кожна своєю назвою, без прапорів. Екран одразу перемикається на обрану мову;
- «Інші мови…» — пошук серед усіх UTF-8 локалей системи (`/usr/share/i18n/SUPPORTED`, інакше `locale -a`), назви мовою оригіналу з iso-codes через GnomeDesktop, як у налаштуваннях GNOME. Система й GNOME будуть цією мовою, програми Vilna і Gloxi поки англійською, і екран про це попереджає. У плані `ui.locale` і `ui.translated`;
- «Читач екрана» вмикає Orca так само, як налаштування GNOME;
- «Без асистента» або «Далі з Gloxi».

Екран працює без моделі. Він записує початок плану встановлення (розд. 4) у `$XDG_RUNTIME_DIR/vilna-installer/plan.json` (права `0700` на теку) і завершується:

```json
{"version": 1, "ui": {"language": "uk", "screen_reader": false}, "assistant": true, "confirmed_by_user": false}
```

```
vilna-installer            # на весь екран, мова з LANG
vilna-installer --window --language pl --plan /tmp/plan.json
```

Тексти екрана лежать у `src/vilna_installer/locale/*.po`, усі десять мов разом, бо екран перемикає мову одразу. Переклади, крім української, варто перевірити носіям мови (Weblate). Логіка без GTK — `start.py`, тести — `tests/`.

Далі: розмова з Gloxi (кроки 1–13), MCP-сервер `vilna-mcp-installer`, валідатор плану й екран підтвердження Herkos.
