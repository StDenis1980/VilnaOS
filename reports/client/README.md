# `vilna-reportd`

Збирач звітів про збої і відгуків на пристрої ([ADR 0018](../../docs/adr/0018-error-reports-and-feedback.md), розд. 2–6). Rust, без паніки й без `unsafe` (ті самі `deny`, що й у `protocol` і `server`). Мережі, крім одного запиту HTTPS до `reports.vilnaos.com`, у нього немає.

## Що робить служба (`vilna-reportd run`)

1. Раз на хвилину читає з журналу нові записи systemd-coredump (`journalctl --cursor-file`, `MESSAGE_ID=fc2e22bc…`), уперше — лише за останню добу.
2. З кожного бере тільки назву програми, її версію й пакування (rpm або Flatpak), сигнал і перший потік стеку: модуль, build-id і зсув, до 64 кадрів. **Ніколи** — дамп пам'яті, командний рядок, змінні оточення, робочу теку, шлях до програми, ім'я людини чи комп'ютера.
3. Звіт лягає в чергу `/var/lib/vilna-reports/reports/` з новим випадковим UUIDv4. Постійного ідентифікатора пристрою немає. Ліміти: до 50 у черзі, той самий збій тієї самої версії — раз на добу, не більше 20 надсилань на добу, тіла видаляються через 30 днів.
4. Надсилає сам **лише** за відповіді `auto`. За `ask` чекає, доки людина натисне «Надіслати» у вікні чи сповіщенні; за `never` чи без відповіді нічого не надсилає.
5. Збої Tinta Office пропускає: вона звітує сама й отримує ту саму відповідь (`TINTA_OFFICE_REPORTS`, `ask` для неї — `false`).

## Відповідь людини

Ставить установник (крок «Приватність», без попереднього вибору) або `vilna-reportd consent auto|ask|never` від root. Одна відповідь — три файли:

| Файл | Для кого |
| --- | --- |
| `/etc/vilna/reports.conf` (`crash_reports=…`) | `vilna-reportd` |
| `/etc/environment.d/60-vilna-reports.conf` (`TINTA_OFFICE_REPORTS=…`) | Tinta Office у сесії |
| `/etc/xdg/tinta-office/reports.conf` (`enabled=…`) | Tinta Office поза сесією |

Той самий текст пише рушій установника (`installer/src/vilna_installer/engine.py`); тест порівнює обидва.

## Сокет керування

`/run/vilna-reportd/control`, один JSON-рядок на запит. Підключитися може кожен, ядро каже хто (`SO_PEERCRED`): людина бачить і надсилає лише свої звіти й системні (uid < 1000), root — усі.

| `op` | Що |
| --- | --- |
| `status` | відповідь і скільки звітів чекає |
| `list`, `show` + `id` | звіти; `show` — рівно те, що піде (або пішло) на сервер |
| `send`, `discard` + `id` | надіслати або видалити звіт |
| `feedback` + `feedback` | надіслати відгук (канал `window` або `gloxi`, новий id); розмову установника (`conversation`) додає вікно, лише коли людина її прочитала й погодилася, бо служба файлів людини не читає |
| `preview` + `feedback` | те саме, перевірене й очищене, без надсилання (для «Що буде надіслано») |
| `forget` | видалити з сервера все, що ця людина надіслала (GDPR) |
| `system` | відомості про комп'ютер, які людина може додати до відгуку |

Командний рядок: `vilna-reportd status | list | system | forget`, `show | send | discard ID`, `feedback < feedback.json`, `parse FILE|-` (який звіт дав би кожен запис `journalctl -o json`, для налагодження).

## Файли в образі

- `data/vilna-reportd.service` → `/usr/lib/systemd/system/`: користувач `vilna-reports` у групі `systemd-journal`, `StateDirectory`, `RuntimeDirectory`, мережа лише до localhost і сервера звітів, решта систему лише читає.
- `data/vilna-reportd.sysusers` → `/usr/lib/sysusers.d/`.
- Вікно й сповіщення — [`../feedback/`](../feedback/).

Середовище: `VILNA_REPORTS_URL` (сервер, `http://` лише для localhost), `STATE_DIRECTORY`, `RUNTIME_DIRECTORY`, `VILNA_REPORTD_SOCKET`.
