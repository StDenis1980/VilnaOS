#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs the extension inside a headless GNOME Shell (46 or newer) and checks
# it with the helper extension vilna-test@vilna.test.
#
#   tests/shell/run.sh        # user session: password fields in the Shell
#                             # and in a GTK window, lock screen, Gloxi above
#                             # other windows, the Gloxi strip, D-Bus
#   tests/shell/run.sh gdm    # login screen (needs root: fakes localed)
#
# Needs gnome-shell, gjs, the GTK 4 typelib, dbus-run-session and a system
# bus. Settings use the keyfile backend in a throwaway home.
set -euo pipefail

mode=${1:-user}
here=$(cd "$(dirname "$0")" && pwd)
extension=$(cd "$here/../../vilna@stdenis1980.github.io" && pwd)
work=$(mktemp -d)
cleanup() {
    [[ -n ${localed:-} ]] && kill "$localed" 2>/dev/null || true
    rm -rf -- "$work"
}
trap cleanup EXIT

extensions=$work/home/.local/share/gnome-shell/extensions
mkdir -p "$extensions"
cp -r "$extension" "$here/vilna-test@vilna.test" "$extensions/"
glib-compile-schemas --strict "$extensions/vilna@stdenis1980.github.io/schemas"

if [[ $mode == gdm ]]; then
    gjs -m "$here/localed.js" >"$work/localed.log" 2>&1 &
    localed=$!
    sleep 1
    grep -q owned "$work/localed.log" || { echo "could not fake localed (run as root)" >&2; exit 2; }
fi

export HOME=$work/home GSETTINGS_BACKEND=keyfile VILNA_TEST_OUT=$work/result.txt
export VILNA_TEST_MODE=$mode VILNA_TEST_HERE=$here
dbus-run-session -- bash -c '
    set -u
    export XDG_RUNTIME_DIR=$(mktemp -d)
    gsettings set org.gnome.shell disable-user-extensions false
    gsettings set org.gnome.shell enabled-extensions "[\"vilna@stdenis1980.github.io\", \"vilna-test@vilna.test\"]"
    gsettings set org.gnome.shell welcome-dialog-last-shown-version "999"
    args=(--headless --wayland --no-x11 --virtual-monitor 1920x1080)
    [[ $VILNA_TEST_MODE == gdm ]] && args+=(--mode=gdm)
    gnome-shell "${args[@]}" >"$HOME/shell.log" 2>&1 &
    shell=$!
    windows=no
    for _ in $(seq 900); do
        grep -qs "^END" "$VILNA_TEST_OUT" && break
        # The helper asks for the windows once the Shell-only checks are done.
        if [[ $windows == no && -e $VILNA_TEST_OUT.windows ]]; then
            windows=yes
            for app in io.github.stdenis1980.Gloxi:plain io.github.stdenis1980.Gloxi.HerkosDialog:plain org.example.Other:password; do
                WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland GSK_RENDERER=cairo \
                    gjs -m "$VILNA_TEST_HERE/window.js" "${app%%:*}" "${app##*:}" >/dev/null 2>&1 &
            done
        fi
        sleep 0.1
    done
    kill "$shell" 2>/dev/null; wait "$shell" 2>/dev/null
    true
'

if ! grep -qs "^END" "$VILNA_TEST_OUT"; then
    echo "no result from the Shell; its log:" >&2
    tail -n 60 "$HOME/shell.log" >&2
    exit 1
fi
grep -v "^END" "$VILNA_TEST_OUT"
if grep -q "^FAIL" "$VILNA_TEST_OUT"; then
    grep -E "JS ERROR|vilna" "$HOME/shell.log" >&2 || true
    exit 1
fi
echo "all passed ($mode)"
