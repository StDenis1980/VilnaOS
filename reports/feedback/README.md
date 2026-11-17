# «Відгук про Vilna» (`vilna-feedback`)

Вікно з [ADR 0018](../../docs/adr/0018-error-reports-and-feedback.md), розділ 5: відгук і звіти про збої. GTK 4 і libadwaita з системи, решта — стандартна бібліотека Python. Мережі в програми немає: усе йде через службу [`vilna-reportd`](../client/) по її сокету `/run/vilna-reportd/control`.

Дві сторінки:

- **Відгук.** Про що це (щось не працює, ідея, інше), текст і три вибори людини, жоден не ввімкнено наперед: додати відомості про комп'ютер, відповісти на e-mail, дозволити публікацію. «Що буде надіслано» показує запит, який служба перевірила й очистила (операція `preview`), тобто саме те, що піде на сервер.
- **Звіти про збої.** Відповідь про звіти (зміна через `pkexec vilna-reportd consent …`, polkit питає адміністратора), список звітів цієї людини й системи з «Переглянути», «Надіслати» і «Не надсилати», та «Видалити з сервера все, що я надіслав».

`vilna-feedback --watch` запускається в сесії (autostart). Лише за відповіді «Питати щоразу» він показує одне сповіщення на кожен новий звіт із кнопками «Переглянути», «Надіслати» і «Не надсилати». Уже показані звіти пам'ятає в `~/.local/state/vilna-feedback/notified.json`.

Тексти — англійські msgid у `texts.py`, переклади всіма мовами ADR 0009 у `src/vilna_feedback/locale/<код>.po`. Українська — наша, решта зроблена з допомогою ШІ й чекає погляду носія мови.

| Файл | Куди в образі |
| --- | --- |
| `data/io.github.stdenis1980.VilnaFeedback.desktop` | `/usr/share/applications/` |
| `data/vilna-feedback-watch.desktop` | `/etc/xdg/autostart/` |
| `data/io.github.stdenis1980.vilna-reportd.policy` | `/usr/share/polkit-1/actions/` |

## Перевірка

```sh
cd reports/feedback
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Тести вікна (`tests/test_window.py`) потребують GTK 4, libadwaita і дисплея (досить `xvfb-run`), без них пропускаються. Вони говорять із підробленою службою на справжньому Unix-сокеті.
