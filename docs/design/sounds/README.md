# Системні звуки Vilna

Стан: план, 21.11.2026. Звуків Vilna ще немає.

## Що є зараз

- GNOME грає звуки теми `freedesktop`: `[org.gnome.desktop.sound] event-sounds=true`, `theme-name='freedesktop'` у [`30_Vilna.gschema.override`](../../../obs/packages/vilna-branding/30_Vilna.gschema.override); пакет `sound-theme-freedesktop` тягне `vilna-branding`.
- `audible-bell=false`: дзвінок терміналу мовчить.
- `canberra-gtk-play` у `patterns-vilna`, тобто звук за назвою події вже можна зіграти з командного рядка.
- Gloxi має голос (ADR 0015) через GStreamer, але не має коротких сигналів («слухаю», «готово», «помилка»).
- У дизайн-системі v0.1 і в брифі розділу про звук немає. «Аудіорежим» сови в матеріалах Дмитра — це вигляд сови, не звук.

## Що зробимо

1. **Своя тема звуків `vilna`** за специфікацією freedesktop Sound Theme: `/usr/share/sounds/vilna/index.theme` з `Inherits=freedesktop`, файли `stereo/<подія>.oga`. Чого в нас немає, бере freedesktop, тож тема працює з першого файлу.
2. **Пакет `vilna-sound-theme`** в `obs/packages/`, а в `30_Vilna.gschema.override` — `theme-name='vilna'`.
3. **Невеликий набір подій** (стандартні назви, щоб їх грали GNOME і програми):

   | Подія | Коли |
   |---|---|
   | `message-new-instant` | нове сповіщення |
   | `bell`, `dialog-warning`, `dialog-error` | попередження, помилка |
   | `complete` | дію завершено |
   | `screen-capture` | знімок екрана |
   | `audio-volume-change` | гучність |
   | `device-added`, `device-removed` | USB |
   | `power-plug`, `power-unplug`, `battery-low` | живлення |
   | `trash-empty` | кошик |
   | `desktop-login` | вхід (за замовчуванням вимкнено, див. питання) |

   Власні події з префіксом `x-vilna-`: `x-vilna-gloxi-listen`, `x-vilna-gloxi-done`, `x-vilna-gloxi-error`, `x-vilna-herkos-consent` (запит дозволу Herkos).
4. **Звуки робимо самі в коді**: скрипт `make_sounds.py` синтезує всі файли детерміновано (як `make_text_logo.py` для логотипа). Тоді ліцензія чиста й наша, а звук легко змінити.
5. **Правила звучання**: одна гама на всі звуки (пентатоніка), 0,2–0,7 с, вхід до 2 с; рівень однаковий і тихий (пік −9 дБ); Ogg Vorbis 48 кГц. Звук ніколи не єдиний сигнал: подія видна й без нього. Gloxi і Herkos поважають «Не турбувати» і вимкнені звуки подій.

## Ліцензії

- Тема `freedesktop` має змішану ліцензію (GPL-2.0-or-later і Creative Commons), і в минулому до її походження були питання. Як запасна тема вона лишається, але свої звуки з неї не беремо.
- Свої звуки — CC-BY-SA-4.0, як інша графіка Vilna (`LICENSES/CC-BY-SA-4.0.txt`). Скрипт — Apache-2.0.
- Чужі записи (наприклад, Freesound) беремо лише з CC0 або CC-BY-4.0, кожен файл з автором і посиланням.

## Кроки

- **З1.** Демо трьох характерів і вибір одного (зараз).
- **З2.** `make_sounds.py`, увесь набір подій, прослуховування.
- **З3.** Пакет `vilna-sound-theme`, `theme-name='vilna'`, сигнали Gloxi і Herkos.
- **З4.** Перевірка в образі у VM: сповіщення, USB, гучність, знімок екрана, Gloxi.
