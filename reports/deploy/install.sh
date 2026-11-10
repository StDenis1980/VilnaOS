#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs or updates reports.vilnaos.com on a Debian 13 server (run as root
# from a checkout of VilnaOS: sh reports/deploy/install.sh). Safe to run
# again: it keeps the database and existing secrets. See reports/docs/deploy.md.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)

if [ "$(id -u)" -ne 0 ]; then
	echo "Run as root." >&2
	exit 1
fi

apt-get install -y --no-install-recommends caddy build-essential curl ca-certificates unattended-upgrades

# Rust from rustup, for this user only (root), if missing.
if ! command -v cargo >/dev/null 2>&1 && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
PATH="$HOME/.cargo/bin:$PATH"

(cd "$root" && cargo build --release --locked -p vilna-reports-server)
install -m 0755 "$root/target/release/vilna-reports-server" /usr/local/bin/vilna-reports-server

install -d -m 0700 /etc/vilna-reports
if [ ! -s /etc/vilna-reports/admin_key ]; then
	(umask 077 && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >/etc/vilna-reports/admin_key)
	echo "New admin key in /etc/vilna-reports/admin_key (keep it in the password manager)."
fi
if [ ! -s /etc/vilna-reports/github_token ]; then
	(umask 077 && : >/etc/vilna-reports/github_token)
	echo "No GitHub token yet: crashes are counted, no issues are filed."
	echo "Put a fine-grained token (Issues: read and write on VilnaOS only) in /etc/vilna-reports/github_token."
fi

install -m 0644 "$here/vilna-reports.service" /etc/systemd/system/vilna-reports.service
install -d /etc/systemd/journald.conf.d
install -m 0644 "$here/journald.conf" /etc/systemd/journald.conf.d/60-vilna-reports.conf
install -m 0644 "$here/Caddyfile" /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now vilna-reports.service
systemctl restart vilna-reports.service
systemctl reload-or-restart caddy

sleep 1
curl -fsS http://127.0.0.1:8087/healthz >/dev/null && echo "vilna-reports-server answers on 127.0.0.1:8087."
