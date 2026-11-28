#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Gets or renews the certificate for vilnaos.com and www.vilnaos.com on the
# spare host (ADR 0032), with lego and an HTTP-01 challenge. lego writes the
# challenge file to $webroot, which Caddy serves on http://acme.vilnaos.com
# (acme.caddy). While the names still point at statichost, its _redirects
# rule sends /.well-known/acme-challenge/* there, so the spare host is ready
# before it is needed; after a failover Let's Encrypt reaches it directly.
# No DNS token on the server. Run by vilna-site-cert.timer, as root.
set -eu

dir=/var/lib/vilna-site/lego
out=/etc/caddy/certs
webroot=/var/www/acme-challenge

install -d -m 0755 "$webroot"
set -- --accept-tos --email postmaster@vilnaos.com --http --http.webroot "$webroot" \
	--domains vilnaos.com --domains www.vilnaos.com --path "$dir"
if [ -s "$dir/certificates/vilnaos.com.crt" ]; then
	lego "$@" renew --days 30
else
	lego "$@" run
fi

install -d -m 0750 -g caddy "$out"
new_crt=$dir/certificates/vilnaos.com.crt
if ! cmp -s "$new_crt" "$out/vilnaos.com.crt"; then
	install -m 0640 -g caddy "$dir/certificates/vilnaos.com.key" "$out/vilnaos.com.key"
	install -m 0644 "$new_crt" "$out/vilnaos.com.crt"
	if [ -s /etc/caddy/sites/vilnaos.com.caddy ]; then
		systemctl reload caddy
	else
		# First certificate: let update.sh write the site block now.
		/usr/local/lib/vilna-site/update.sh
	fi
fi
