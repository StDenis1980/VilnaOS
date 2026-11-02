# 0011. Офісний пакет за замовчуванням: Tinta Office

- Стан: Запропоновано
- Дата: 2026-11-02
- Пов'язано: [склад образу](../../image/docs/composition.md) розд. 6, [`packages.yaml`](../../image/packages.yaml), [ADR 0001](0001-licensing.md), [ADR 0009](0009-languages.md)

## Контекст

Vilna має з першого запуску вміти працювати з документами, таблицями, презентаціями, PDF і поштою. У чернетці складу офісом був LibreOffice. Denys хоче, щоб за замовчуванням у Vilna був [Tinta Office](https://tintaoffice.com): окремий проєкт вільного офісного пакета, який теж робить Denys. Це п'ять нативних програм на Rust:

| Програма | Що робить | Код | Ідентифікатор програми |
| --- | --- | --- | --- |
| Tinta Texto | Текстовий редактор: .docx, .odt, експорт у PDF | [StDenis1980/wordcraft](https://github.com/StDenis1980/wordcraft) | `io.github.stdenis1980.tintatexto` |
| Tinta Cálculo | Електронні таблиці: .xlsx | [StDenis1980/gridcraft](https://github.com/StDenis1980/gridcraft) | `io.github.stdenis1980.tintacalculo` |
| Tinta Presenta | Презентації: .pptx | [StDenis1980/deckcraft](https://github.com/StDenis1980/deckcraft) | `io.github.stdenis1980.tintapresenta` |
| Tinta PDF | Читання, анотації, форми, сторінки PDF | [StDenis1980/pdfcraft](https://github.com/StDenis1980/pdfcraft) | `io.github.stdenis1980.tintapdf` |
| Tinta Correo | Пошта (IMAP/SMTP), календар (CalDAV), контакти (CardDAV) | [StDenis1980/ibercorreo](https://github.com/StDenis1980/ibercorreo) | `io.github.stdenis1980.ibercorreo` |

Перші чотири — змінені версії програм ArtCraft (WordCraft, GridCraft, DeckCraft, PdfCraft), Tinta Correo написано окремо. Стан на листопад 2026: тестові збірки для Windows; для Linux у кожному репозиторії вже є опис пакетів (Flatpak-маніфест і `nfpm` для .rpm/.deb), але готових Linux-збірок ще не опубліковано.

## Рішення

1. **Tinta Office входить у програми за замовчуванням Vilna** (розд. 6 складу образу) у тому ж статусі, що й Firefox: ставиться під час першого запуску.
2. **Формат — Flatpak**, як і решта користувацьких програм (принцип 4 складу образу). Мета — опублікувати всі п'ять програм на Flathub під ідентифікаторами `io.github.stdenis1980.*`; тоді вони доступні й на будь-якому іншому Linux. Поки їх там немає, Vilna бере їх із власного Flatpak-сховища, яке збирається з тих самих маніфестів.
3. **Ліцензія сумісна.** Tinta Office поширюється за `MIT OR Apache-2.0`. Це окремі програми поруч із системою під GPL-3.0-or-later (просте об'єднання), тож нічого перевідкривати не треба; до того ж Apache-2.0 сумісна з GPL-3.0. У `packages.yaml` ліцензія записується як є.
4. **Стосунок до LibreOffice, Thunderbird і Papers** — відкрите питання (розд. 8 складу образу). Пропозиція: до стабільної версії Tinta Office LibreOffice лишається поруч, бо Tinta сама поки не радить свої тестові збірки для щоденної роботи і не обіцяє повної сумісності з файлами Office. Перегляд — перед альфою Vilna.

## Наслідки

- Gloxi отримує офіс, яким уміє керувати напряму: Tinta Texto, Cálculo, Presenta і PDF мають вбудований MCP-сервер (кожна кнопка — команда, яку можна викликати з CLI чи агента). Інструмент «програми» Gloxi може підключатися до нього через Herkos, із тими самими рівнями дозволів, що й для інших MCP-серверів. Це треба описати окремо, коли дійде черга до `vilna-mcp-apps`.
- **Мови.** Vilna має 10 основних мов ([ADR 0009](0009-languages.md)), а Tinta Office зараз іде з іспанською, українською й англійською. Щоб бути програмою за замовчуванням до альфи, Tinta має отримати повний переклад цих 10 мов (або через спільний Weblate).
- **Звіти про помилки.** Tinta Office питає згоду на звіти під час першого запуску; у Vilna відповідь має бути та сама, що людина дала Gloxi під час першого запуску, щоб не питати двічі.
- **Що треба зробити в Tinta Office:** Linux-збірки з ідентифікаторами Tinta (зараз маніфести Flatpak названі за ArtCraft і перейменовуються скриптом `tinta-brand.sh`), подання на Flathub, перевірка в GNOME/Wayland на Aeon.
- Поки Tinta Office не на Flathub, Vilna підтримує власне Flatpak-сховище; це додаткова робота для збірки образу (етап 2).

## Розглянуті варіанти

- **RPM-пакети в OBS-проєкті Vilna.** У репозиторіях Tinta уже є опис `.rpm` (nfpm), але це порушує принцип «хост мінімальний, програми — Flatpak»: кожне оновлення офісу йшло б через `transactional-update` і перезавантаження, а Flathub дає Tinta Office й іншим дистрибутивам.
- **Лише LibreOffice.** Зрілий і повний, але Vilna не може ним керувати через MCP, і він не є частиною спільного з Vilna продукту.
