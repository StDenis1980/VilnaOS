# Herkos

Herkos (гомерівська огорожа двору; внутрішня назва компонента) — шар безпеки Vilna. Через нього проходить кожна дія асистента Gloxi.

Складові:

- **Шлюз дозволів** («Монемвасія» — один вхід): єдина точка, де вирішується, чи можна виконати дію.
- **Keros** — фільтр prompt injection.
- **Mitos** — журнал дій і відкат через знімки btrfs.
- **Шлюз приватності** — псевдонімізація чутливих даних на пристрої перед відправкою в хмару.

Документи:

- Модель дозволів: [`docs/permissions.md`](docs/permissions.md) (чернетка v0.1).
- Пристрої людини: підключення, перелік і блокування: [`docs/devices.md`](docs/devices.md) (чернетка v0.1).
- Протокол Gloxi ↔ Herkos, журнал і формат маніфестів: [`docs/protocol.md`](docs/protocol.md) (v0.1).

## Herkos-lite (Gloxi 0.1 preview)

Python-реалізація шлюзу дозволів для сеансу користувача (ADR 0003, 0005). Залежностей під час роботи немає, лише стандартна бібліотека.

Стан: шлюз дозволів (рівні R0–R2, маніфести, модифікатори ризику), дозволи на клас дій, журнал Mitos з ланцюжком хешів, сокет JSON-RPC, MCP-клієнт і інструмент «файли» ([`../mcp/files/`](../mcp/files/)) зі скасуванням змін (`herkos undo`). Є також вікно підтвердження GTK (`herkos.dialog`) і клієнт для Gloxi (`herkos.client`). Gloxi вже викликає інструмент «файли» через цей клієнт (`gloxi/src/gloxi/tools.py`). Якщо встановлено [`../mcp/backup/`](../mcp/backup/), Herkos запускає й інструмент «копії» (ADR 0007). Keros-lite перевіряє вміст файлів на ознаки prompt injection: спершу правила, потім, якщо вони нічого не знайшли, локальна модель Gloxi ([ADR 0010](../docs/adr/0010-keros-local-model.md)). Herkos-lite працює під тим самим користувачем, що й Gloxi, і **не дає гарантій ізоляції** системного Herkos.

```sh
cd herkos
uv sync
uv run pytest
uv run herkos serve --confirm terminal   # питає дозволу в цьому терміналі
uv run herkos journal verify
uv run herkos permissions list
uv run herkos undo <action_id>           # скасувати зміну (id — з журналу)
```

Мова вікна й терміналу береться з системи (`LANG`); переклади — файли gettext `.po` у [`src/herkos/locale/`](src/herkos/locale/) (зараз українська). Повідомлення для моделі й журналу лишаються англійською.

Налаштування: `~/.config/herkos/config.toml` (див. [`src/herkos/config.py`](src/herkos/config.py)): `allowed_roots` (типово `~/Documents`), `bulk_threshold`, `confirm_timeout`, `keros_model` (типово `http://127.0.0.1:8080/v1`, або `"off"`), `keros_budget`. Замір Keros: `herkos keros-bench` ([`docs/keros-bench.md`](docs/keros-bench.md)).

## Тести відповідності

[`conformance/`](conformance/) говорить з Herkos лише через сокет, як Gloxi, тому ті самі тести перевіряють і Herkos-lite, і `herkosd`. Програму задає змінна `HERKOS_SERVE` (типово Herkos-lite):

```sh
cd herkos
uv run pytest conformance                                                  # Herkos-lite
HERKOS_SERVE="rs/target/debug/herkosd serve --confirm deny" HERKOSD=$PWD/rs/target/debug/herkosd \
    uv run pytest conformance                                              # herkosd
```

## herkosd (Rust, етап 2)

Системний Herkos ([ADR 0013](../docs/adr/0013-herkos-rust.md)) у [`rs/`](rs/): `herkos-core` (запит дії, маніфести, оцінка ризику, правила Keros, без вводу-виводу) і `herkosd` (сокет, протокол, шлюз рішень, дозволи, журнал Mitos, клієнт MCP, перевірка Keros моделлю). Кроки 1–5 готові: `herkosd` ухвалює ті самі рішення, що й Herkos-lite, з тими самими маніфестами, `config.toml`, `permissions.json` і журналом (`herkos journal verify` перевіряє журнал `herkosd`, і навпаки: `herkosd journal verify`), сам запускає інструменти «файли» й «копії» (`vilna-mcp-files`, `vilna-mcp-backup` з `PATH`), а Keros знаходить те саме, що й у Python (`conformance/test_keros_rules.py` порівнює їх на всьому наборі текстів). Модель Keros `herkosd` питає лише через `http://` на петльовій адресі. Питає людину так само, як Herkos-lite: у вікні `herkos-dialog` (з `PATH`), якщо є робочий стіл, інакше у своєму терміналі (`--confirm auto|gui|terminal|deny`, `--confirm-command ШЛЯХ`; протокол вікна — [`docs/protocol.md`](docs/protocol.md), розд. 3). Текст у терміналі `herkosd` поки лише англійською. Далі: системна служба (крок 6), де агент сеансу реєструється в Herkos за своїм uid.

```sh
cd herkos/rs
cargo test
cargo run -p herkosd -- serve --confirm deny
```
