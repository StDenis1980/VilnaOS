# Запасна копія vilnaos.com на VPS

Другий хост сайту ([ADR 0032](../../docs/adr/0032-site-hosts-dns.md)): той самий VPS IONOS, що й `reports.vilnaos.com` ([`reports/docs/deploy.md`](../../reports/docs/deploy.md)), той самий Caddy без журналів доступу.

| Файл | Що робить |
| --- | --- |
| [`update.sh`](update.sh) + `vilna-site-update.timer` | раз на 10 хвилин бере `main` з GitHub у `/opt/vilna-site`, перевіряє сайт `tools/check_site.py` і лише тоді перемикає `/var/www/vilnaos.com/current` на нову копію (попередня лишається для відкату) |
| [`caddy_site.py`](caddy_site.py) | пише блок Caddy `/etc/caddy/sites/vilnaos.com.caddy` з `site/public/_headers` і `_redirects`: ті самі заголовки безпеки, переадресація `/en/`, сторінка 404 кожною мовою, `www` → корінь |
| [`cert.sh`](cert.sh) + `vilna-site-cert.timer` | сертифікат Let's Encrypt для `vilnaos.com` і `www` через `lego` і DNS-01 у deSEC, щодня перевіряє, чи пора продовжити |
| [`install.sh`](install.sh) | ставить усе це на сервер |

Сервер нічого не отримує з GitHub, крім публічного коду: ключів до сервера в GitHub немає.

## Установлення (Denys або хтось із доступом SSH)

Спершу DNS, кроки 1–6 у [`site/dns/README.md`](../dns/README.md): сертифікат потребує CNAME `_acme-challenge`, а він з'являється лише в deSEC і bunny.

```sh
ssh root@217.160.125.31 -i ~/.ssh/denis-agentique-win
cd /opt/VilnaOS && git pull
sh reports/deploy/install.sh          # новий Caddyfile з рядком import sites/*.caddy
sh site/deploy/install.sh
install -m 0600 /dev/stdin /etc/vilna-site/desec_token   # токен vps-acme, Ctrl+D
systemctl start vilna-site-cert.service
journalctl -u vilna-site-cert -u vilna-site-update --since -10min
```

Перевірка з будь-якого комп'ютера, поки DNS ще веде на statichost:

```sh
curl -sI --resolve vilnaos.com:443:217.160.125.31 https://vilnaos.com/ | grep -i -E "^(HTTP|content-security|strict-transport)"
curl -sI --resolve vilnaos.com:443:217.160.125.31 https://vilnaos.com/uk/nema/ | head -1   # 404
curl -sI --resolve www.vilnaos.com:443:217.160.125.31 https://www.vilnaos.com/ | grep -i location
python3 tools/site_dns.py probe
```

## Обмеження

- Лише IPv4: у VPS немає IPv6. Поки сайт на запасному хості, AAAA для кореня немає.
- Сайт і сервер звітів на одній машині: якщо одночасно ляжуть statichost і VPS, сайту не буде.
- Caddy з Debian 13 (2.6): `handle_errors` віддає сторінку 404 на будь-яку помилку, з її власним кодом.
