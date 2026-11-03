# 0011. Офісний пакет за замовчуванням: Tinta Office

- Стан: Прийнято (Denys, 2026-11-02)
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
4. **Tinta Office замінює** LibreOffice, Papers, Thunderbird, GNOME Calendar і GNOME Contacts (рішення Denys 2026-11-02). Ці програми не ставляться за замовчуванням; людина може поставити їх сама з Flathub. Рішення спирається на те, що до виходу Vilna Tinta Office буде стабільним: зараз усі п'ять програм в альфі, тож умову перевіряємо перед альфою Vilna, і якщо вона не виконана, повертаємося до цього ADR.

## Наслідки

- **Календар і контакти без програм GNOME.** GNOME Calendar і Contacts показували події й контакти, які створює Gloxi. Тепер це робить Tinta Correo, тож він має бачити ті самі облікові записи: або брати їх з GNOME Online Accounts, або Gloxi і Tinta Correo працюють з одним сервером CalDAV/CardDAV. Також у GNOME Shell є панель подій у годиннику, яка бере дані з Evolution Data Server; без GNOME Calendar вона буде порожня, якщо Tinta Correo не пише туди.

- Gloxi отримує офіс, яким уміє керувати напряму: Tinta Texto, Calculo, Presenta і PDF мають вбудований MCP-сервер (кожна кнопка — команда, яку можна викликати з CLI чи агента). Інструмент «програми» Gloxi може підключатися до нього через Herkos, із тими самими рівнями дозволів, що й для інших MCP-серверів. Це треба описати окремо, коли дійде черга до `vilna-mcp-apps`.
- **Мови.** Vilna має 10 основних мов ([ADR 0009](0009-languages.md)). Tinta Office уже має uk, es, pl, de, fr, it (а також ca, gl, eu), частину — машинним перекладом без вичитки. Бракує ro, cs, lt; до альфи Vilna всі 10 мають бути повні й перечитані людиною.
- **Звіти про помилки.** Tinta Office питає згоду на звіти під час першого запуску кожної програми; у Vilna відповідь має братися з того, що людина сказала Gloxi під час першого запуску системи, щоб не питати шість разів.
- **Що треба зробити в Tinta Office** (запити вже передано проєкту Tinta Office): Flatpak-збірки всіх п'яти програм з ідентифікаторами Tinta і подання на Flathub, перевірка в GNOME/Wayland на Aeon; мови ro, cs, lt; системна згода на звіти про помилки; опис MCP-серверів для Gloxi.
- Поки Tinta Office не на Flathub, Vilna підтримує власне Flatpak-сховище; це додаткова робота для збірки образу (етап 2).

## Інтеграція з системою (рішення Denys 2026-11-03)

Tinta Office відповів на запит Vilna документом [vilna-integration.md](https://github.com/StDenis1980/tintaoffice-web/blob/main/docs/vilna-integration.md) (приватний репозиторій tintaoffice-web, issue #16). Уже зроблено: системна відповідь про звіти (змінна `TINTA_OFFICE_REPORTS`, у Flatpak — через `flatpak override --system --env=…` або `/etc/environment.d/`), позначки `readOnlyHint` на всіх інструментах MCP, `mailto:`/`.ics`/`.vcf`/`.eml` у Tinta Correo і одне вікно на скриньку. Щоб Correo став типовим для цих типів, Vilna прописує його в `/etc/xdg/gnome-mimeapps.list`.

Відповіді Vilna на питання Tinta:

1. **Формат — Flatpak** (див. рішення вище). Tinta Correo отримує `--talk-name=org.gnome.OnlineAccounts`, а для подій у панелі годинника `--filesystem=xdg-config/evolution/sources:create` і `--filesystem=xdg-data/tinta-correo:create`. Herkos має знати про ці дозволи.
2. **Провайдери GNOME Online Accounts** — стандартний набір GNOME 49: Nextcloud (тестовий випадок), Google, Microsoft 365, IMAP і SMTP. Що з цього Correo справді бере, Tinta перевіряє на GNOME 49.
3. **Панель годинника без затримки.** Подія, яку створив Gloxi або людина в Correo, має з'явитися в панелі одразу: Correo після зміни просить Evolution Data Server оновити джерело.

Обмеження, які стосуються Herkos, поки Tinta їх не виправить (запит tintaoffice-web #20): у Tinta Calculo MCP-сервер не вміє обмежувати файли теками, тож його стримує лише пісочниця Flatpak; канал керування Tinta Presenta (`--control`) не має ключа, тож Vilna не запускає Presenta з `--control`. Для Texto, Presenta і PDF Gloxi використовує headless-сервер MCP з коренями читання й запису в теці, яку дозволила людина.

## Розглянуті варіанти

- **RPM-пакети в OBS-проєкті Vilna.** У репозиторіях Tinta уже є опис `.rpm` (nfpm), але це порушує принцип «хост мінімальний, програми — Flatpak»: кожне оновлення офісу йшло б через `transactional-update` і перезавантаження, а Flathub дає Tinta Office й іншим дистрибутивам.
- **Лише LibreOffice.** Зрілий і повний, але Vilna не може ним керувати через MCP, і він не є частиною спільного з Vilna продукту.
- **Tinta Office поряд із LibreOffice, Thunderbird і Papers** до стабільної версії Tinta. Безпечніше, але дві програми на одне завдання плутають людину, а Vilna виходить тоді, коли Tinta вже стабільна.
