#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# KIWI runs this inside the disk image after the system is copied onto
# it, before the root snapshot is made read-only.
set -euxo pipefail

# /etc as a writable btrfs subvolume nested in the root snapshot
# (@/.snapshots/1/snapshot/etc), as on Aeon and MicroOS: the root is
# read-only, and the SELinux relabel, systemd-tmpfiles and
# gnome-initial-setup must write /etc on first boot (VM test of
# 2026-11-05). transactional-update carries the nested subvolume into
# each new snapshot. Inode 256 means /etc already is a subvolume.
if [ "$(stat -c %i /etc)" != 256 ]; then
    mv /etc /etc.kiwi
    btrfs subvolume create /etc
    cp -a /etc.kiwi/. /etc/
    rm -rf /etc.kiwi
fi

# The root is mounted read-only ("ro" in fstab), and a nested subvolume
# is not a mount of its own, so /etc needs a mount to be writable. As in
# openSUSE's read-only-root-fs (/usr/libexec/setup-etc-subvol), bind
# /etc onto itself: it is always the etc of the snapshot that booted, so
# a transactional-update brings its own /etc. A subvol= path would pin
# /etc to snapshot 1, as the VM test of Build6.2 showed. writable-etc
# (dracut) and systemd-remount-fs make the bind writable; x-initrd.mount
# mounts it before switch-root. KIWI wrote fstab before this script.
if ! awk '$2 == "/etc" { found = 1 } END { exit !found }' /etc/fstab; then
    echo "/etc /etc none bind,x-initrd.mount 0 0" >>/etc/fstab
fi

# The EFI partition only for root (bootctl warns about a world-readable
# random-seed). efimountoptions is gone from the KIWI schema on
# Tumbleweed, so set it here.
sed -i -E '/[[:space:]]\/boot\/efi[[:space:]]+vfat[[:space:]]/ { /umask=/! s/^([^[:space:]]+[[:space:]]+[^[:space:]]+[[:space:]]+[^[:space:]]+[[:space:]]+)([^[:space:]]+)/\1\2,umask=0077/ }' /etc/fstab

# With a disk.sh, KIWI leaves the SELinux labels to it: label the disk
# as KIWI would (the first boot relabels again, config.sh asks for it).
contexts=/etc/selinux/targeted/contexts/files/file_contexts
if [ -e "$contexts" ] && command -v setfiles >/dev/null; then
    setfiles -F -e /dev -e /proc -e /sys -e /run "$contexts" /
fi

exit 0
