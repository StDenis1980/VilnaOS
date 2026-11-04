#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs Vilna's default Flatpak apps from Flathub, system-wide.
# Run once by vilna-flatpak-defaults.service; on success it leaves a stamp
# so it does not run again. On failure (no network, Flathub down) it
# exits non-zero and the service tries again on the next boot.
set -euo pipefail

LIST=${VILNA_FLATPAK_LIST:-/usr/share/vilna/flatpaks.list}
STAMP=${VILNA_FLATPAK_STAMP:-/var/lib/vilna/flatpak-defaults.done}

mapfile -t apps < <(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$LIST" | grep -v '^$' || true)
if [ "${#apps[@]}" -eq 0 ]; then
    echo "no apps in $LIST"
else
    flatpak remote-add --system --if-not-exists flathub \
        https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --system --noninteractive --or-update flathub "${apps[@]}"
fi

mkdir -p "$(dirname "$STAMP")"
touch "$STAMP"
