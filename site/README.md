# Сайт vilnaos.com

Статичний сайт Vilna OS: звичайні HTML-файли в [`public/`](public/), без генератора, без JavaScript і без жодного ресурсу з інших серверів. Хостинг роздає теку `public/` як є.

| Адреса | Файл |
| --- | --- |
| `/`, `/en/` | заглушка «Скоро» (до релізу Gloxi 0.1; повна головна — в історії git, коміт f113a49) |
| `/gloxi/`, `/en/gloxi/` | Gloxi 0.1 preview для тестувальників (до релізу — лише за прямим посиланням, `noindex`) |
| `/privacy/`, `/en/privacy/` | приватність |
| `/.well-known/security.txt` | контакт для вразливостей (RFC 9116) |
| `/404.html` | сторінка «не знайдено» |

Відгуки тестувальників приходять через форму [`.github/ISSUE_TEMPLATE/gloxi-feedback.yml`](../.github/ISSUE_TEMPLATE/gloxi-feedback.yml) з міткою `gloxi-feedback`.

## Перевірка

```sh
python3 tools/check_site.py
```

Скрипт (і CI) перевіряє: жодного `<script>`, `<iframe>`, `style=` чи `on…=`; усі `src` і стилі локальні; локальні посилання існують; у кожної української сторінки є англійська пара під `/en/`; `security.txt` не прострочений.

Подивитися локально: `python3 -m http.server -d site/public 8000` і відкрити http://127.0.0.1:8000/.

## Як додати сторінку

1. Скопіюйте найближчу сторінку, змініть `<title>`, `description`, `hreflang`-посилання, `aria-current` у меню.
2. Додайте англійську пару під `en/` (перевірка без неї не пройде).
3. Шапка й підвал однакові на всіх сторінках; якщо змінюєте меню — змінюйте всюди. Коли сторінок стане більше десятка, перейдемо на генератор (Zola).

## Розгортання: statichost.eu

Хостинг — [statichost.eu](https://statichost.eu/) (рішення Denys 3.11.2026). Публікує й змінює DNS лише Denys або за його згодою.

- [`statichost.yml`](statichost.yml): збірки немає, публікується тека `public/` як є.
- [`public/_headers`](public/_headers): заголовки безпеки для всього сайту (CSP без сторонніх джерел, HSTS на тиждень, `no-referrer`). Через місяць без проблем — `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
- На кожній сторінці `<link rel="canonical">` на https://vilnaos.com/…, тож копії на інших адресах не заважають пошуку.

### Один раз у панелі statichost.eu

1. Створити сайт (назва, наприклад, `vilnaos`), репозиторій `https://github.com/StDenis1980/VilnaOS.git` (публічний, ключ не потрібен), гілка `main`, **working directory: `site`**.
2. Settings → Domains: додати `vilnaos.com` і `www.vilnaos.com`. Точну адресу для DNS (`<назва>.statichost.page`) показує ця ж сторінка.
3. Автооновлення: у GitHub, Settings → Webhooks → Add webhook, payload URL `https://builder.statichost.eu/<назва сайту>`, content type `application/json`, «Just the push event».

### Записи DNS в Arsys

| Домен | Тип | Значення |
| --- | --- | --- |
| `vilnaos.com` | ALIAS, якщо Arsys його має; інакше A + AAAA | `<назва>.statichost.page.` або `95.217.26.94` і `2a01:4f9:c01f:8002::` ([документація](https://www.statichost.eu/docs/domains/), звірити з панеллю) |
| `www.vilnaos.com` | CNAME | `<назва>.statichost.page.` |
| `vilnaos.com` | CAA | `0 issue "letsencrypt.org"` — лише після того, як statichost підтвердить, який центр сертифікації він використовує |
| `vilnaos.com`, `.es`, `.eu`, `.online` | MX | `0 .` (пошти поки немає) |
| ті самі | TXT | `v=spf1 -all` |
| `_dmarc.` кожного | TXT | `v=DMARC1; p=reject` |

HTTPS-сертифікат statichost випускає сам після першого запиту на домен.

### vilnaos.es, vilnaos.eu, vilnaos.online

Переадресацію за доменом (host → host) документація statichost не описує, а `_redirects` працює лише за шляхом. Поки ці три домени стоять лише із захисними поштовими записами й не відкривають сайт. Варіанти пізніше: спитати підтримку statichost про переадресацію доменів-синонімів або увімкнути переадресацію в Arsys, якщо вона працює через HTTPS.

### Перевірка після підключення

```sh
curl -sI https://vilnaos.com/ | grep -i -E "^(HTTP|content-security|strict-transport)"
curl -s https://vilnaos.com/.well-known/security.txt | head -3   # тека з крапкою має віддаватися
curl -sI http://vilnaos.com/ | head -3                           # має вести на https
```

## Запуск

До випуску Gloxi 0.1 сайт закритий від пошукових систем ([`public/robots.txt`](public/robots.txt)). У день випуску:

1. Тег `gloxi-v0.1.0` (ставить Denys) → CI публікує `gloxi.flatpak` і `.sha256` на GitHub Releases.
2. На сторінках Gloxi прибрати речення «Gloxi 0.1 вийде наприкінці січня…» і `noindex`; повернути головну з коміту f113a49 (якщо перевірка назви пройдена).
3. `robots.txt` → `User-agent: *` + `Allow: /`.
4. Раз на рік оновлювати `Expires` у `security.txt` (CI нагадає, коли строк мине).
