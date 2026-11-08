# Екран завантаження Vilna (Plymouth)

Тема Plymouth `vilna`: те, що людина бачить між увімкненням комп'ютера і екраном входу. Пакет — [`plymouth-branding-Vilna`](../../obs/packages/plymouth-branding-Vilna/), у образі вона тема за замовчуванням (`<bootsplash-theme>vilna</bootsplash-theme>` у [`Vilna.kiwi`](../../obs/images/Vilna/Vilna.kiwi)).

## Як це виглядає

На нічному синьому тлі (`#1F345C`, донизу темніше) зі світінням синього `#2D6BFF` за логотипом ([бриф](../../docs/design/brief.md), розд. 2 і 6):

| Час | Що відбувається |
| --- | --- |
| 0–0,4 с | з'являється синє світіння |
| 0,2–1,4 с | мазок пензля замикається в коло (логотип для темного тла) |
| 1,1–2,0 с | ластівка вилітає з кола на своє місце |
| 1,5–3,0 с | у лівому нижньому й правому верхньому кутах виростають стебла з зернятками, квіткою і ягідками в манері петриківки |
| від 2,4 с | три золоті вузли під логотипом пульсують по черзі, поки система стартує |

- **Пароль диска** (коли буде шифрування): підпис над полем із золотою рамкою, крапки замість символів. Питання (`plymouth ask-question`) — у тому самому полі.
- **Повідомлення** (`plymouth display-message`) — внизу екрана.
- **Вимкнення, перезапуск, оновлення** — готова картинка одразу, без анімації появи.
- **Перехід до входу:** GDM сам завершує Plymouth (`plymouth quit --retain-splash`), тож остання картинка лишається на екрані, доки GNOME Shell не намалює екран входу. Перед цим тема зупиняє анімацію і ховає вузли, щоб застиг спокійний кадр, а не середина пульсу.
- **Журнал:** Esc на екрані завантаження показує повідомлення системи, як і в будь-якій темі Plymouth.

Мотиви в кутах — абстрактні мазки, намальовані кодом ([`make_theme.py`](make_theme.py)), а не копія чийогось розпису: справжня петриківка від майстрів за договором прийде пізніше (бриф, розд. 5).

## Файли

| Файл | Що це |
| --- | --- |
| `vilna.plymouth` | Опис теми для Plymouth (модуль `script`) |
| `vilna.script` | Сценарій: розташування, час, пароль, повідомлення, вихід |
| `make_theme.py` | Малює всі PNG із логотипа `docs/design/logo/Vilna_logo_dark-bg.svg`: 30 кадрів кола, ластівку, 24 кадри мотиву, світіння, вузол, поле пароля. Лише стандартна бібліотека Python і `rsvg-convert` |
| `plymouthd.defaults` | Типові налаштування plymouthd: тема `vilna`, `ShowDelay=0`, `DeviceTimeout=8`. Цей файл завжди несе пакет `plymouth-branding`, а наш його замінює. Без файла обидва значення порожні, і plymouthd падає (`Assertion 'seconds > 0.0' failed`): так було в Build11.2 |
| `VERSION` | Версія пакета |

PNG у репозиторії не лежать: пакет малює їх під час збірки RPM, тож нове лого чи зміна мотиву — це зміна тексту, а не бінарних файлів. Тема займає близько 1,2 МБ і потрапляє в initrd (там Plymouth стартує).

## Як подивитися без перезавантаження

Plymouth уміє малювати у вікні X11 (плагін `x11`; в openSUSE він у `plymouth`, у Debian/Ubuntu — пакет `plymouth-x11`, текст — `plymouth-label`).

```sh
python3 image/plymouth/make_theme.py docs/design/logo/Vilna_logo_dark-bg.svg /tmp/vilna-theme
sudo mkdir -p /usr/share/plymouth/themes/vilna
sudo cp /tmp/vilna-theme/*.png image/plymouth/vilna.plymouth image/plymouth/vilna.script /usr/share/plymouth/themes/vilna/
printf '[Daemon]\nTheme=vilna\n' | sudo tee /etc/plymouth/plymouthd.conf   # лише на тестовій машині
sudo plymouthd --no-daemon --debug \
  --kernel-command-line="splash plymouth.ignore-udev plymouth.ignore-serial-consoles" &
sudo plymouth show-splash
sudo plymouth ask-for-password --prompt="Пароль диска"   # поле пароля
sudo plymouth display-message --text="Повідомлення"
sudo plymouth quit
```

У справжньому завантаженні: `sudo plymouth-set-default-theme -R vilna` (перебудовує initrd; на Vilna — усередині `transactional-update shell`).
