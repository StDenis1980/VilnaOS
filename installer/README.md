# Встановлення Vilna

Установник Vilna як розмова з Gloxi ([`../image/docs/installer.md`](../image/docs/installer.md), [ADR 0006](../docs/adr/0006-conversational-installer.md)). Готові **крок 0, перший екран** (макет 10.12 «Vilna Design v0.1»), і **розмова кроків 1–13** з панеллю плану.

## Крок 0: перший екран


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

## Кроки 1–13: розмова з Gloxi

«Далі з Gloxi» відкриває у тій самій програмі вікно розмови (`chat.py`, логіка без GTK — `conversation.py`): чат ліворуч, план праворуч. Кожен пункт плану має «Змінити», що повертає розмову до цього кроку. Знімки: `design/installer/gloxi-chat-*.png` у файлах проєкту.

- Gloxi — локальна модель `vilna-llm.service` ([ADR 0025](../docs/adr/0025-gloxi-system-service.md)): `http://127.0.0.1:8080/v1`, ключ із `/var/lib/vilna-llm/key` (група `gloxi`). Інше — `VILNA_INSTALLER_MODEL_URL`, `VILNA_INSTALLER_MODEL_KEY_FILE`. Якщо модель ще завантажується (у live-образі `vilna-llm` стартує після входу), вікно пише «Gloxi прокидається…» і чекає до 5 хвилин (`VILNA_INSTALLER_MODEL_WAIT`, секунди); ключ читається щоразу, тож годиться й створений пізніше. Якщо служба впала або час вийшов, вікно каже, що Gloxi не запуститься, і пропонує «Без асистента».
- План заповнює лише MCP-сервер [`vilna-mcp-installer`](../mcp/installer/), який установник запускає сам; там же формат плану і валідатор. Інструмента, що стирає диск чи встановлює, немає.
- Паролі (Wi‑Fi, обліковий запис, фраза диска) — у захищених полях вікна (`Gtk.PasswordEntry`, розширення Vilna перемикає їх на латиницю). У план іде «введено», сам пароль — лише в пам'яті процесу (`chat.Secrets`) для рушія; Wi‑Fi підключається через NetworkManager, пароль — у приватному файлі, не в командному рядку.
- Мітки дисків і назви мереж ідуть до моделі в `<external-data>` як дані.
- Нові рядки — українською й англійською (`locale/uk.po`); вісім інших мов поки бачать англійську, їх додає переклад.

## Крок 12: підсумок і екран Herkos

«Перевірити й підтвердити план» відкриває екран підтвердження Herkos — окремий процес `vilna-installer-confirm` ([`confirm.py`](src/vilna_installer/confirm.py), вікно — `confirm_window.py`; розд. 4а [`installer.md`](../image/docs/installer.md)). Він сам оглядає диски, показує кожну зміну простими словами ([`summary.py`](src/vilna_installer/summary.py), той самий підсумок бачить і звичайний майстер), ключ відновлення зашифрованого диска, який треба ввести назад (останню групу), і кнопку «Стерти й встановити», що оживає через 3 с. Відповідь іде установнику одним рядком JSON; ключ відновлення лишається в пам'яті установника (`vault.Vault`) для рушія. Кроки шифрування для рушія — [`encryption.py`](src/vilna_installer/encryption.py) (LUKS2 через systemd-repart, ключ відновлення, TPM2 або фраза, як в Aeon; розд. 8а).

Код завершення `vilna-installer`: **0** — людина підтвердила план на екрані Herkos (далі рушій встановлення), **3** — «Без асистента» або немає моделі (далі звичайний майстер з тим самим планом), **1** — вікно закрито.

```
vilna-installer --window --demo windows          # синтетичний комп'ютер із Windows і BitLocker
vilna-installer-confirm --window --plan /tmp/plan.json --demo windows   # лише екран Herkos
vilna-installer-chat --window --plan /tmp/plan.json --demo empty   # лише розмова
```

Ще не зроблено: звичайний майстер і редактор розділів, рушій встановлення, що запускає кроки шифрування й записує систему (крок 13 читає його прогрес з `progress.json` поруч із планом), перенесення файлів і відкриття старих зашифрованих томів (бета).

## Пакет OBS

Пакет `vilna-installer` ([`../obs/packages/vilna-installer/`](../obs/packages/vilna-installer/)): команди `vilna-installer` і `vilna-installer-chat`, пункт меню «Встановити Vilna» десятьма мовами (`data/`), значки `vilna-installer`. Він вимагає `vilna-mcp-installer`, `typelib(GnomeDesktop) = 4.0`, `iso-codes` і `vilna-backgrounds`. Ставиться лише в живий образ `Vilna-Live`, де установник запускається сам після автовходу ([`../obs/README.md`](../obs/README.md#живий-образ-з-установником)); у встановленій системі його немає.
