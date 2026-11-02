# 0011. Офісний пакет за замовчуванням: Tinta Office

- Стан: Запропоновано
- Дата: 2026-11-02
- Пов'язано: [склад образу](../../image/docs/composition.md) розд. 6, [`packages.yaml`](../../image/packages.yaml), [ADR 0001](0001-licensing.md), [ADR 0009](0009-languages.md)

## Контекст

Vilna має з першого запуску вміти працювати з документами, таблицями, презентаціями, PDF і поштою. У чернетці складу офісом був LibreOffice. Denys хоче, щоб за замовчуванням у Vilna був [Tinta Office](https://tintaoffice.com): окремий проєкт вільного офісного пакета, який теж робить Denys. Це п'ять нативних програм на Rust:

| Програма | Що робить | Код | Ідентифікатор програми |
| --- | --- | --- | --- |
| Tinta Texto | Текстовий редактор: .docx, .odt, експорт у PDF | [StDenis1980/wordcraft](https://github.com/StDenis1980/wordcraft) | `io.github.stdenis1980.tintatexto` |
| Tinta Calculo | Електронні таблиці: .xlsx | [StDenis1980/gridcraft](https://github.com/StDenis1980/gridcraft) | `io.github.stdenis1980.tintacalculo` |
| Tinta Presenta | Презентації: .pptx | [StDenis1980/deckcraft](https://github.com/StDenis1980/deckcraft) | `io.github.stdenis1980.tintapresenta` |
| Tinta PDF | Читання, анотації, форми, сторінки PDF | [StDenis1980/pdfcraft](https://github.com/StDenis1980/pdfcraft) | `io.github.stdenis1980.tintapdf` |
| Tinta Correo | Пошта (IMAP/SMTP), календар (CalDAV), контакти (CardDAV) | [StDenis1980/ibercorreo](https://github.com/StDenis1980/ibercorreo) | `io.github.stdenis1980.ibercorreo` |

Перші чотири — змінені версії програм ArtCraft (WordCraft, GridCraft, DeckCraft, PdfCraft), Tinta Correo написано окремо. Стан на листопад 2026: усі п'ять — альфа; публічні тестові збірки є для Windows, тестові .deb/.rpm/AppImage для Linux теж є, але Flatpak під ідентифікаторами Tinta ще немає (маніфести в репозиторіях названі за ArtCraft, у Tinta Correo маніфесту немає).

## Рішення

1. **Tinta Office входить у програми за замовчуванням Vilna** (розд. 6 складу образу) у тому ж статусі, що й Firefox: ставиться під час першого запуску.
2. **Формат — Flatpak**, як і решта користувацьких програм (принцип 4 складу образу). Мета — опублікувати всі п'ять програм на Flathub під ідентифікаторами `io.github.stdenis1980.*`; тоді вони доступні й на будь-якому іншому Linux. Поки їх там немає, Vilna бере їх із власного Flatpak-сховища, яке збирається з тих самих маніфестів.
3. **Ліцензія сумісна.** Tinta Office поширюється за `MIT OR Apache-2.0`. Це окремі програми поруч із системою під GPL-3.0-or-later (просте об'єднання), тож нічого перевідкривати не треба; до того ж Apache-2.0 сумісна з GPL-3.0. У `packages.yaml` ліцензія записується як є.
4. **Стосунок до LibreOffice, Thunderbird і Papers** — відкрите питання (розд. 8 складу образу). Пропозиція: до стабільної версії Tinta Office LibreOffice лишається поруч, бо всі програми Tinta поки в альфі, Tinta сама не радить тестові збірки для щоденної роботи і не обіцяє повної сумісності з файлами Office. Перегляд — перед альфою Vilna.

## Наслідки

- Gloxi отримує офіс, яким уміє керувати напряму: Tinta Texto, Calculo, Presenta і PDF мають вбудований MCP-сервер (кожна кнопка — команда, яку можна викликати з CLI чи агента). Інструмент «програми» Gloxi може підключатися до нього через Herkos, із тими самими рівнями дозволів, що й для інших MCP-серверів. Це треба описати окремо, коли дійде черга до `vilna-mcp-apps`.
- **Мови.** Vilna має 10 основних мов ([ADR 0009](0009-languages.md)). Tinta Office уже має uk, es, pl, de, fr, it (а також ca, gl, eu), частину — машинним перекладом без вичитки. Бракує ro, cs, lt; до альфи Vilna всі 10 мають бути повні й перечитані людиною.
- **Звіти про помилки.** Tinta Office питає згоду на звіти під час першого запуску кожної програми; у Vilna відповідь має братися з того, що людина сказала Gloxi під час першого запуску системи, щоб не питати шість разів.
- **Що треба зробити в Tinta Office** (запити вже передано проєкту Tinta Office): Flatpak-збірки всіх п'яти програм з ідентифікаторами Tinta і подання на Flathub, перевірка в GNOME/Wayland на Aeon; мови ro, cs, lt; системна згода на звіти про помилки; опис MCP-серверів для Gloxi.
- Поки Tinta Office не на Flathub, Vilna підтримує власне Flatpak-сховище; це додаткова робота для збірки образу (етап 2).

## Розглянуті варіанти

- **RPM-пакети в OBS-проєкті Vilna.** У репозиторіях Tinta уже є опис `.rpm` (nfpm), але це порушує принцип «хост мінімальний, програми — Flatpak»: кожне оновлення офісу йшло б через `transactional-update` і перезавантаження, а Flathub дає Tinta Office й іншим дистрибутивам.
- **Лише LibreOffice.** Зрілий і повний, але Vilna не може ним керувати через MCP, і він не є частиною спільного з Vilna продукту.
