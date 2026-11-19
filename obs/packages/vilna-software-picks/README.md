# vilna-software-picks — «Рекомендовано Vilna»

Добірка вільних програм з Flathub, яку GNOME Software («Програмне забезпечення») показує окремим блоком на головній сторінці. Рішення і критерії — [ADR 0012](../../../docs/adr/0012-recommended-apps-and-install-via-gloxi.md).

## Що в пакеті

| Файл у системі | Звідки | Що робить |
| --- | --- | --- |
| `/usr/share/swcatalog/xml/vilna-software-picks.xml` | `build_picks.py build` з `picks.toml` | Метадані AppStream з `merge="append"`: додають до опису програми з Flathub три позначки (нижче) |
| `/usr/share/gnome-software/deployment-featured.ini` | те саме | Заголовок блоку «Рекомендовано Vilna» 10 мовами (ADR 0009) і селектор `vilna` |

Позначки в метаданих:

- `GnomeSoftware::DeploymentFeatured` = `vilna` — програма може потрапити в блок «Рекомендовано Vilna» на головній сторінці. GNOME Software щоразу бере 12 випадкових програм з добірки; якщо доступних менше 12, блок не показується зовсім.
- категорія `Featured` — програму виділено на сторінці її категорії («Створювати», «Працювати», «Грати»…).
- `Vilna::Pick` = розділ добірки — наша власна позначка. Її читає `vilna-mcp-apps` ([`mcp/apps`](../../../mcp/apps/README.md)): пошук Gloxi ставить програми з добірки першими, а діалог Herkos перед встановленням показує, чи програма входить у «Рекомендовано Vilna» (ADR 0012 пп. 5–6).

Програми, що стоять у Vilna за замовчуванням (Firefox, Tinta Office, KeePassXC, базові GNOME), теж у добірці, але лише з `Vilna::Pick`: так Gloxi знає про них і може поставити знову після видалення, а плитки на головній сторінці дістаються програмам, яких у людини ще немає.

Це механізм «Deployment Featured Apps» з документації GNOME Software (`doc/vendor-customisation.md`). Сам GNOME Software ми не змінюємо, а блок «Вибір редакції» (`GnomeSoftware::popular`, список GNOME з пакета `gnome-app-list`) лишається як є.

## Як додати чи прибрати програму

1. Перевірте програму за критеріями ADR 0012: вільна ліцензія (OSI або FSF); розробник підтверджений на Flathub (verified) або це наша програма (виняток — збірка спільноти Flathub, `source = "flathub-community"`, лише окремим рішенням, і `why` каже, що збірка неофіційна; зараз Signal, [ADR 0019](../../../docs/adr/0019-messengers.md), і 8 відомих програм, доповнення [ADR 0012](../../../docs/adr/0012-recommended-apps-and-install-via-gloxi.md) від 2026-11-13); працює в GNOME на Wayland; не надсилає дані без згоди людини; є переклад хоча б кількома з 10 основних мов. Програм, які замінює Tinta Office (LibreOffice, Thunderbird, Papers, GNOME Calendar і Contacts, ADR 0011), у добірці немає.
2. Додайте блок `[[app]]` у [`picks.toml`](picks.toml) у потрібний розділ.
3. Перевірте:

   ```sh
   python3 obs/packages/vilna-software-picks/build_picks.py --defaults image/packages.yaml check
   python3 tools/test_software_picks.py
   ```

   Перевірка не пропустить: повтор, невідомий розділ, невільну ліцензію, програму, яку замінює Tinta, менше 12 програм для головної сторінки, менше 30 чи більше 60 програм, програму за замовчуванням з `image/packages.yaml` поза добіркою.
4. Збільште `Version` у spec і додайте запис у `.changes`. Список переглядаємо перед кожним випуском Vilna.

Перевірка не ходить у мережу, тож статус verified і ліцензію на Flathub людина звіряє сама на сторінці програми (`https://flathub.org/apps/<id>`).

## Як подивитися на Aeon чи Tumbleweed з GNOME

```sh
python3 obs/packages/vilna-software-picks/build_picks.py build /tmp/picks
sudo install -D -m 0644 /tmp/picks/vilna-software-picks.xml /var/lib/swcatalog/xml/vilna-software-picks.xml
sudo install -D -m 0644 /tmp/picks/deployment-featured.ini /etc/xdg/gnome-software/deployment-featured.ini
gnome-software --quit; gnome-software
```

На Aeon `/usr` лише для читання, тому тут `/var/lib/swcatalog` і `/etc/xdg` (обидва GNOME Software теж читає; `/usr/local` для каталогу AppStream він не читає). Прибрати — видалити ці два файли й перезапустити GNOME Software.
