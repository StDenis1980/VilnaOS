#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs Vilna's default Flatpak apps, system-wide, from two remotes:
#   flathub - Firefox, the GNOME basics, KeePassXC (flatpaks.list);
#   vilna   - Vilna's own signed repository (ADR 0024): Tinta Office
#             (flatpaks-vilna.list, ADR 0011). Its runtime comes from Flathub.
# With each remote it also fetches that remote's app catalogue, so the store
# (GNOME Software) shows every app at once and can install it again.
# Run by vilna-flatpak-defaults.service, in the background (it must not hold
# the boot). Each remote that succeeds gets its own stamp and is not tried
# again; when both are done the final stamp stops the service for good.
# Exit codes:
#   0  - done, or the Vilna repository file is not in this build yet (Tinta
#        waits for a package update; the next boot tries again);
#   75 - offline or a remote failed: not an error, the service runs the
#        script again, 30 seconds later at first, then up to every 15 minutes.
set -uo pipefail

SHARE=${VILNA_FLATPAK_SHARE:-/usr/share/vilna}
STATE=${VILNA_FLATPAK_STATE:-/var/lib/vilna}
STAMP=${VILNA_FLATPAK_STAMP:-$STATE/flatpak-defaults-all.done}
# Before Tinta Office (package 0.1) one stamp covered Flathub alone.
OLD_STAMP=$STATE/flatpak-defaults.done
FLATPAK=${VILNA_FLATPAK_BIN:-flatpak}
# Translations Flatpak installs with each app and runtime (their .Locale
# extensions): the main languages of ADR 0009, Valencian with ca. Without
# this Flatpak takes only the system language of this first boot, en_US,
# and Firefox stayed in English after the person chose Українська (VM test
# of Build15.6).
LANGUAGES=${VILNA_FLATPAK_LANGUAGES:-uk;en;es;pl;de;fr;it;ro;cs;lt;ca;gl;eu;pt}
NM_ONLINE=${VILNA_FLATPAK_NM_ONLINE:-nm-online}
TEMPFAIL=75

# One application id per word; anything else (an option such as --user, a
# typo) is left out with a word in the log, never passed to flatpak install.
read_list() {
    local line word words
    while IFS= read -r line || [ -n "$line" ]; do
        read -ra words <<< "${line%%#*}"
        for word in "${words[@]}"; do
            if [[ $word =~ ^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$ ]]; then
                printf '%s\n' "$word"
            else
                echo "$1: '$word' is not an application id, left out" >&2
            fi
        done
    done < "$1"
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
            return 2
        fi
        "$FLATPAK" config --system --set languages "$LANGUAGES" || return 1
        "$FLATPAK" remote-add --system --if-not-exists "$remote" "$repofile" || return 1
        # The remote's app catalogue (its AppStream branch): what GNOME
        # Software and Gloxi (vilna-mcp-apps) read to show an app and to
        # install it again after the person removed it. flatpak install does
        # not fetch it, and until GNOME Software refreshed on its own the
        # store did not know Tinta Office. Not fatal: the store fetches it
        # itself later.
        "$FLATPAK" update --system --noninteractive --appstream "$remote" \
            || echo "$remote: app catalogue not fetched yet, the store fetches it later"
        "$FLATPAK" install --system --noninteractive --or-update "$remote" "${apps[@]}" || return 1
    else
        echo "$remote: no apps in $list"
    fi
    mkdir -p "$STATE" && touch "$done"
}

if [ -e "$OLD_STAMP" ] && [ ! -e "$STATE/flatpak-defaults-flathub.done" ]; then
    touch "$STATE/flatpak-defaults-flathub.done"
fi

# Wait for the network here, not in the unit (see the .service). nm-online
# returns at once when NetworkManager is not running; then flatpak decides.
if command -v "$NM_ONLINE" >/dev/null 2>&1 && ! "$NM_ONLINE" -q -x -t 300; then
    echo "offline, trying again later"
    exit "$TEMPFAIL"
fi

failed=0
waiting=0
install_from flathub https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$SHARE/flatpaks.list" || failed=1

vilna_repo=""
[ -r "$SHARE/vilna.flatpakrepo" ] && vilna_repo="$SHARE/vilna.flatpakrepo"
install_from vilna "$vilna_repo" "$SHARE/flatpaks-vilna.list"
case $? in
    0) ;;
    2) waiting=1 ;;
    *) failed=1 ;;
esac

if [ "$failed" -ne 0 ]; then
    exit "$TEMPFAIL"
fi
if [ "$waiting" -ne 0 ]; then
    exit 0
fi
mkdir -p "$(dirname "$STAMP")"
touch "$STAMP"
