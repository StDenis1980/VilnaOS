# Тема значків Vilna

Тема `Vilna` для GNOME (пакет `vilna-icon-theme`, рішення Denys 23.11.2026: «теки + symbolic Vilna»). Свої значки малюємо лише там, де їх видно найбільше і де вони про Vilna. Усе інше тема бере з Adwaita (`Inherits=Adwaita,hicolor`), тож жоден значок GNOME не зникає.

| Що | Значки | Тека |
|---|---|---|
| Теки | `folder`, `user-home`, `user-desktop`, `folder-documents`, `folder-download`, `folder-music`, `folder-pictures`, `folder-videos`, `folder-templates`, `folder-publicshare` | `scalable/places` |
| Кошик | `user-trash`, `user-trash-full` | `scalable/places` |
| Диски | `drive-harddisk`, `drive-removable-media` | `scalable/devices` |
| Gloxi і Herkos (symbolic) | `vilna-masked`, `vilna-local`, `vilna-cloud`, `vilna-risk-0` … `vilna-risk-4` | `symbolic/status` |
| | `vilna-consent`, `vilna-key`, `vilna-model`, `vilna-mic` | `symbolic/actions` |

Стиль тек: задня стінка нічна синя `#1F345C`, аркуш кремовий `#F2E8D5`, передня стінка — градієнт синього `#5B8CFF → #2D6BFF`, по верхньому краю золота смуга `#F5C24A → #F0963A`, емблема кремова. Symbolic-значки мають 16×16 і один колір, як в Adwaita: GNOME сам перефарбовує їх під світлу й темну тему. Рівні ризику: R0 — порожній щит, R1–R3 — одна, дві, три крапки, R4 — повний щит.

## Як змінити

Значки малює код, руками SVG не правимо:

```sh
cd docs/design/icons/system
python3 src/make_icons.py Vilna     # перемалювати тему
python3 src/check_icons.py Vilna    # те саме, що %check пакета
```

`check_icons.py` перевіряє, що закомічені файли збігаються з генератором, `index.theme` перелічує всі теки й успадковує Adwaita, а symbolic-значки мають один колір. Він працює в `%check` пакета, тож CI пакетів OBS ловить розбіжності.

Після зміни підніміть `VERSION` і додайте запис у `obs/packages/vilna-icon-theme/vilna-icon-theme.changes`.

## Ліцензія і чистота

Усе намальовано в коді з нуля з простих фігур, GPL-3.0-or-later. Нічого не взято з Adwaita, Yaru, Papirus чи інших тем, і жоден значок не обведено з чужого. Якщо Дмитро Сізьмін доведе теки до чистового вигляду, його файли підуть під CC-BY-SA-4.0 і в перелік у `LICENSING.md`.

## Що далі

- Перевірити у VM: Файли, док, діалог вибору файлу, світла й темна тема.
- Підключити symbolic-значки в Gloxi і Herkos замість тимчасових значків GNOME (чип Herkos, картки дій, перемикач моделі).
- Дмитро: затвердити або доробити стиль тек.
