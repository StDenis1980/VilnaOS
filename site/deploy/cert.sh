#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Gets or renews the certificate for vilnaos.com and www.vilnaos.com on the
# spare host (ADR 0032), with lego and a DNS-01 challenge at deSEC. The DNS
# challenge works while the names still point at statichost, so the spare
# host is ready before it is needed. Run by vilna-site-cert.timer, as root.
#
# _acme-challenge.vilnaos.com is a CNAME to a small zone of its own at deSEC
# (site/dns/vilnaos.com.toml); lego follows it. The token in
# /etc/vilna-site/desec_token may only write that zone.
set -eu

dir=/var/lib/vilna-site/lego
out=/etc/caddy/certs
token_file=${CREDENTIALS_DIRECTORY:-/etc/vilna-site}/desec_token

if [ ! -s "$token_file" ]; then
	echo "No deSEC token in $token_file; see site/deploy/README.md." >&2
	exit 1
fi
DESEC_TOKEN=$(cat "$token_file")
export DESEC_TOKEN
# Older lego releases only follow the CNAME when asked to.
export LEGO_EXPERIMENTAL_CNAME_SUPPORT=true
# deSEC takes a minute or so to serve a new record on all its servers.
export DESEC_PROPAGATION_TIMEOUT=600

set -- --accept-tos --email postmaster@vilnaos.com --dns desec \
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
