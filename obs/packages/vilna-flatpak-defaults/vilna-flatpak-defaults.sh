#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs Vilna's default Flatpak apps, system-wide, from two remotes:
#   flathub - Firefox, the GNOME basics, KeePassXC (flatpaks.list);
#   vilna   - Vilna's own signed repository (ADR 0024): Tinta Office
#             (flatpaks-vilna.list, ADR 0011). Its runtime comes from Flathub.
# Run once by vilna-flatpak-defaults.service. Each remote that succeeds gets
# its own stamp and is not tried again; when both are done the final stamp
# stops the service for good. On a failure (no network, a remote down, the
# Vilna repository file not in this build yet) it exits non-zero and the
# service tries the rest again on the next boot.
set -uo pipefail

SHARE=${VILNA_FLATPAK_SHARE:-/usr/share/vilna}
STATE=${VILNA_FLATPAK_STATE:-/var/lib/vilna}
STAMP=${VILNA_FLATPAK_STAMP:-$STATE/flatpak-defaults-all.done}
# Before Tinta Office (package 0.1) one stamp covered Flathub alone.
OLD_STAMP=$STATE/flatpak-defaults.done
FLATPAK=${VILNA_FLATPAK_BIN:-flatpak}
# Translations Flatpak installs with each app and runtime (their .Locale
# extensions): the ten languages of ADR 0009. Without this Flatpak takes
# only the system language of this first boot, en_US, and Firefox stayed
# in English after the person chose Українська (VM test of Build15.6).
LANGUAGES=${VILNA_FLATPAK_LANGUAGES:-uk;en;es;pl;de;fr;it;ro;cs;lt}

read_list() {
    sed -e 's/#.*//' -e 's/[[:space:]]//g' "$1" | grep -v '^$' || true
}

# install_from REMOTE REPOFILE LIST
install_from() {
    local remote=$1 repofile=$2 list=$3 done="$STATE/flatpak-defaults-$1.done"
    local -a apps
    [ -e "$done" ] && return 0
    if [ ! -r "$list" ]; then
        echo "$remote: no list $list"
        return 1
    fi
    mapfile -t apps < <(read_list "$list")
    if [ "${#apps[@]}" -gt 0 ]; then
        if [ -z "$repofile" ]; then
            echo "$remote: no repository file in this build, ${apps[*]} wait for it"
            return 1
        fi
        "$FLATPAK" config --system --set languages "$LANGUAGES" || return 1
        "$FLATPAK" remote-add --system --if-not-exists "$remote" "$repofile" || return 1
        "$FLATPAK" install --system --noninteractive --or-update "$remote" "${apps[@]}" || return 1
    else
        echo "$remote: no apps in $list"
    fi
    mkdir -p "$STATE" && touch "$done"
}

if [ -e "$OLD_STAMP" ] && [ ! -e "$STATE/flatpak-defaults-flathub.done" ]; then
    touch "$STATE/flatpak-defaults-flathub.done"
fi

failed=0
install_from flathub https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$SHARE/flatpaks.list" || failed=1

vilna_repo=""
[ -r "$SHARE/vilna.flatpakrepo" ] && vilna_repo="$SHARE/vilna.flatpakrepo"
install_from vilna "$vilna_repo" "$SHARE/flatpaks-vilna.list" || failed=1

if [ "$failed" -ne 0 ]; then
    exit 1
fi
mkdir -p "$(dirname "$STAMP")"
touch "$STAMP"
