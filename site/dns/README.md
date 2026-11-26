# DNS vilnaos.com: deSEC і bunny DNS

Зону vilnaos.com обслуговують одночасно deSEC і bunny DNS ([ADR 0032](../../docs/adr/0032-site-hosts-dns.md)). Arsys лишається лише реєстратором. Обидва провайдери не вміють передавати зону один одному, тому записи лежать у файлі [`vilnaos.com.toml`](vilnaos.com.toml), а [`tools/site_dns.py`](../../tools/site_dns.py) пише їх в обох.

- **Змінити запис:** PR зі зміною `vilnaos.com.toml`. Після злиття робочий процес [`dns.yml`](../../.github/workflows/dns.yml) запише його в обох провайдерів. Руками в панелях записи не правимо, інакше наступний запуск їх поверне.
- **Корінь і `www`** (адреса сайту) задає таблиця `[site]`. `dns.yml` їх не чіпає. Перемикає їх [`site-watch.yml`](../../.github/workflows/site-watch.yml) або команда `failover`.
- **Перевірити:** `python3 tools/site_dns.py check` (без мережі). `diff` показує, чим кожен провайдер відрізняється від файлу (потрібні токени).

## Перший запуск (робить Denys)

Кожен крок можна відкотити, поки не змінено NS в Arsys (крок 6). До кроку 6 сайт і пошта працюють як зараз.

1. **Звірити файл з Arsys.** Відкрити в Arsys усі записи vilnaos.com і порівняти з `vilnaos.com.toml`. Чого у файлі немає (наприклад, TXT перевірки домену від mailbox.org або щось додане пізніше), треба додати у файл через PR до кроку 6, бо інакше після переходу цього запису не буде. Скриншот або список записів можна дати Claude, і він внесе зміни.
2. **deSEC.** Зареєструватися на https://desec.io (пошта hello@vilnaos.com, пароль у менеджері паролів). Створити два домени:
   - `vilnaos.com`;
   - `vilnaos-acme.dedyn.io` (безкоштовне ім'я deSEC, лише для сертифіката запасного хоста; якщо воно зайняте, взяти інше й змінити CNAME `_acme-challenge` у файлі).

   У Token management створити два токени:
   - `github-vilnaos-dns`: для `dns.yml` і `site-watch.yml`, з правом лише на домен vilnaos.com (Token policy);
   - `vps-acme`: для VPS, з правом лише на `vilnaos-acme.dedyn.io`.

   Написати в підтримку deSEC з проханням знизити найменший TTL для vilnaos.com до 300 с (для швидкого перемикання). Коли погодять, змінити `ttl` у `[site]` на 300.
3. **bunny DNS.** DNS → Add Zone → `vilnaos.com`. Записи не імпортувати: їх запише скрипт. В Account → API скопіювати API key.
4. **GitHub.** VilnaOS → Settings → Environments → New environment `dns`. Deployment branches: лише `main`. Required reviewers не ставити, інакше перемикання вночі чекатиме на тебе. Секрети середовища: `DESEC_TOKEN` (токен `github-vilnaos-dns`), `BUNNY_API_KEY`.
5. **Записати зону й перевірити.** Actions → DNS → Run workflow не запуститься, доки немає змінної, тому спершу локально або з iapc:

   ```sh
   export DESEC_TOKEN=… BUNNY_API_KEY=…
   python3 tools/site_dns.py apply --with-site          # показує, що запише
   python3 tools/site_dns.py apply --with-site --yes    # пише
   python3 tools/site_dns.py diff --with-site           # має бути 0 і 0
   ```

   Потім змінна репозиторію `VILNA_SITE_DNS` = `true` (Settings → Secrets and variables → Actions → Variables). Перевірити, що обидва провайдери відповідають як Arsys:

   ```sh
   for ns in ns1.desec.io kiki.bunny.net dns1.servidoresdns.net; do
     dig +short @$ns vilnaos.com MX; dig +short @$ns flatpak.vilnaos.com CNAME
   done
   ```

6. **NS в Arsys.** Для vilnaos.com замінити сервери імен на чотири: `ns1.desec.io`, `ns2.desec.org`, `kiki.bunny.net`, `coco.bunny.net`. Запис DS (DNSSEC) не додавати. Протягом доби перевірити сайт, пошту (надіслати й отримати лист), `https://flatpak.vilnaos.com/vilna.flatpakrepo` і `https://reports.vilnaos.com/healthz`.
7. **Запасний хост** — [`site/deploy/README.md`](../deploy/README.md). Коли сайт там відповідає, можна ввімкнути автоматичне перемикання: змінна `VILNA_SITE_FAILOVER` = `auto`. Без неї `site-watch.yml` лише повідомляє про збій.

Повернути все назад: у кроці 6 знову поставити сервери імен Arsys (записи там лишаються, поки їх не видалити).

## Перемикання вручну

Actions → Site watch → Run workflow → «move to»: `ionos` або `statichost`. Або локально: `python3 tools/site_dns.py failover ionos --yes`.

## DNSSEC

Поки вимкнено: deSEC підписує зону сам, bunny DNS — ні, а без запису DS в Arsys це звичайна непідписана зона. Щоб увімкнути DNSSEC з двома провайдерами, потрібна схема multi-signer ([RFC 8901](https://www.rfc-editor.org/rfc/rfc8901)): кожен провайдер публікує ключі обох. Спершу перевіримо її на vilnaos.online.
