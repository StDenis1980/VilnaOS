#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# KIWI runs this inside the image root after all packages are installed.
set -euxo pipefail

test -f /.kconfig && . /.kconfig
test -f /.profile && . /.profile

echo "Configure image: [${kiwi_iname:-Vilna}]"

# Services: the presets decide (vilna-presets, 50-vilna.preset, wins over
# the openSUSE and Aeon presets): avahi, samba and openssh stay off,
# herkos, gloxi and the first-boot Flatpak install are on.
systemctl preset-all
systemctl set-default graphical.target

# The disk is ready-made by KIWI, not installed by Aeon's tik: if any
# repart.d from Aeon's packages is still present, systemd-repart must
# not try to repartition the disk we booted from (first-boot failure
# seen in the VM test of 2026-11-05).
systemctl mask systemd-repart.service

# SELinux runs enforcing, as in Aeon: label the whole system on first boot.
if [ -e /etc/selinux/config ]; then
    touch /etc/selinux/.autorelabel
fi

# KIWI writes /etc/kernel/cmdline without a final newline, after this
# script and disk.sh. sdbootutil reads it with "while read", which drops
# a last line without a newline: every boot entry made by
# transactional-update lost "security=selinux selinux=1 enforcing=1"
# and SELinux was off (VM test of Build6.2). Add the newline on the
# first boot, before any transactional-update.
cat >/usr/lib/systemd/system/vilna-kernel-cmdline.service <<'UNIT'
[Unit]
Description=End /etc/kernel/cmdline with a newline for sdbootutil
ConditionPathExists=/etc/kernel/cmdline
DefaultDependencies=no
After=local-fs.target
Before=sysinit.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '[ -z "$$(tail -c1 /etc/kernel/cmdline)" ] || echo >>/etc/kernel/cmdline'

[Install]
WantedBy=sysinit.target
UNIT
systemctl enable vilna-kernel-cmdline.service

# zypper (inside transactional-update) installs only what is required,
# as in Aeon: recommended packages do not slip into the system.
if [ -e /etc/zypp/zypp.conf ]; then
    sed -i 's/^#\? *solver.onlyRequires.*/solver.onlyRequires = true/' /etc/zypp/zypp.conf
fi

# Branding check (composition.md, section 4.5): no openSUSE or Aeon
# release, logo or branding package may be left in the image. Warn for
# now, while the *-branding-Vilna packages do not exist yet; this
# becomes an error before the first public image.
leftover=$(rpm -qa --qf '%{NAME}\n' | grep -E '^(Aeon-release|openSUSE-release|distribution-logos-openSUSE.*|.*-branding-(openSUSE|Aeon))$' || true)
if [ -n "$leftover" ]; then
    echo "WARNING: openSUSE/Aeon branding still in the image:"
    echo "$leftover"
fi

exit 0
