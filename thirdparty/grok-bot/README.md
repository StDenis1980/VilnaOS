<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Grok Bot для Linux як Flatpak з extra-data

Обгортка, щоб офіційну програму Grok Bot від xAI можна було поставити на Vilna з магазину. Зроблено так само, як [Claude](../claude-desktop/README.md) і [Cursor](../cursor/README.md).

## Що це за програма

Чат Grok для Linux програми не має, лише сайт grok.com. Grok Bot — агенти xAI, які виконують завдання на комп'ютері у хмарі xAI, навіть коли програму закрито. З листопада 2026 Grok Bot підтримує Linux x64 і ARM64 (`.deb`, `.rpm`, AppImage; [документація xAI](https://docs.x.ai/grok-bot/get-started)). Будує й роздає її Anysphere, яка робить Cursor: той самий обліковий запис Cursor, та сама служба оновлень (`api2.cursor.sh`, назва програми `sand`) і ті самі адреси `downloads.cursor.com`.

Програма невільна. Пакунок `io.github.stdenis1980.GrokBot` містить лише наші файли; `.deb` завантажує Flatpak на комп'ютері людини й звіряє з розміром і SHA-256 у маніфесті, а [`../electron-deb/apply_extra.sh`](../electron-deb/apply_extra.sh) розпаковує його в `/app/extra/app`.

```sh
python3 tools/cursor_apps.py check grok-bot
python3 tools/cursor_apps.py latest --write grok-bot
```

Підписаного сховища apt для Grok Bot ми не знайшли, тож хеш береться з файлу, завантаженого через HTTPS з `downloads.cursor.com`. Якщо Anysphere додасть його в `downloads.cursor.com/aptrepo`, `latest` сам звірить хеш і з підписаним індексом.

## Дозволи

Агенти працюють у хмарі xAI, а не на цьому комп'ютері, тож доступу до системи програмі не треба: мережа, Wayland, GPU, звук, сповіщення, сховище секретів, `login1` і тека «Завантаження». Інші файли — лише через портал вибору файлів.

## Що ще не зроблено

- **Вхід.** Вхід завершується в браузері й повертається в програму посиланням з власною схемою URL. Яку схему реєструє Grok Bot, ми ще не знаємо: CI друкує `.desktop` з пакета, і після цього схему треба додати в `MimeType` нашого `.desktop`.
- **Хеші, публікація, «Рекомендовано Vilna», назва й іконка** — як у [Cursor](../cursor/README.md#що-ще-не-зроблено).
