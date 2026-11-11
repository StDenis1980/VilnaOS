# Розгортання reports.vilnaos.com

Стан: усе для розгортання готове в репозиторії; сервер ще не орендовано. Постачальник — Hetzner Cloud (рішення Denys 2026-11-11). Кроки 1–4 робить **лише Denys** (оренда, гроші, DNS, договори); кроки 5–7 — будь-хто з доступом SSH.

## Що потрібно

| Що | Чому | Ціна |
| --- | --- | --- |
| **Hetzner Cloud, найменший тип Regular Performance** (2 vCPU, 4 ГБ) у **Німеччині** (Falkenstein, Nuremberg) чи **Фінляндії** (Helsinki), Debian 13, IPv4 + IPv6 | Дані в ЄС (ADR 0018 розд. 6). Сервісу вистачає 256 МБ пам'яті, решта — для інших простих публічних сервісів | від €5,49 на місяць без ПДВ (2026-11-11); перевірити, чи входить IPv4 |
| Не той сервер, що для копій, віддаленого стирання, облікових записів чи Vilna Chat | Тут лежить токен GitHub (лише Issues) і відкрита точка для всіх; поруч можна тримати лише так само прості публічні сервіси, кожен своєю службою systemd | — |
| DNS у Arsys: `reports.vilnaos.com` → `A` і `AAAA` на адреси VM; `CAA` для Let's Encrypt уже є на `vilnaos.com` | TLS від Let's Encrypt через Caddy | — |
| Договір обробки даних (AVV/DPA) з Hetzner | GDPR: звіти — персональні дані. У Hetzner його укладають у кабінеті (розділ про захист даних, «Auftragsverarbeitung») | безкоштовно |
| Fine-grained токен GitHub: лише репозиторій VilnaOS, Issues: read and write, строк 1 рік | Сервер відкриває issue `auto-report` для схвалених збоїв | — |
| Мітка `auto-report` у VilnaOS | Тією міткою позначаються автоматичні issue | — |

## Кроки

1. **Сервер (Denys).** Hetzner Console → новий проєкт «Vilna» → Add Server: розташування Falkenstein, Nuremberg чи Helsinki, образ Debian 13, тип — найменший Regular Performance (2 vCPU, 4 ГБ), увімкнути IPv4 і IPv6, додати свій SSH-ключ, Backups **не** вмикати (див. нижче). Firewall у Hetzner Console: вхідні TCP 22 (лише зі своїх адрес, якщо можна), 80, 443 і ICMP; решта закрито; прив'язати до сервера. Вхід: `ssh root@<IP>`.
2. **DPA (Denys).** Укласти договір обробки даних у Hetzner Console до першого реального звіту.
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

- Сторінка `/privacy/` на сайті: що приймає сервер, де (Hetzner, Німеччина чи Фінляндія), скільки (90 днів), як видалити (`/v1/forget` з пристрою або лист на privacy@), хто обробник (Hetzner). До першого реального звіту.
- Сторінки сайту `/feedback/`, `/feedback/thanks/` 10 мовами з формою з [`protocol.md`](protocol.md) (Gloxi 0.2).
- Каталог ресурсів проєкту: VM, DNS-запис, токен (лише назва й де лежить), DPA.

## Резервні копії

Своїх не робимо. У базі лише звіти на 90 днів і відгуки; втратити їх не страшно, а довга копія тримала б дані довше, ніж ми обіцяємо, і після `/v1/forget`. Лічильники за сигнатурою можна відновити з issue `auto-report`.

Резервні копії Hetzner для сервера (Backups) не вмикаємо з тієї ж причини.

## Чому так

- **Hetzner Cloud.** Порівнювали 2026-11-10/11 OVHcloud, IONOS, Scaleway, netcup, UpCloud, Infomaniak. Найдешевший OVHcloud VPS-1 (€3,81) не вдалося замовити в зоні ЄС (бракувало на складі; лишався лише Цюрих і Амстердам LocalZone за €5,49), тож Hetzner за ту саму ціну, німецька компанія, дані в ЄС, доступний одразу. AWS і Azure відкинуто: американські компанії (CLOUD Act), а серверу звітів треба читати дані, тож шифрування диска не допомагає. `install.sh` працює на будь-якій VM з Debian 13, тож перейти до іншого постачальника просто.
- **Debian 13 і Caddy з Debian.** Мінімум частин, оновлення безпеки через `unattended-upgrades`, TLS сам. Caddy без директиви `log` не пише журнал доступу, а глобальний фільтр прибирає адреси з журналу помилок.
- **systemd з `DynamicUser`, `ProtectSystem=strict`, без привілеїв.** Служба пише лише свою базу в `/var/lib/vilna-reports/`; секрети приходять через `LoadCredential=` і не видні в `systemctl show`.
- **Збирання на сервері.** Немає окремого каналу випусків бінарних файлів, який теж треба було б охороняти; `--locked` бере ті самі версії залежностей, що й CI.
