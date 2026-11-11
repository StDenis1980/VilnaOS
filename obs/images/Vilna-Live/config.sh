#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# KIWI runs this inside the live image root after all packages are installed.
set -euxo pipefail

test -f /.kconfig && . /.kconfig
test -f /.profile && . /.profile

echo "Configure image: [${kiwi_iname:-Vilna-Live}]"

# Services: the Vilna presets, as in the disk image (obs/images/Vilna).
systemctl preset-all
systemctl set-default graphical.target

# What makes no sense on a live system, whose root is a squashfs with a
# RAM overlay, not a btrfs snapshot: repartitioning the disk it booted
# from (Aeon's repart.d), updates and snapshots, btrfs maintenance, the
# boot health check, kdump; and the first-boot Flatpak install, which
# would download the default apps into RAM on every start.
for unit in \
    systemd-repart.service \
    vilna-flatpak-defaults.service \
    transactional-update.timer transactional-update-cleanup.timer \
    snapper-timeline.timer snapper-cleanup.timer snapper-boot.timer \
    btrfs-balance.timer btrfs-scrub.timer btrfs-trim.timer btrfs-defrag.timer \
    health-checker.service kdump.service kdump-early.service \
    rebootmgr.service; do
    systemctl mask "$unit"
done

# Trust the package signing keys shipped in /usr/lib/rpm/gnupg/keys
# (Vilna's and openSUSE's), as the disk image does: the install engine
# will install from this system's repositories.
for key in /usr/lib/rpm/gnupg/keys/gpg-pubkey-*.asc; do
    rpm --import "$key"
done
rpm -q gpg-pubkey-06386694 gpg-pubkey-29b700a4

# The first installer screen stands on the Blavet wallpaper.
test -e /usr/share/backgrounds/vilna/vilna-blavet-light.jpg
test -e /usr/share/backgrounds/vilna/vilna-blavet-dark.jpg
test -x /usr/bin/vilna-installer

echo vilna-live >/etc/hostname

# The live user: no password, logged in by GDM at once. A live system
# has no data of its own to protect, and the installer will need to act
# as root (partitioning, writing the system), so "live" may do so
# without a password, through sudo and polkit, as on other live images.
# None of this is in the installed system: the installer creates the
# person's own account there.
useradd --create-home --comment "Vilna Live" --user-group live
passwd --delete live
install -d -m 0750 /etc/sudoers.d
cat >/etc/sudoers.d/50-vilna-live <<'SUDO'
# Vilna live image only (obs/images/Vilna-Live/config.sh)
live ALL=(ALL:ALL) NOPASSWD: ALL
SUDO
chmod 0440 /etc/sudoers.d/50-vilna-live
cat >/etc/polkit-1/rules.d/49-vilna-live.rules <<'RULES'
// Vilna live image only (obs/images/Vilna-Live/config.sh): the live
// user may do what an administrator may, without a password.
polkit.addRule(function (action, subject) {
    if (subject.user == "live" && subject.local && subject.active) {
        return polkit.Result.YES;
    }
});
RULES
chmod 0644 /etc/polkit-1/rules.d/49-vilna-live.rules

# The installer's conversation with Gloxi talks to the local model as the
# live user: vilna-llm.service (enabled by the Vilna presets above) wants
# its key, which only the group gloxi may read (ADR 0025).
usermod --append --groups gloxi live
test -x /usr/bin/vilna-mcp-installer
# But not at boot: on a live system the model (~2.5 GB) is read from the
# USB stick, and while it loads every other read waits. In the VM test of
# Build2.2 (2026-11-10) gnome-shell hung on the disk for minutes, GDM
# gave up the automatic login ("Session never registered") and showed a
# login screen the passwordless live user cannot pass. Started at login,
# it still kept the installer window away for ~3 minutes (Build3.2). So
# the installer starts it (systemctl, allowed by the polkit rule below)
# once its own window is on the screen (vilna_installer.start.wake_model).
systemctl disable vilna-llm.service
# Nor does Gloxi wake at login (gloxi wake starts gloxid, which wants
# vilna-llm.service and would load the model before the installer is up).
rm -f /etc/xdg/autostart/io.github.stdenis1980.Gloxi-wake.desktop
install -d /etc/systemd/system/vilna-llm.service.d
cat >/etc/systemd/system/vilna-llm.service.d/50-vilna-live.conf <<'LLM'
# Vilna live image only (obs/images/Vilna-Live/config.sh)
[Service]
IOSchedulingClass=idle
Nice=10
LLM

# Plymouth hands the screen over to GDM without falling back to text:
# in the VM test of Build2.2 boot finished after the live session had
# started, and "plymouth quit" put the boot log on the screen over the
# desktop until a VT switch. GDM itself quits Plymouth this way.
install -d /etc/systemd/system/plymouth-quit.service.d
cat >/etc/systemd/system/plymouth-quit.service.d/50-vilna-live.conf <<'PLY'
# Vilna live image only (obs/images/Vilna-Live/config.sh)
[Service]
ExecStart=
ExecStart=-/usr/bin/plymouth quit --retain-splash
PLY

# GDM logs "live" in at once, without GNOME Initial Setup (the installer
# asks the language itself). openSUSE's GDM reads the autologin user
# from /etc/sysconfig/displaymanager; custom.conf says the same for
# upstream GDM.
if [ -e /etc/sysconfig/displaymanager ]; then
    sed -i 's/^DISPLAYMANAGER_AUTOLOGIN=.*/DISPLAYMANAGER_AUTOLOGIN="live"/' /etc/sysconfig/displaymanager
    sed -i 's/^DISPLAYMANAGER_PASSWORD_LESS_LOGIN=.*/DISPLAYMANAGER_PASSWORD_LESS_LOGIN="yes"/' /etc/sysconfig/displaymanager
fi
install -d /etc/gdm
cat >/etc/gdm/custom.conf <<'GDM'
# Vilna live image (obs/images/Vilna-Live/config.sh)
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=live
InitialSetupEnable=False
GDM

# In the live session: the installer starts on the whole screen, and
# stays first in the dash to open it again; the screen never locks (the
# live user has no password to unlock it with).
home=/home/live
install -d -o live -g live "$home/.config" "$home/.config/autostart"
echo yes >"$home/.config/gnome-initial-setup-done"
desktop=/usr/share/applications/io.github.stdenis1980.VilnaInstaller.desktop
test -e "$desktop"
cp "$desktop" "$home/.config/autostart/"
chown -R live:live "$home/.config"
# GNOME opens the Activities overview at login, and the installer sat
# under it as a thumbnail until Esc (VM test of 2026-11-09). A small
# extension, only in this image, starts the session on the desktop.
ext=/usr/share/gnome-shell/extensions/vilna-live@stdenis1980.github.io
install -d "$ext"
cat >"$ext/metadata.json" <<'META'
{
  "uuid": "vilna-live@stdenis1980.github.io",
  "name": "Vilna Live",
  "description": "Vilna live image: start the session on the desktop, with the installer in front, not in the overview.",
  "shell-version": ["46", "47", "48", "49", "50", "51"],
  "url": "https://github.com/StDenis1980/VilnaOS"
}
META
cat >"$ext/extension.js" <<'JS'
// SPDX-License-Identifier: GPL-3.0-or-later
// Vilna live image only (obs/images/Vilna-Live/config.sh).
//
// GNOME Shell 51 runs the startup animation into the overview whenever
// the session mode has an overview (layout.js: "if
// (Main.sessionMode.hasOverview) await
// Main.overview.runStartupAnimation()"), and has no startInOverview to
// turn it off (VM test 2 of 2026-11-09). So: no overview while the
// shell starts up, the overview back once it has, and, if the overview
// is shown all the same in the first seconds, hide it.
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const GRACE_SECONDS = 15;

export default class VilnaLiveExtension extends Extension {
    enable() {
        if (Main.layoutManager._startingUp) {
            Main.sessionMode.hasOverview = false;
            this._startup = Main.layoutManager.connect('startup-complete', () => {
                Main.sessionMode.hasOverview = true;
                this._disconnectStartup();
            });
        }
        this._hide();
        this._shown = Main.overview.connect('shown', () => this._hide());
        this._grace = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, GRACE_SECONDS, () => {
            this._grace = 0;
            this._disconnectShown();
            return GLib.SOURCE_REMOVE;
        });
    }

    disable() {
        if (this._startup)
            Main.sessionMode.hasOverview = true;
        this._disconnectStartup();
        this._disconnectShown();
        if (this._grace) {
            GLib.source_remove(this._grace);
            this._grace = 0;
        }
    }

    _hide() {
        if (Main.overview.visible)
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                Main.overview.hide();
                return GLib.SOURCE_REMOVE;
            });
    }

    _disconnectStartup() {
        if (this._startup) {
            Main.layoutManager.disconnect(this._startup);
            this._startup = null;
        }
    }

    _disconnectShown() {
        if (this._shown) {
            Main.overview.disconnect(this._shown);
            this._shown = null;
        }
    }
}
JS

cat >/usr/share/glib-2.0/schemas/40_Vilna-Live.gschema.override <<'GSCHEMA'
# Vilna live image only (obs/images/Vilna-Live/config.sh); wins over
# 30_Vilna.gschema.override.
[org.gnome.shell]
enabled-extensions=['vilna@stdenis1980.github.io', 'vilna-live@stdenis1980.github.io']
favorite-apps=['io.github.stdenis1980.VilnaInstaller.desktop', 'io.github.stdenis1980.Gloxi.desktop', 'org.gnome.Nautilus.desktop']

[org.gnome.desktop.screensaver]
lock-enabled=false

[org.gnome.desktop.session]
idle-delay=uint32 0
GSCHEMA
glib-compile-schemas /usr/share/glib-2.0/schemas

# zypper installs only what is required, as in the disk image.
if [ -e /etc/zypp/zypp.conf ]; then
    sed -i 's/^#\? *solver.onlyRequires.*/solver.onlyRequires = true/' /etc/zypp/zypp.conf
fi

exit 0
