# Структура і склад образу Vilna

Стан: **чернетка v0.3** (2 листопада 2026). Документ визначає, з яких шарів складається Vilna, що ми беремо з openSUSE Aeon без змін, що замінюємо і що додаємо. Машиночитний список пакетів — [`../packages.yaml`](../packages.yaml).

Збірка образу запланована на етап 2 дорожньої карти (квітень–вересень 2027, див. [`../../docs/roadmap.md`](../../docs/roadmap.md)). Склад визначаємо вже зараз, паралельно з Gloxi 0.1, щоб компоненти Gloxi й Herkos одразу писалися під реальну систему.

Склад Aeon взято з `patterns-aeon.spec` (джерело: [src.opensuse.org/rpm/patterns-aeon](https://src.opensuse.org/rpm/patterns-aeon)). Aeon — rolling-дистрибутив на базі Tumbleweed, тому список треба звіряти з актуальною версією перед кожною збіркою.

## 1. Принципи

1. **Vilna = Aeon + тонкий шар Vilna.** Беремо патерн `aeon_base` цілком і додаємо свій патерн `vilna_base`. Чим менше ми змінюємо Aeon, тим менше роботи при кожному оновленні Tumbleweed. Для одного розробника це головне правило.
2. **Нічого не видаляємо з Aeon без причини.** Видалення пакета з базового патерна означає вічну підтримку розбіжності. Видаляємо лише те, що конфліктує з Vilna або порушує її обіцянки (приватність, безпека).
3. **Замінюємо лише брендування.** Логотипи, назва ОС, шпалери, тема входу — через стандартні пакети `*-branding-*`, як це робить сам openSUSE.
4. **Хост мінімальний, програми — Flatpak.** Як і в Aeon: у системному образі лише те, що мусить бути частиною ОС. Користувацькі програми ставляться як Flatpak, інструменти розробника — у Distrobox.
5. **Компоненти Vilna — звичайні RPM-пакети** в нашому проєкті Open Build Service. Їх можна встановити й прибрати окремо, і вони оновлюються через `transactional-update` разом із системою.
6. **Мала модель — в образі.** Ваги локальної моделі за замовчуванням (1–3B, ≈1–2 ГБ) входять в ISO і систему, бо Gloxi веде встановлення без мережі ([ADR 0006](../../docs/adr/0006-conversational-installer.md), [`installer.md`](installer.md)). Більші моделі завантажуються за бажанням.

## 2. Шари системи

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 6. Дані користувача: /home (окремий підтом btrfs, знімки Mitos)          │
├──────────────────────────────────────────────────────────────────────────┤
│ 5. Програми: Flatpak (Flathub) + Distrobox для інструментів              │
├──────────────────────────────────────────────────────────────────────────┤
│ 4. Шар Vilna (наші RPM):                                                 │
│    Herkos · Gloxi (системний сервіс + клієнт сеансу) · шлюз приватності  │
│    MCP-сервери · рушій локальної моделі · брендування · перший запуск   │
├──────────────────────────────────────────────────────────────────────────┤
│ 3. Робоче середовище: GNOME (Wayland), portals, AT-SPI, GNOME Online     │
│    Accounts, Nautilus, gnome-software з Flatpak                          │
├──────────────────────────────────────────────────────────────────────────┤
│ 2. База Aeon: systemd, NetworkManager, PipeWire, CUPS, SELinux, polkit,  │
│    snapper, transactional-update, zypper, podman                         │
├──────────────────────────────────────────────────────────────────────────┤
│ 1. Завантаження: shim + systemd-boot, шифрування диска з TPM2, ядро      │
│    kernel-default (openSUSE), прошивки                                   │
└──────────────────────────────────────────────────────────────────────────┘
       Шари 1–4 — незмінна коренева система (btrfs, лише читання)
```

## 3. Що беремо з Aeon без змін

Групи взято з коментарів у `patterns-aeon.spec`. Колонка «Навіщо Vilna» показує, на що з цього спираються Gloxi та Herkos.

| Група Aeon | Основне | Навіщо Vilna |
| --- | --- | --- |
| bootloader | shim, systemd-boot (`sdbootutil`), `dracut-pcr-signature`, efibootmgr | Secure Boot і шифрування диска з TPM2 з коробки |
| base | systemd, NetworkManager (Wi‑Fi, Bluetooth), sudo, chrony, curl | NetworkManager — інструмент Herkos для мережевих дій R3 |
| base_zypper | `transactional-update`, zypper | Атомарні оновлення і встановлення наших RPM |
| snapper, btrfsmaintenance | знімки btrfs | Основа відкату Mitos |
| selinux | `selinux-policy-targeted`, policycoreutils, container-selinux | Aeon працює з **SELinux enforcing**. Herkos і Gloxi потребують власного модуля політики (розд. 5) |
| defaults | audit, systemd-coredump | Журнал аудиту ядра доповнює журнал Mitos |
| desktop-common | PipeWire, CUPS і драйвери принтерів, sane, udisks2, upower, `xdg-desktop-portal-gtk`, `kernel-firmware-all`, `sof-firmware` | Залізо «просто працює»; udisks2 і upower — інструменти для R0-запитів |
| desktop-gnome | GNOME Shell (Wayland), gdm, Nautilus, gnome-software, flatpak, `xdg-desktop-portal-gnome`, `at-spi2-core`, `polkit-gnome`, `gnome-online-accounts`, `gnome-keyring`, `power-profiles-daemon`, шрифти Noto/DejaVu/Source | Portals і AT-SPI — інтерфейси Herkos (розд. 9 permissions.md); GNOME Online Accounts — джерело календаря й пошти для MCP-серверів |
| x11 | xorg-x11-server, libinput | Сумісність для старих програм (XWayland) |
| systemd-zram | `systemd-zram-service` | Важливо для 8 ГБ RAM з локальною моделлю |
| Containers | podman, distrobox | Інструменти розробника поза незмінною системою |
| TPM2 | tpm2.0-tools, tss | Шифрування диска; у майбутньому — захист ключів журналу Mitos |
| Firstboot | ignition, combustion, `gnome-initial-setup` | Перший запуск; додаємо сторінки Vilna (розд. 4.6) |
| Virtualisation | spice-vdagent, qemu-guest-agent | Тестування образу у віртуальних машинах |

Мінімальне залізо Vilna (Intel Core 8-го покоління / AMD Ryzen 2000) підтримує набір інструкцій x86-64-v3 (AVX2) і має вбудований TPM2 (Intel PTT / AMD fTPM), тож оптимізовані збірки Aeon і шифрування з TPM2 на ньому працюють. Перевірити на реальному ноутбуці на етапі 2.

## 4. Що додає Vilna

Усі пакети нижче — наші, збираються в OBS-проєкті Vilna. Ліцензії — за [`../../LICENSING.md`](../../LICENSING.md).

### 4.1. Патерн і реліз

| Пакет | Що робить | Ліцензія |
| --- | --- | --- |
| `patterns-vilna` | Патерн `vilna_base`: вимагає `aeon_base` (з заміною брендування) і всі пакети Vilna | GPL-3.0-or-later |
| `Vilna-release` | `/etc/os-release` (`NAME="Vilna"`, `ID=vilna`, `ID_LIKE="opensuse-tumbleweed opensuse suse"`), адреси оновлень і підтримки | GPL-3.0-or-later |

### 4.2. Herkos

| Пакет | Що робить |
| --- | --- |
| `herkos` | Сервіс шлюзу дозволів (systemd-юніт, окремий системний користувач `herkos`), Keros, Mitos |
| `herkos-polkit` | Дії `org.vilna.herkos.*` і правила `/etc/polkit-1/rules.d/`, що забороняють користувачу `gloxi` прямі системні дії (розд. 9.2 permissions.md) |
| `herkos-selinux` | Модуль SELinux: домени `herkos_t` і `gloxi_t`, що Gloxi може говорити лише з сокетом Herkos |
| `herkos-dialog` | Діалог підтвердження в сеансі GNOME (окремий процес, приймає лише реальне введення) |
| `herkos-manifests` | Маніфести для інструментів, D-Bus-інтерфейсів GNOME і MCP-серверів |

### 4.3. Gloxi

| Пакет | Що робить |
| --- | --- |
| `gloxi` | Системний сервіс асистента (окремий користувач `gloxi`, обмеження systemd з розд. 9.1 permissions.md), маршрутизатор моделей |
| `gloxi-session` | Клієнт у сеансі користувача: вікно чату, гаряча клавіша, сповіщення. У Vilna — **RPM** (рішення 2.11.2026), бо говорить із системним сокетом Herkos; для звичайного Linux той самий код іде як Flatpak |
| `gloxi-gnome-shell-extension` | Кнопка і швидкий виклик Gloxi з панелі GNOME (за бажанням, пізніше) |

### 4.4. Шлюз приватності, MCP-сервери, модель

| Пакет | Що робить | Ліцензія |
| --- | --- | --- |
| `vilna-privacy-gateway` | Бібліотека шлюзу приватності | Apache-2.0 |
| `vilna-mcp-files` | MCP-сервер «файли» | Apache-2.0 |
| `vilna-mcp-settings` | MCP-сервер «налаштування GNOME» (через GSettings / D-Bus) | Apache-2.0 |
| `vilna-mcp-calendar` | MCP-сервер «календар» (CalDAV / GNOME Online Accounts) | Apache-2.0 |
| `vilna-mcp-apps` | MCP-сервер «програми» (запуск, встановлення Flatpak через portal) | Apache-2.0 |
| `vilna-mcp-mail` | MCP-сервер «пошта» (IMAP / SMTP) | Apache-2.0 |
| `llama.cpp` (`llama-server`) | Рушій локальної моделі. Перевірити, чи є актуальний пакет у Tumbleweed; якщо ні — збираємо самі | MIT (upstream) |
| `vilna-model-default` | Ваги моделі за замовчуванням (1–3B, GGUF, ≈1–2 ГБ) у `/usr/share/vilna/models`; оновлюється рідко | Ліцензія моделі |
| `vilna-model-fetch` | Завантаження більших моделей (7–8B) за бажанням, перевірка підпису й хешу | GPL-3.0-or-later |

Ліцензія моделі за замовчуванням має дозволяти її **розповсюдження в складі ОС**, зокрема комерційне. Це умова вибору моделі.

### 4.5. Брендування (замість пакетів Aeon)

Замінюємо пакети Aeon з `branding` у назві на власні з тими самими `Provides`. Це стандартний механізм openSUSE, він не вимагає змін у решті пакетів.

| Пакет Aeon | Пакет Vilna | Що містить |
| --- | --- | --- |
| `Aeon-release` | `Vilna-release` | Назва й версія ОС |
| `branding-openSUSE`, `distribution-logos-openSUSE-Aeon` | `branding-Vilna`, `distribution-logos-Vilna` | Логотип (коло-мазок з ластівкою), іконки, зокрема варіант для 16–32 px |
| `gdm-branding-Aeon`, `gnome-branding-Aeon` | `gdm-branding-Vilna`, `gnome-branding-Vilna` | Екран входу, налаштування GNOME за замовчуванням, шпалери |
| `systemd-presets-branding-Aeon` | `systemd-presets-branding-Vilna` | Які служби ввімкнені: додаємо `herkos`, `gloxi` |
| `systemd-repart-branding-Aeon`, `x86_64_v3-branding-Aeon` | відповідні `*-Vilna` | Розмітка диска і оптимізовані збірки — копії Aeon зі зміненою назвою |
| `gtk*-branding-openSUSE`, `hicolor-icon-theme-branding-openSUSE`, `systemd-icon-branding-openSUSE` | `*-branding-Vilna` | Теми й іконки |
| — | `vilna-wallpapers` | Шпалери, зокрема петриківський розпис (після договорів з майстрами) |

#### Правила торгових марок openSUSE

Перевірено 2.11.2026 за [openSUSE:Trademark guidelines](https://en.opensuse.org/openSUSE:Trademark_guidelines). Що вони вимагають від похідного дистрибутива, який змінює openSUSE:

- **Прибрати всі знаки openSUSE** з образу: назву, логотипи, брендування. Кожен пакет, назва якого закінчується на `branding-openSUSE`, треба видалити або замінити: власним пакетом (як у таблиці вище) або відповідним `branding-basedonopensuse`, який openSUSE готує саме для похідних. Для пакетів, які ми не хочемо брендувати самі, беремо `*-branding-basedonopensuse`.
- Заголовки пакетів, README і журнали змін можна не чистити, якщо вони не створюють враження офіційного зв'язку з проєктом openSUSE.
- **Можна писати «Based on openSUSE»** (а також «Powered by openSUSE», «Built on openSUSE») поруч із власною маркою: на сайті, в «Про систему», у документації. Дозволу на це не потрібно.
- **Не можна** включати «openSUSE» у назву продукту, компанії чи домену без дозволу. «Vilna», vilnaos.com і gloxi.eu цим правилам не суперечать.
- Збирати можна через KIWI, OBS або власний процес; окремого погодження для цього не потрібно.
- Контакт для питань і дозволів — правління openSUSE (board@lists.opensuse.org).

Брендування Aeon (`*-branding-Aeon`, логотипи Aeon) замінюємо так само: це частина айдентики проєкту openSUSE.

Перед першою публікацією образу треба перевірити, що жоден пакет не тягне брендування openSUSE через залежності: зібрати образ і пошукати в ньому файли й пакети з `openSUSE` та `Aeon` у назві. Перевірку додати в CI збірки образу.

### 4.6. Встановлення, перший запуск і локалізація

| Пакет | Що робить |
| --- | --- |
| `vilna-installer` | Діалогове встановлення з Gloxi і звичайний майстер на тому самому плані ([`installer.md`](installer.md)); з етапу 3 |
| `vilna-mcp-installer` | MCP-сервер встановлення: огляд заліза, дисків, мереж; змінює лише план |
| `vilna-initial-setup` | Додаткові сторінки `gnome-initial-setup`: продовження розмови з Gloxi після встановлення, перенесення файлів, згода на хмару |
| `vilna-locale` | Переклади Gloxi і Herkos; мови за дорожньою картою: uk, en, de, fr, es, pl, lt, далі решта мов ЄС |

Системні локалі (`glibc-locale`), шрифти з кирилицею, латиницею з діакритиками й грецькою (Noto) і розкладки клавіатури вже є в Aeon.

## 5. Що змінюємо в поведінці Aeon

| Що | Зміна | Чому |
| --- | --- | --- |
| Служби за замовчуванням | Вмикаємо `herkos.service`, `gloxi.service` | Асистент працює з першого запуску |
| SELinux | Додаємо модуль `herkos-selinux`, режим enforcing не змінюємо | Ізоляція Gloxi на рівні ядра на додачу до systemd |
| Знімки snapper | Додаємо конфігурацію для `/home` з політикою зберігання під Mitos | Відкат змін Gloxi у файлах користувача (у Aeon знімки лише для кореня) |
| Автоматичні оновлення | Залишаємо як в Aeon; Mitos записує кожне оновлення в журнал | Користувач бачить, що змінилося |
| Avahi, samba, openssh | **Вимкнені за замовчуванням** (рішення 2.11.2026): пакети залишаються, служби не вмикаються через `systemd-presets-branding-Vilna`. Користувач вмикає в налаштуваннях; через Gloxi — як дія R3 | Принцип найменшої поверхні атаки. Пакети не видаляємо, щоб не розходитися з Aeon |
| kdump, `steam-devices` | Залишаємо | Видалення не дає переваг і створює розбіжність з Aeon |

## 6. Програми за замовчуванням (Flatpak)

Встановлюються під час першого запуску з Flathub. Список — пропозиція для обговорення.

| Призначення | Програма | Чому важливо для Gloxi |
| --- | --- | --- |
| Браузер | Firefox | — |
| Офіс | LibreOffice | Файли ODF/DOCX, з якими працює інструмент «файли» |
| Пошта | Thunderbird | Та сама скринька IMAP, що й MCP-сервер «пошта» |
| Календар, контакти | GNOME Calendar, GNOME Contacts | Показують події, які створює Gloxi |
| Базові | Text Editor, Loupe (зображення), Papers (PDF), Calculator, Clocks, Weather | — |
| Менеджер паролів | **KeePassXC** (рішення 2.11.2026): офлайн, відкритий формат KDBX, інтеграція з браузером. Синхронізація — пізніше через зашифровану хмару компанії (файл бази в її папці). Розглянути після бети: підтримка браузера в GNOME Secrets для рідного вигляду GNOME | Herkos забороняє читати паролі; має бути зрозуміле місце для них |

## 7. Репозиторії й збірка

- **OBS-проєкт Vilna** (назву узгодити, наприклад `home:<обліковий запис>:vilna` для початку): наші пакети + брендування, збираються проти Tumbleweed.
- **Опис образу:** форк KIWI-опису `openSUSE-Aeon` з `devel:microos:aeon:images` із заміною патерна на `vilna_base` і нашим репозиторієм.
- **Встановлювач:** на етапі 2 — tik (Transactional Installation Kit), як у Aeon, з образом `tik-osimage-Vilna`. Далі — діалогове встановлення з Gloxi ([`installer.md`](installer.md)).
- **Ризик rolling-бази:** Aeon оновлюється разом із Tumbleweed. Потрібна автоматична перевірка образу після кожного оновлення бази (завантаження у VM, запуск Herkos і Gloxi, відкат). Наприклад, власний невеликий тест у CI або openQA пізніше.

## 8. Відкриті питання

1. Яку локальну модель брати за замовчуванням і як перевіряти її ліцензію?
