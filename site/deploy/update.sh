#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Brings the spare copy of vilnaos.com on the VPS up to date with main
# (ADR 0032). Run by vilna-site-update.timer every 10 minutes, as root.
# Nothing is pushed to the server: it pulls the public repository itself,
# checks the site and only then switches to the new copy.
set -eu

repo=https://github.com/StDenis1980/VilnaOS.git
src=/opt/vilna-site
www=/var/www/vilnaos.com
caddy_site=/etc/caddy/sites/vilnaos.com.caddy
cert=/etc/caddy/certs/vilnaos.com.crt

if [ ! -d "$src/.git" ]; then
	git clone --quiet --depth 1 --filter=blob:none --sparse "$repo" "$src"
	git -C "$src" sparse-checkout set site tools
fi
git -C "$src" fetch --quiet --depth 1 origin main
rev=$(git -C "$src" rev-parse FETCH_HEAD)
if [ "$(cat "$www/current/.revision" 2>/dev/null || true)" = "$rev" ] && [ -s "$caddy_site" ]; then
	exit 0
fi
git -C "$src" checkout --quiet --force "$rev"

# The same check CI runs before statichost publishes: no script, no foreign
# resource, every page in every language, public/ matches site/src.
python3 "$src/tools/check_site.py" "$src/site/public"

install -d -m 0755 "$www"
new="$www/$rev"
rm -rf "$new"
cp -a "$src/site/public" "$new"
rm -f "$new/_headers" "$new/_redirects"
echo "$rev" >"$new/.revision"
ln -sfn "$new" "$www/current.new"
mv -T "$www/current.new" "$www/current"
# Keep the previous copy for a quick rollback, drop older ones.
ls -1dt "$www"/*/ 2>/dev/null | grep -v '/current/$' | tail -n +3 | xargs -r rm -rf

# The site block needs the certificate; until cert.sh has one, the copy
# waits on disk and Caddy keeps serving reports.vilnaos.com only.
if [ -s "$cert" ]; then
	tmp=$(mktemp)
	python3 "$src/site/deploy/caddy_site.py" "$src/site/public" >"$tmp"
	if ! cmp -s "$tmp" "$caddy_site"; then
		install -d -m 0755 /etc/caddy/sites
		cp "$caddy_site" "$caddy_site.old" 2>/dev/null || true
		install -m 0644 "$tmp" "$caddy_site"
		if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
			systemctl reload caddy
		else
			echo "New Caddy site block does not validate; keeping the old one." >&2
			if [ -s "$caddy_site.old" ]; then mv "$caddy_site.old" "$caddy_site"; else rm -f "$caddy_site"; fi
			rm -f "$tmp"
			exit 1
		fi
	fi
	rm -f "$tmp" "$caddy_site.old"
fi
echo "vilnaos.com spare copy at $rev."
