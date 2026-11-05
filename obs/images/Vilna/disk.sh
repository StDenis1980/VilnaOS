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
# is not a mount of its own, so /etc must be mounted separately to be
# writable (Build5.1 still booted into emergency mode without it).
# x-initrd.mount mounts it before switch-root, as systemd needs /etc
# early. KIWI wrote fstab before calling this script.
if ! awk '$2 == "/etc" { found = 1 } END { exit !found }' /etc/fstab; then
    root=$(awk '$2 == "/" { print $1 }' /etc/fstab)
    # KIWI's first root snapshot. Each new snapshot needs its own
    # subvol= here; check after a transactional-update in the VM.
    [ -n "$root" ]
    echo "$root /etc btrfs defaults,subvol=/@/.snapshots/1/snapshot/etc,x-initrd.mount 0 0" >>/etc/fstab
fi

# With a disk.sh, KIWI leaves the SELinux labels to it: label the disk
# as KIWI would (the first boot relabels again, config.sh asks for it).
contexts=/etc/selinux/targeted/contexts/files/file_contexts
if [ -e "$contexts" ] && command -v setfiles >/dev/null; then
    setfiles -F -e /dev -e /proc -e /sys -e /run "$contexts" /
fi

exit 0
