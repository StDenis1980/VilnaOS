#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Sets up the spare copy of vilnaos.com on the reports VPS (ADR 0032). Run as
# root from a checkout of VilnaOS, after reports/deploy/install.sh:
#     sh site/deploy/install.sh
# Safe to run again. See site/deploy/README.md.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
	echo "Run as root." >&2
	exit 1
fi
if [ ! -f /etc/caddy/Caddyfile ] || ! grep -q 'import sites/\*.caddy' /etc/caddy/Caddyfile; then
	echo "Install reports first (reports/deploy/install.sh): its Caddyfile imports sites/*.caddy." >&2
	exit 1
fi

apt-get install -y --no-install-recommends git lego python3

install -d -m 0755 /usr/local/lib/vilna-site /etc/caddy/sites /var/www/vilnaos.com
install -d -m 0700 /etc/vilna-site /var/lib/vilna-site
install -m 0755 "$here/update.sh" /usr/local/lib/vilna-site/update.sh
install -m 0755 "$here/cert.sh" /usr/local/lib/vilna-site/cert.sh
for unit in vilna-site-update.service vilna-site-update.timer vilna-site-cert.service vilna-site-cert.timer; do
	install -m 0644 "$here/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

/usr/local/lib/vilna-site/update.sh
systemctl enable --now vilna-site-update.timer vilna-site-cert.timer

if [ -s /etc/vilna-site/desec_token ]; then
	systemctl start vilna-site-cert.service
	echo "Certificate: systemctl status vilna-site-cert.service"
else
	echo "Next: put the deSEC token for the ACME zone in /etc/vilna-site/desec_token"
	echo "(install -m 0600 /dev/stdin /etc/vilna-site/desec_token), then"
	echo "systemctl start vilna-site-cert.service."
fi
