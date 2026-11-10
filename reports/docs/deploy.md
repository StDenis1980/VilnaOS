# Розгортання reports.vilnaos.com

Стан: усе для розгортання готове в репозиторії; сервер ще не орендовано. Постачальник — OVHcloud (рішення Denys 2026-11-10). Кроки 1–4 робить **лише Denys** (оренда, гроші, DNS, договори); кроки 5–7 — будь-хто з доступом SSH.

## Що потрібно

| Що | Чому | Ціна |
| --- | --- | --- |
| **OVHcloud VPS-1** (2 vCore, 4 ГБ, 40 ГБ NVMe, необмежений трафік, anti-DDoS) з розташуванням у **Франції** (Gravelines, Roubaix, Strasbourg) чи **Німеччині** (Frankfurt), Debian 13, IPv4 + IPv6 | Дані в ЄС (ADR 0018 розд. 6), рішення Denys 2026-11-10. Сервісу вистачає 256 МБ пам'яті, решта — для інших простих публічних сервісів | €3,81 на місяць без ПДВ (€4,61 з ПДВ, 2026-11-10) |
| Не той сервер, що для копій, віддаленого стирання, облікових записів чи Vilna Chat | Тут лежить токен GitHub (лише Issues) і відкрита точка для всіх; поруч можна тримати лише так само прості публічні сервіси, кожен своєю службою systemd | — |
| DNS у Arsys: `reports.vilnaos.com` → `A` і `AAAA` на адреси VM; `CAA` для Let's Encrypt уже є на `vilnaos.com` | TLS від Let's Encrypt через Caddy | — |
| Договір обробки даних (DPA) з OVHcloud | GDPR: звіти — персональні дані. У OVHcloud він входить в умови договору; у кабінеті (Control Panel → акаунт → Contracts) перевірити, що він є, і зберегти PDF | безкоштовно |
| Fine-grained токен GitHub: лише репозиторій VilnaOS, Issues: read and write, строк 1 рік | Сервер відкриває issue `auto-report` для схвалених збоїв | — |
| Мітка `auto-report` у VilnaOS | Тією міткою позначаються автоматичні issue | — |

## Кроки

1. **Сервер (Denys).** ovhcloud.com → VPS → VPS-1 → «Configurar»: розташування у Франції чи Німеччині, образ Debian 13 (якщо його ще немає — Debian 12, далі оновлення до 13), додати свій SSH-ключ. Після створення в Control Panel: Network → IP → для адреси VPS увімкнути **Edge Network Firewall** з правилами «дозволити TCP 22 (лише зі своїх адрес, якщо можна), 80, 443, ICMP; решта — заборонити». Вхід: `ssh debian@<IP>`, далі `sudo -i`. Запис IPv6 VPS видно на сторінці VPS.
2. **DPA (Denys).** Перевірити й зберегти договір обробки даних OVHcloud до першого реального звіту.
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

- Сторінка `/privacy/` на сайті: що приймає сервер, де (OVHcloud, Франція чи Німеччина), скільки (90 днів; щоденна копія VPS у OVHcloud — ще до доби), як видалити (`/v1/forget` з пристрою або лист на privacy@), хто обробник (OVHcloud). До першого реального звіту.
- Сторінки сайту `/feedback/`, `/feedback/thanks/` 10 мовами з формою з [`protocol.md`](protocol.md) (Gloxi 0.2).
- Каталог ресурсів проєкту: VM, DNS-запис, токен (лише назва й де лежить), DPA.

## Резервні копії

Своїх не робимо. У базі лише звіти на 90 днів і відгуки; втратити їх не страшно, а довга копія тримала б дані довше, ніж ми обіцяємо, і після `/v1/forget`. Лічильники за сигнатурою можна відновити з issue `auto-report`.

OVHcloud VPS-1 сам щодня робить копію всієї VPS і тримає лише останню (за попередні 24 години). Тому видалене (через 90 днів чи за `/v1/forget`) може ще до доби лежати в цій копії. Це треба одним реченням написати на `/privacy/`. Вимкнути копію в цьому тарифі не можна; якщо колись захочемо без неї, переходимо на тариф без копії чи інший сервер.

## Чому так

- **OVHcloud VPS-1.** Найдешевший серед порівняних 2026-11-10 (Hetzner, IONOS, Scaleway, netcup, UpCloud, Infomaniak) при 2 vCore і 4 ГБ; французька компанія, дані в ЄС. Запасний варіант — Hetzner Cloud: `install.sh` працює на будь-якій VM з Debian 13.
- **Debian 13 і Caddy з Debian.** Мінімум частин, оновлення безпеки через `unattended-upgrades`, TLS сам. Caddy без директиви `log` не пише журнал доступу, а глобальний фільтр прибирає адреси з журналу помилок.
- **systemd з `DynamicUser`, `ProtectSystem=strict`, без привілеїв.** Служба пише лише свою базу в `/var/lib/vilna-reports/`; секрети приходять через `LoadCredential=` і не видні в `systemctl show`.
- **Збирання на сервері.** Немає окремого каналу випусків бінарних файлів, який теж треба було б охороняти; `--locked` бере ті самі версії залежностей, що й CI.
