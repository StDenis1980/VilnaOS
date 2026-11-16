# Шар випуску на екрані завантаження

Знак і назва поточного випуску Vilna, дрібно внизу кожного екрана Plymouth: завантаження, пароль диска, вимкнення, перезапуск. Зараз це **Blavet** (рибалочка визирає з дупла).

Шар лежить окремо, щоб наступний випуск змінив лише його:

| Що | Де |
| --- | --- |
| Знак для темного тла, квадратний SVG | `mark.svg` (зараз копія `installer/src/vilna_installer/data/blavet-mark-dark.svg`) |
| Назва випуску | рядок `RELEASE_NAME` на початку [`../vilna.script`](../vilna.script); має збігатися з `VERSION_CODENAME` у [`os-release`](../../../obs/packages/Vilna-release/os-release) |

`make_theme.py` малює з `mark.svg` файл `release-mark.png` (256 px). Якщо `mark.svg` немає, тема працює без шару випуску.
