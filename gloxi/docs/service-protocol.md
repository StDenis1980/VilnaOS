# Протокол між вікном Gloxi і сервісом `gloxid`

Стан: версія 1 (листопад 2026). Рішення — [ADR 0025](../../docs/adr/0025-gloxi-system-service.md). Код: сервіс [`src/gloxi/service.py`](../src/gloxi/service.py), вікно [`src/gloxi/gui/remote.py`](../src/gloxi/gui/remote.py), тести [`tests/test_service.py`](../tests/test_service.py).

## З'єднання

- Unix-сокет, потоковий. У Vilna `/run/gloxi/<uid>/gloxi.sock`: його тримає systemd (`gloxi@<uid>.socket`) і передає `gloxid` як stdin. Для розробки — `gloxid --socket <шлях>`, а вікно бере шлях зі змінної `GLOXI_SERVICE_SOCKET`.
- `gloxid` дивиться, хто під'єднався (`SO_PEERCRED`), і відповідає лише людині, якій належить (`--owner-uid`), або root. Решті він не відповідає зовсім і закриває з'єднання.
- Вікно водночас одне: нове з'єднання забирає події собі, а старе отримує `{"event":"replaced"}` і закривається.
- Кожне повідомлення — один об'єкт JSON в UTF-8 в одному рядку. Рядок, довший за 1 МіБ, закриває з'єднання.

## Запити вікна

`{"id": 1, "method": "...", "params": {...}}` → `{"id": 1, "result": {...}}` або `{"id": 1, "error": {"code": "...", "message": "..."}}`.

| Метод | Параметри | Результат |
| --- | --- | --- |
| `hello` | `language` (необов'язково; значення `LANGUAGE`/`LANG` вікна, напр. `uk_UA.UTF-8`) | `protocol`, `version`, `state` |
| `state` | — | `state` (нижче) |
| `chat.submit` | `text` | `accepted`: чи взято повідомлення (не взято, коли Gloxi зайнятий, текст порожній або хмару вимкнено) |
| `chat.reset` | — | `reset`: чи почато нову розмову |
| `chat.set_mode` | `mode`: `auto`, `local` або `cloud` | `ok`, `state` |
| `chat.offer_intro` | `always`: `true` — розмова про копії на прохання людини (`/backup`) | `started` |
| `chat.report_problem` | — | `started` |

`state`: `busy`, `cloud_status` (рядок для заголовка), `current` (вибраний режим), `choices` — `[{"mode", "label", "detail"}]` для перемикача моделі.

Коди помилок: `bad_json`, `bad_request`, `bad_params`, `unknown_method`.

Рядки в результатах і подіях уже перекладено мовою з `hello`.

## Події сервісу

`{"event": "...", ...}` приходять будь-коли, також посеред відповіді на запит. Вони повторюють виклики, які `ChatController` робить у вікні (`ChatView`):

| Подія | Поля | Що робить вікно |
| --- | --- | --- |
| `user_message` | `text` | показує репліку людини |
| `start_answer` | `label` | починає відповідь з підписом («На цьому комп'ютері», «Хмара: …») |
| `append_answer` | `piece` | дописує шматок відповіді |
| `finish_answer` | — | закінчує відповідь |
| `busy` | `busy` | вмикає або вимикає очікування |
| `note` | `text` | коротке повідомлення (дія через Herkos, підказка) |
| `error` | `text` | помилка |
| `open_uri` | `url` | відкриває посилання; вікно бере лише `https://` і `mailto:` |
| `replaced` | — | Gloxi відкрито в іншому вікні |

## Чого в протоколі ще немає

- Завантаження моделі: у Vilna модель — `vilna-llm.service`, а не справа вікна (кроки 3 і 5 ADR 0025).
- Тек для Herkos-lite і програм для Gloxi: у Vilna теки й програми — справа системного `herkosd`; вікно в режимі сервісу ці кнопки не показує.
- Історії після перезапуску сервісу: поки розмова живе в пам'яті `gloxid`.
