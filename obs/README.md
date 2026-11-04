# Пакети Vilna в Open Build Service

Тут живе все, що потрібно для проєкту Vilna на [build.opensuse.org](https://build.opensuse.org) (OBS): опис проєкту і RPM-пакети. Склад пакетів і причини — [`../image/docs/composition.md`](../image/docs/composition.md).

## Що тут є

| Шлях | Що це |
| --- | --- |
| `project/_meta` | Опис OBS-проєкту: репозиторій `openSUSE_Tumbleweed` (збірка проти `openSUSE:Factory/snapshot`), x86_64, публікація ввімкнена. `@PROJECT@` і `@USER@` підставляє інструмент |
| `project/_prjconf` | Конфігурація проєкту; поки порожня, усе береться з Factory |
| `packages/<назва>/` | Один пакет OBS: spec, `.changes` і джерела. Файли вивантажуються в OBS як є |
| `tools/obs_sync.py` | Збирає пакети з репозиторію (`assemble`) і вивантажує їх в OBS (`upload`) |
| [`../.github/workflows/obs.yml`](../.github/workflows/obs.yml) | CI: збирає всі RPM у контейнері Tumbleweed на кожному PR, а на `main` вивантажує в OBS |

### Перші пакети

| Пакет | Що містить | Замінює в Aeon |
| --- | --- | --- |
| `Vilna-release` | `/usr/lib/os-release` (`NAME="Vilna"`, `ID=vilna`, `ID_LIKE="opensuse-tumbleweed opensuse suse"`, `LOGO=vilna-logo`) | `Aeon-release` (через `Provides: distribution-release`) |
| `distribution-logos-Vilna` | Іконка `vilna-logo` (звичайна й symbolic) і `/usr/share/pixmaps/distribution-logos/`. **Логотип — тимчасова заглушка** до готового логотипа | `distribution-logos-openSUSE-Aeon` |
| `vilna-presets` | `50-vilna.preset`: avahi, samba, openssh вимкнені за замовчуванням; `herkos` і `gloxi` увімкнені | Поки що **доповнює** пресети Aeon, а не замінює їх; згодом стане `systemd-presets-branding-Vilna` (див. нижче) |
| `gloxi` | Gloxi 0.1 preview: команда `gloxi` (чат у терміналі з локальною моделлю). Тарбол робиться з папки [`../gloxi/`](../gloxi/), версія береться з `gloxi/pyproject.toml` | — |
| `python-vilna-privacy-gateway` | Шлюз приватності (`python3-vilna-privacy-gateway`), потрібен Gloxi. Тарбол — з папки [`../privacy-gateway/`](../privacy-gateway/), версія — з її `pyproject.toml` | — |
| `vilna-software-picks` | Добірка «Рекомендовано Vilna» для GNOME Software (ADR 0012): метадані AppStream і заголовок блоку 10 мовами, генеруються під час збірки з [`picks.toml`](packages/vilna-software-picks/picks.toml). Як додати програму — [`README`](packages/vilna-software-picks/README.md) пакета | — |

Пакет `gloxi` збирається з поточного коду `gloxi/`. Коли в Gloxi з'являться нові залежності, системний сервіс чи користувач `gloxi`, треба оновити `packages/gloxi/gloxi.spec`; CI на PR покаже, якщо spec відстав від коду.

## Як це працює

```
GitHub (StDenis1980/VilnaOS)                   build.opensuse.org
┌──────────────────────────────┐   osc commit  ┌──────────────────────────────┐
│ obs/packages/*  + gloxi/     │ ────────────▶ │ home:<логін>:vilna           │
│ GitHub Actions: obs_sync.py  │               │  збирає RPM проти Tumbleweed │
└──────────────────────────────┘               │  публікує репозиторій zypper │
                                               └──────────────────────────────┘
```

Джерела в OBS вивантажує GitHub Actions через `osc`: так один і той самий скрипт збирає пакети і в CI, і для OBS, а spec-файли з вкладеними джерелами (брендування) не потребують окремого git-сервісу. Тепер, коли репозиторій публічний, можна перейти на інтеграцію OBS з GitHub (`_service` з `obs_scm` і `.obs/workflows.yml`): тоді OBS сам забирає код і збирає кожен PR, а пароль у секретах GitHub не потрібен, зате потрібні токени OBS і GitHub. Це окремий крок на потім.

Усе на build.opensuse.org публічне, як і сам репозиторій.

## Що зробити Denys (одноразово)

Цього не можна зробити з репозиторію: потрібен обліковий запис і пароль.

1. **Обліковий запис openSUSE.** На [build.opensuse.org](https://build.opensuse.org) натисніть **Sign Up** і створіть обліковий запис (він спільний для всіх сервісів openSUSE). Запишіть логін — далі він `<логін>`. Особисті дані в профілі можна не заповнювати.
2. **Перший вхід.** Увійдіть на build.opensuse.org. OBS сам створить домашній проєкт `home:<логін>`. Саму підпапку `home:<логін>:vilna` створить інструмент на кроці 4, вручну нічого не потрібно.
3. **Налаштування GitHub.** У репозиторії: **Settings → Secrets and variables → Actions**.
   - вкладка **Variables** → *New repository variable*: `OBS_USER` = `<логін>`, `OBS_PROJECT` = `home:<логін>:vilna`;
   - вкладка **Secrets** → *New repository secret*: `OBS_PASSWORD` = пароль облікового запису openSUSE.
4. **Перше вивантаження.** **Actions → OBS packages → Run workflow** (гілка `main`). Завдання `rpm` збере пакети в CI, завдання `upload` створить проєкт і закомітить пакети. Далі це відбувається саме після кожного злиття в `main`, яке змінює `obs/` або `gloxi/`.
5. **Перевірка.** Відкрийте `https://build.opensuse.org/project/show/home:<логін>:vilna`. Через кілька хвилин усі пакети мають бути зелені (*succeeded*). Якщо щось червоне — клік на статус покаже журнал збірки; надішліть його в тред.

Без змінних із кроку 3 завдання `upload` просто пропускається, тож PR і злиття працюють і без облікового запису.

Пароль у секретах GitHub дає CI повний доступ до облікового запису openSUSE. Для початку цього досить; пізніше можна завести окремий обліковий запис для CI з правами лише на `home:<логін>:vilna`.

### Вручну, без GitHub Actions

Потрібен `osc` (на Windows — через WSL з openSUSE Tumbleweed: `sudo zypper in osc`). З кореня репозиторію:

```sh
osc -A https://api.opensuse.org ls home:<логін>      # osc запитає логін і пароль
python3 obs/tools/obs_sync.py upload --project home:<логін>:vilna --user <логін> --dry-run
python3 obs/tools/obs_sync.py upload --project home:<логін>:vilna --user <логін>
```

## Як поставити пакети

Після успішної збірки OBS публікує репозиторій:

```sh
# Tumbleweed
sudo zypper ar -f https://download.opensuse.org/repositories/home:/<логін>:/vilna/openSUSE_Tumbleweed/ vilna
sudo zypper in gloxi

# Aeon (незмінна система)
sudo transactional-update pkg install gloxi   # потім перезавантаження
```

На своєму робочому Aeon чи Tumbleweed ставте лише `gloxi`. `Vilna-release` і `distribution-logos-Vilna` конфліктують з `Aeon-release` / `openSUSE-release` і логотипами openSUSE за задумом: вони для образу Vilna, а не для наявної системи.

## Локальна перевірка

```sh
python3 obs/tools/obs_sync.py assemble build/obs   # усі пакети в build/obs/<назва>/
```

Повна збірка RPM — у CI (`.github/workflows/obs.yml`, завдання `rpm`), бо потрібні макроси openSUSE. Там же працює rpmlint з фільтрами `ci-rpmlintrc` (лише перевірки, які в OBS поводяться інакше). Те саме можна повторити в контейнері `opensuse/tumbleweed`.

## Що далі

- `vilna-presets` → `systemd-presets-branding-Vilna`: скопіювати пресети з `systemd-presets-branding-Aeon`, додати `Provides: systemd-presets-branding` і конфлікт із пакетом Aeon, щоб повністю його замінити (rpmlint вимагає цього від пакетів з `branding` у назві).
- `Vilna-release`: звірити з `Aeon-release.spec` (продукт для zypper у `/etc/products.d`, `/etc/issue`), додати `HOME_URL`, коли буде домен.
- Справжній логотип замість заглушки; решта пакетів `*-branding-Vilna` з [`composition.md`](../image/docs/composition.md), розд. 4.5.
- `patterns-vilna` — коли буде більше пакетів Vilna.
- Репозиторій для `aarch64` і оптимізованої збірки x86-64-v3 — разом з образом на етапі 2.
