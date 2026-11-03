# Сайт vilnaos.com

Статичний сайт Vilna OS: звичайні HTML-файли в [`public/`](public/), без генератора, без JavaScript і без жодного ресурсу з інших серверів. Хостинг роздає теку `public/` як є.

| Адреса | Файл |
| --- | --- |
| `/`, `/en/` | головна |
| `/gloxi/`, `/en/gloxi/` | Gloxi 0.1 preview для тестувальників |
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

## Розгортання

Публікує й змінює DNS лише Denys або за його згодою. План: документ «План сайту й доменів Vilna OS» у проєкті.

Що налаштувати на хостингу:

**Переадресації (301)**

| Звідки | Куди |
| --- | --- |
| `http://*` | `https://` та сама адреса |
| `www.vilnaos.com/*` | `https://vilnaos.com/*` |
| `gloxi.eu/*`, `www.gloxi.eu/*` | `https://vilnaos.com/gloxi/` |

**Заголовки (для всіх відповідей)**

```
Content-Security-Policy: default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
Strict-Transport-Security: max-age=604800
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Permissions-Policy: camera=(), microphone=(), geolocation=(), interest-cohort=()
Cross-Origin-Opener-Policy: same-origin
```

Через місяць без проблем — `Strict-Transport-Security: max-age=31536000; includeSubDomains`.

**Журнали:** без IP-адрес або не довше 7 днів.

## Запуск

До випуску Gloxi 0.1 сайт закритий від пошукових систем ([`public/robots.txt`](public/robots.txt)). У день випуску:

1. Тег `gloxi-v0.1.0` (ставить Denys) → CI публікує `gloxi.flatpak` і `.sha256` на GitHub Releases.
2. На сторінках Gloxi прибрати речення «Gloxi 0.1 вийде наприкінці січня…».
3. `robots.txt` → `User-agent: *` + `Allow: /`.
4. Раз на рік оновлювати `Expires` у `security.txt` (CI нагадає, коли строк мине).
