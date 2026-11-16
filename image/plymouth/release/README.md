# Шар випуску на екрані завантаження

Знак і назва поточного випуску Vilna, дрібно внизу кожного екрана Plymouth: завантаження, пароль диска, вимкнення, перезапуск. Зараз це **Blavet** (рибалочка визирає з дупла).

Шар лежить окремо, щоб наступний випуск змінив лише його:

| Що | Де |
| --- | --- |
| Знак для темного тла, квадратний SVG | [`docs/design/blavet/svg/blavet-mark-dark-bg.svg`](../../../docs/design/blavet/svg/blavet-mark-dark-bg.svg), Дмитро Сізьмін, CC-BY-SA-4.0. У тарбол пакета він потрапляє як `release/mark.svg` (`obs/tools/obs_sync.py`) |
| Назва випуску | рядок `RELEASE_NAME` на початку [`../vilna.script`](../vilna.script); має збігатися з `VERSION_CODENAME` у [`os-release`](../../../obs/packages/Vilna-release/os-release) |

`make_theme.py` малює зі знака файл `release-mark.png` (256 px). Інший знак можна дати через `--release-mark`. Без знака тема працює без шару випуску.
