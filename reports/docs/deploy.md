# Розгортання reports.vilnaos.com

Стан: працює з 2026-11-12 на IONOS VPS S+ (https://reports.vilnaos.com). Постачальник — IONOS VPS S+ (рішення Denys 2026-11-12). Кроки 1–4 робить **лише Denys** (оренда, гроші, DNS, договори); кроки 5–7 — будь-хто з доступом SSH.

## Що потрібно

| Що | Чому | Ціна |
| --- | --- | --- |
| **IONOS VPS S+** (1 vCore, 2 ГБ, 60 ГБ NVMe), розташування **ЄС**, Debian 13 | Дані в ЄС (ADR 0018 розд. 6). Сервісу вистачає 256 МБ пам'яті, решта — для інших простих публічних сервісів. Збирання Rust на 1 ядрі триває довше; `install.sh` сам додає тимчасовий swap | €2 на місяць без ПДВ у перші місяці за акцією, далі €5 (2026-11-12) |
| Не той сервер, що для копій, віддаленого стирання, облікових записів чи Vilna Chat | Тут лежить токен GitHub (лише Issues) і відкрита точка для всіх; поруч можна тримати лише так само прості публічні сервіси, кожен своєю службою systemd | — |
| DNS у Arsys: `reports.vilnaos.com` → `A` і `AAAA` на адреси VM; `CAA` для Let's Encrypt уже є на `vilnaos.com` | TLS від Let's Encrypt через Caddy | — |
| Договір обробки даних (DPA) з IONOS | GDPR: звіти — персональні дані. В IONOS він входить в умови (CGC) з 14.12.2023, тобто діє з моменту замовлення; текст — Mi cuenta → Protección de datos → «Contrato de tratamiento de datos» (перевірено 2026-11-12) | безкоштовно |
| Fine-grained токен GitHub: лише репозиторій VilnaOS, Issues: read and write, строк 1 рік | Сервер відкриває issue `auto-report` для схвалених збоїв | — |
| Мітка `auto-report` у VilnaOS | Тією міткою позначаються автоматичні issue | — |

## Кроки

1. **Сервер (Denys).** IONOS → VPS S+: розташування ЄС, образ Debian 13, без Plesk, без резервних копій Acronis (див. нижче), без SSL IONOS (сертифікат бере Caddy). У Cloud Panel додати свій SSH-ключ; у політиці файрвола: вхідні TCP 22 (лише зі своїх адрес, якщо можна), 80 і 443; решта закрито. Вхід: `ssh root@<IP>`.
2. **DPA.** В IONOS окремо підписувати не треба: договір входить в умови з 14.12.2023. PDF за бажанням — Mi cuenta → Protección de datos.
3. **DNS (Denys).** В Arsys для `vilnaos.com`: `reports` `A` → IPv4 VM, `reports` `AAAA` → IPv6 VM, TTL 3600. Перевірка: `dig +short reports.vilnaos.com A`.
4. **GitHub (Denys).** Створити мітку `auto-report` у VilnaOS і fine-grained токен (вище). Токен не надсилати в чат: його вставляють прямо на сервері в кроці 6.
5. **Установлення.** На VM від root:

   ```sh
   apt-get update && apt-get full-upgrade -y && apt-get install -y git
   git clone https://github.com/StDenis1980/VilnaOS.git /opt/VilnaOS
   sh /opt/VilnaOS/reports/deploy/install.sh
   ```

   Скрипт ставить Caddy і Rust, збирає `vilna-reports-server` з `--locked`, кладе службу, Caddyfile і налаштування журналу на 7 днів, робить ключ адміністратора `/etc/vilna-reports/admin_key` і вмикає службу. Ключ адміністратора переписати в менеджер паролів.
6. **Токен GitHub.** `install -m 0600 /dev/stdin /etc/vilna-reports/github_token` (вставити токен, Ctrl+D), потім `systemctl restart vilna-reports`.
7. **Перевірка.**

   ```sh
   curl -fsS https://reports.vilnaos.com/healthz              # ok
   curl -fsS -X POST https://reports.vilnaos.com/v1/crash \
     -H 'content-type: application/json' \
     -d '{"schema":1,"id":"00000000-0000-4000-8000-000000000001","program":{"name":"vilna-test"},"kind":"panic","location":"crates/test/src/lib.rs:1"}'
   curl -fsS -X POST https://reports.vilnaos.com/v1/forget \
     -H 'content-type: application/json' -d '{"ids":["00000000-0000-4000-8000-000000000001"]}'
   journalctl -u vilna-reports -u caddy --since -10min   # без IP-адрес
   ```

Оновлення: `cd /opt/VilnaOS && git pull && sh reports/deploy/install.sh` (база й ключі лишаються).

## Що лишається після розгортання

- Сторінка `/privacy/` на сайті: що приймає сервер, де (IONOS, дата-центр у ЄС), скільки (90 днів), як видалити (`/v1/forget` з пристрою або лист на privacy@), хто обробник (IONOS). До першого реального звіту.
- Сторінки сайту `/feedback/`, `/feedback/thanks/` 10 мовами з формою з [`protocol.md`](protocol.md) (Gloxi 0.2).
- Каталог ресурсів проєкту: VM, DNS-запис, токен (лише назва й де лежить), DPA.

## Резервні копії

Своїх не робимо. У базі лише звіти на 90 днів і відгуки; втратити їх не страшно, а довга копія тримала б дані довше, ніж ми обіцяємо, і після `/v1/forget`. Лічильники за сигнатурою можна відновити з issue `auto-report`.

Резервні копії IONOS (Acronis) для сервера не вмикаємо з тієї ж причини.

## Чому так

- **IONOS VPS S+.** Порівнювали 2026-11-10/12 OVHcloud, Hetzner, IONOS, Scaleway, netcup, UpCloud, Infomaniak. OVHcloud VPS-1 (€3,81) не вдалося замовити в зоні ЄС (бракувало на складі), а 2026-11-12 у Hetzner Cloud теж не було вільних серверів. IONOS — німецька компанія, дані в ЄС, найменший VPS доступний одразу й найдешевший (€5 на місяць після акції), а серверу звітів 1 ядра й 2 ГБ досить. AWS і Azure відкинуто: американські компанії (CLOUD Act), а серверу звітів треба читати дані, тож шифрування диска не допомагає. `install.sh` працює на будь-якій VM з Debian 13, тож перейти до іншого постачальника просто.
- **Debian 13 і Caddy з Debian.** Мінімум частин, оновлення безпеки через `unattended-upgrades`, TLS сам. Caddy без директиви `log` не пише журнал доступу, а глобальний фільтр прибирає адреси з журналу помилок.
- **systemd з `DynamicUser`, `ProtectSystem=strict`, без привілеїв.** Служба пише лише свою базу в `/var/lib/vilna-reports/`; секрети приходять через `LoadCredential=` і не видні в `systemctl show`.
- **Збирання на сервері.** Немає окремого каналу випусків бінарних файлів, який теж треба було б охороняти; `--locked` бере ті самі версії залежностей, що й CI.
