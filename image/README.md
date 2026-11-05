# Образ системи

Збірка образу Vilna на базі openSUSE Aeon / Kalpa (MicroOS). Запасний варіант — Fedora Atomic (bootc).

Тут буде:

- опис образу й пакетів поверх базового дистрибутива;
- встановлення Gloxi і Herkos як системних сервісів;
- налаштування знімків btrfs і транзакційних оновлень, на які спирається Mitos;
- перевірка на мінімальному залізі (Intel Core 8 / Ryzen 2000, 8 ГБ RAM).

Стан: визначено склад образу (чернетка) — [`docs/composition.md`](docs/composition.md), список пакетів — [`packages.yaml`](packages.yaml), встановлення в діалозі з Gloxi — [`docs/installer.md`](docs/installer.md). Перший каркас образу (KIWI, диск для віртуальної машини) — [`../obs/images/Vilna/`](../obs/images/Vilna/), опис у [`../obs/README.md`](../obs/README.md#образ).
