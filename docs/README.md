# Документація

Загальна документація Vilna. Документація окремих компонентів живе в їхніх папках (наприклад, [`../herkos/docs/`](../herkos/docs/)).

Архітектурні рішення записуємо як ADR у [`adr/`](adr/).

## Документи

- [`getting-started.uk.md`](getting-started.uk.md) — як зібрати й запустити Gloxi 0.1 preview з коду ([English](getting-started.md), [Español](getting-started.es.md)).
- [`roadmap.md`](roadmap.md) — дорожня карта: етапи, віхи, залежності, ризики; перший випуск — Gloxi для Linux у січні 2027.
- [`benchmarks.md`](benchmarks.md) — заміри локальної моделі на 8 і 16 ГБ RAM і вибір моделі за замовчуванням.
- [`adr/`](adr/) — архітектурні рішення; повний перелік зі станами — у [`adr/README.md`](adr/README.md).

## Дизайн

- [`design/brief.md`](design/brief.md) — бриф візуального дизайну: настрій, палітра, шрифти, петриківка, анімації.
- [`design/logo/`](design/logo/) — логотип Vilna (SVG: кольоровий, для темного тла, монохромний, значок).
- [`design/icons/`](design/icons/) — значки програм Vilna у стилі GNOME і їхні symbolic-версії.
- Шпалери лежать у пакеті [`../obs/packages/vilna-backgrounds/`](../obs/packages/vilna-backgrounds/).

## Документація компонентів

- Herkos: [`permissions.md`](../herkos/docs/permissions.md) (модель дозволів), [`protocol.md`](../herkos/docs/protocol.md), [`devices.md`](../herkos/docs/devices.md), [`keros-bench.md`](../herkos/docs/keros-bench.md).
- Gloxi: [`service-protocol.md`](../gloxi/docs/service-protocol.md).
- Образ і установник: [`composition.md`](../image/docs/composition.md), [`installer.md`](../image/docs/installer.md).
- Копії: [`design.md`](../backup/docs/design.md).
- Сервер звітів: [`protocol.md`](../reports/docs/protocol.md), [`deploy.md`](../reports/docs/deploy.md), [`operations.md`](../reports/docs/operations.md).
