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
RemainAfterExit=yes
ExecStart=/bin/sh -c '[ -z "$$(tail -c1 /etc/kernel/cmdline)" ] || echo >>/etc/kernel/cmdline'

[Install]
WantedBy=sysinit.target
UNIT
systemctl enable vilna-kernel-cmdline.service

# KIWI boots the first time from its own entry ("Vilna [ OEM ]",
# loader/entries/vilna-*-default.conf with the kernel in /boot/efi/os).
# It is not a snapshot entry: it stays next to the sdbootutil entries
# and boots whatever snapshot is the btrfs default. On the first boot
# sdbootutil writes the snapshot entries and makes the current snapshot
# the default; only then do KIWI's entry and files go (checked in the
# VM: transactional-update afterwards adds its own entry and keeps this
# one). Runs once: /boot/efi/os is gone afterwards.
install -d /usr/libexec/vilna
cat >/usr/libexec/vilna/boot-entries <<'SCRIPT'
#!/bin/sh
set -eu
snap=$(findmnt -no OPTIONS / | sed -n 's|.*subvol=/@/.snapshots/\([0-9]*\)/snapshot.*|\1|p')
if [ -z "$snap" ]; then
    echo "root is not a snapshot, leaving the boot entries alone" >&2
    exit 0
fi
sdbootutil add-all-kernels
sdbootutil set-default-snapshot "$snap"
# Drop KIWI's entry only when sdbootutil has written one of its own.
if ! ls /boot/efi/loader/entries/*.conf 2>/dev/null | grep -qv '/vilna-[^/]*-default\.conf$'; then
    echo "no sdbootutil entry found, keeping the KIWI entry" >&2
    exit 1
fi
rm -f /boot/efi/loader/entries/vilna-*-default.conf
rm -r -f /boot/efi/os
SCRIPT
chmod 0755 /usr/libexec/vilna/boot-entries
cat >/usr/lib/systemd/system/vilna-boot-entries.service <<'UNIT'
[Unit]
Description=Replace the KIWI boot entry with sdbootutil snapshot entries
ConditionPathExists=/boot/efi/os
RequiresMountsFor=/boot/efi
After=local-fs.target vilna-kernel-cmdline.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/libexec/vilna/boot-entries

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable vilna-boot-entries.service

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
