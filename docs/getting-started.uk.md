# Перший запуск: зібрати й запустити Gloxi 0.1 preview

[English](getting-started.md) · **Українська** · [Español](getting-started.es.md)

Ця інструкція запускає Gloxi з коду на вашому комп'ютері з Linux. Вона для тестувальників і розробників: Gloxi 0.1 preview ще не випущено, пакета чи Flatpak для встановлення поки немає. Посібник користувача з'явиться ближче до випуску.

Що буде в кінці:

1. Локальна модель на вашому комп'ютері через llama.cpp. Нічого не залишає комп'ютер.
2. Чат із Gloxi у терміналі і, за бажанням, у вікні GTK.
3. За бажанням — Gloxi працює з файлами в `~/Documents` через Herkos-lite, який питає дозволу і веде журнал.
4. За бажанням — хмарна модель із вашим власним ключем; особисті дані маскуються, перш ніж залишити комп'ютер.

## 1. Що потрібно

- Linux з GNOME. Gloxi робимо насамперед для openSUSE Aeon; Fedora, Ubuntu та інші дистрибутиви з Python 3.11 або новішим теж мають працювати. Чат у терміналі працює і без GNOME.
- Щонайменше 8 ГБ RAM, радимо 16 ГБ. Близько 3 ГБ вільного місця для моделі.
- 64-бітний процесор Intel чи AMD; відеокарта не потрібна.
- `git`, Python 3.11 або новіший і [uv](https://docs.astral.sh/uv/).

`gloxi check` (або `flatpak run --command=gloxi io.github.stdenis1980.Gloxi check`) каже, чи вистачить цього комп'ютера для локальної моделі, і друкує короткий опис заліза для форми відгуку, без нічого особистого.

**На openSUSE Aeon** система незмінна, тож усе, що нижче, робіть у контейнері [Distrobox](https://distrobox.it/), який є в Aeon:

```sh
distrobox create --name gloxi-dev
distrobox enter gloxi-dev
```

Контейнер бачить вашу домашню папку, тож файли й завантажена модель лишаються між сеансами.

Встановіть `uv`, якщо його ще немає:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Отримати код

```sh
git clone https://github.com/StDenis1980/VilnaOS.git
cd VilnaOS
```

## 3. Запустити локальну модель

Gloxi говорить з моделлю через OpenAI-сумісний API. Для розробки використовуємо `llama-server` із [llama.cpp](https://github.com/ggml-org/llama.cpp) ([ADR 0004](adr/0004-local-model-engine.md)). Отримати його можна так:

- завантажити готовий архів для Linux зі сторінки [випусків llama.cpp](https://github.com/ggml-org/llama.cpp/releases) і розпакувати;
- `brew install llama.cpp`, якщо у вас є Homebrew;
- зібрати з коду: `cmake -B build && cmake --build build --config Release -j`, тоді програма лежить у `build/bin/llama-server`.

Запустіть модель за замовчуванням, Qwen3 4B у Q4_K_M з вимкненими «роздумами» ([ADR 0008](adr/0008-default-local-model.md)):

```sh
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8080 -c 4096 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}'
```

- Перший запуск завантажує ≈2,5 ГБ з Hugging Face у `~/.cache/llama.cpp`. Далі працює без мережі. Якщо файл `.gguf` уже є, вкажіть `-m /шлях/до/Qwen3-4B-Q4_K_M.gguf` замість `-hf ...`.
- `--jinja` вмикає шаблон чату, що знає про інструменти; у нових збірках він увімкнений за замовчуванням. Без нього Gloxi не зможе працювати з файлами.
- Не вимикайте «роздуми» через `--reasoning-budget 0`: у деяких збірках це ламає розбір відповіді. Використовуйте `enable_thinking`, як вище.
- Не закривайте цей термінал. Сервер слухає `http://127.0.0.1:8080/v1` — це і є типова адреса Gloxi.

Для слабшого комп'ютера є Qwen2.5 1.5B Instruct (`-hf Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`, ≈1 ГБ): швидша, але помітно слабша. Підійде й будь-який інший OpenAI-сумісний сервер, наприклад Ollama: запускайте Gloxi з `--url http://127.0.0.1:11434/v1 --model <назва моделі>`.

## 4. Чат у терміналі

У другому терміналі:

```sh
cd VilnaOS/gloxi
uv sync
uv run gloxi ask "Що вмієш?"
uv run gloxi
```

`uv sync` створює віртуальне середовище в `gloxi/.venv` з Gloxi, шлюзом приватності, Herkos-lite і сервером інструменту «файли».

`uv run gloxi` відкриває інтерактивний чат. Перед кожною відповіддю видно, звідки вона, наприклад `[local]`. Команди:

| Команда | Що робить |
| --- | --- |
| `/reset` | Почати нову розмову |
| `/local <текст>`, `/cloud <текст>` | Надіслати одне повідомлення локальній або хмарній моделі |
| `/exit` | Вийти |

## 5. Вікно GTK (за бажанням)

Вікну потрібні PyGObject, GTK 4 і libadwaita із системи, тому воно працює в окремому віртуальному середовищі, яке бачить системні пакети. Спершу встановіть їх:

```sh
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Ubuntu, Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# openSUSE Tumbleweed (типовий образ Distrobox на Aeon)
sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1
```

Потім у папці `gloxi/`:

```sh
python3 -m venv --system-site-packages .venv-gtk
.venv-gtk/bin/pip install -e ../privacy-gateway -e ../mcp/files -e ../herkos -e ".[tools]"
.venv-gtk/bin/gloxi-gtk
```

Локальні пакети треба перелічити явно: pip не читає налаштувань uv і шукав би їх на PyPI.

У заголовку вікна видно, чи ввімкнена хмара. Над кожною відповіддю написано, де її отримано: «На цьому комп'ютері» або «Хмара: host, з маскуванням». Кнопка «+» починає нову розмову. Інтерфейс показується мовою системи: українською або англійською, інші мови додаємо поступово.

## 6. Файли через Herkos (за бажанням)

Сам Gloxi файлів не торкається. Він просить Herkos-lite, а той перевіряє рівень ризику, за потреби питає вас, запускає інструмент «файли» і записує кожну дію в журнал. За замовчуванням дозволено лише `~/Documents`.

Запустіть Herkos у третьому терміналі, з папки `gloxi/`:

```sh
uv run herkos serve --confirm terminal      # питає дозволу в цьому терміналі
# або, із середовищем GTK з кроку 5, у вікні:
.venv-gtk/bin/herkos serve
```

Потім знову запустіть Gloxi (`uv run gloxi` або `.venv-gtk/bin/gloxi-gtk`) і попросіть, наприклад, «Які файли в моїй папці Documents?» чи «Створи в Documents notes.txt зі списком покупок». Кожен виклик видно в чаті, наприклад `[files: read ~/Documents/plan.txt (done)]`.

Варто знати:

- Видаляти файли Gloxi 0.1 не може: видалення — рівень ризику R3, а Herkos-lite виконує лише R0–R2.
- Після того як Gloxi прочитав чи пошукав файли, у розмові є чужий текст, тож Herkos не дає змінювати файли до нової розмови (`/reset` або «+»).
- Для кожної зміни 30 днів зберігається резервна копія. Щоб скасувати зміну, знайдіть її id у журналі й виконайте `uv run herkos undo <action_id>`.
- Інші корисні команди: `uv run herkos journal show`, `uv run herkos journal verify`, `uv run herkos permissions list`.
- Дозволені папки та інші налаштування — у `~/.config/herkos/config.toml`, наприклад `allowed_roots = ["~/Documents", "~/Desktop"]`.
- Якщо Herkos не запущено, Gloxi просто розмовляє. `GLOXI_TOOLS=0` вимикає інструмент «файли».

Herkos-lite працює від вашого ж користувача і поки нічого не ізолює; ізоляція з'явиться із системним Herkos в образі Vilna.

## 7. Хмарна модель із власним ключем (за бажанням)

За замовчуванням нічого не залишає комп'ютер. Щоб Gloxi міг відправляти довгі чи складні запити в хмару, вкажіть будь-якого OpenAI-сумісного провайдера у `~/.config/gloxi/config.toml`:

```toml
[cloud]
url = "https://api.mistral.ai/v1"
model = "mistral-small-latest"

[router]
cloud_allowed = true
```

а ключ покладіть у змінну середовища, а не у файл:

```sh
export GLOXI_CLOUD_API_KEY=...
```

У режимі `mode = "auto"` короткі повідомлення лишаються локально, а довші за 1500 символів ідуть у хмару; `/cloud <текст>` свідомо відправляє туди одне повідомлення. Перед відправкою [шлюз приватності](../privacy-gateway/README.md) замінює пошту, телефони, IBAN, номери карток і документів, ключі й паролі на `[EMAIL_1]` тощо, а у відповідь повертає справжні значення. Імена у вільному тексті шлюз поки не розпізнає. Паролі й ключі, написані в чаті, Gloxi ховає від усіх моделей, і від локальної теж, а якщо модель їх повторить, показує `••••••`. Хмарна модель ніколи не отримує інструмента «файли». Усі налаштування й змінні середовища — у [`gloxi/README.md`](../gloxi/README.md#налаштування).

## 8. Тести й перевірки

Кожен компонент має власні тести, справжня модель їм не потрібна:

```sh
cd gloxi            # або herkos, privacy-gateway, mcp/files
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

У корені репозиторію `python3 tools/check_spdx.py` перевіряє позначки ліцензій. CI запускає ті самі команди для кожного pull request.

Як заміряти, наскільки модель справляється з 30 тестовими командами Gloxi, — у [`benchmarks.md`](benchmarks.md).

## 9. Типові проблеми

| Що бачите | Що робити |
| --- | --- |
| Gloxi пише, що не може дістатися локальної моделі | Перевірте, що `llama-server` запущено й він слухає порт 8080: `curl http://127.0.0.1:8080/v1/models`. Для іншої адреси — `--url` або `GLOXI_LOCAL_URL` |
| Перша відповідь дуже довга | Модель завантажується або ще качається; дивіться термінал `llama-server`. На 8 ГБ спробуйте меншу модель із кроку 3 |
| Модель довго «думає» перед відповіддю | `llama-server` запущено без `--chat-template-kwargs '{"enable_thinking":false}'` |
| Gloxi не користується файлами | Herkos не запущено, або `llama-server` старої збірки запущено без `--jinja` |
| `herkos: vilna-mcp-files is not installed; no files tool` | Запускайте Herkos з папки `gloxi/` після `uv sync` або встановіть `../mcp/files` у середовище GTK, як у кроці 5 |
| `XDG_RUNTIME_DIR is not set` | Herkos потребує сеансу робочого столу, який задає цю змінну. Через ssh спершу увійдіть у робочий стіл або працюйте без Herkos |
| `gloxi-gtk` падає з `No module named 'gi'` | Середовище GTK створено без `--system-site-packages`, або PyGObject не встановлено в системі (крок 5) |

Знайшли щось інше? Створіть issue.
