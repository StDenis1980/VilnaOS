# Звіти про збої й відгуки Vilna

Серверна частина [ADR 0018](../docs/adr/0018-error-reports-and-feedback.md): `reports.vilnaos.com`.

| Тека | Що | Ліцензія |
| --- | --- | --- |
| [`protocol/`](protocol/) | Формат звіту про збій і відгуку, перевірка полів, повторне очищення, сигнатура. Спільний для сервера й збирача `vilna-reportd` | GPL-3.0-or-later |
| [`server/`](server/) | `vilna-reports-server`: HTTP, SQLite, 90 днів, issue `auto-report` | GPL-3.0-or-later |
| [`deploy/`](deploy/) | systemd-служба, Caddy, журнал на 7 днів, `install.sh` для Debian 13 на Hetzner Cloud | GPL-3.0-or-later |
| `client/` | `vilna-reportd` на пристрої (етап 2) — ще немає | GPL-3.0-or-later |

Документи:

- [`docs/protocol.md`](docs/protocol.md) — точки `/v1/*`, поля, відповіді, ліміти. З ним звіряються Gloxi 0.2, Herkos-lite, `vilna-reportd` і форма на сайті.
- [`docs/deploy.md`](docs/deploy.md) — як підняти сервер у ЄС і що для цього має зробити Denys.
- [`docs/operations.md`](docs/operations.md) — щоденна робота: переглянути збої, схвалити issue, позначити виправлення, видалити відгук.

Що сервер **не** робить: не зберігає IP-адрес і заголовків запиту, не приймає дампів пам'яті, не має постійного ідентифікатора пристрою, нічого не публікує без нашого схвалення.

## Перевірка

```sh
cd reports
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

Локально: `CREDENTIALS_DIRECTORY=<тека з файлом admin_key> VILNA_REPORTS_DB=/tmp/r.sqlite cargo run -p vilna-reports-server`, далі `curl` з прикладів у `docs/protocol.md` на `127.0.0.1:8087`.
