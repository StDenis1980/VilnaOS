#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Checks for milestone V2: Vilna installed on a disk, updated and rolled
# back (image/docs/update-rollback.md). Run as root on the installed
# system, in a VM or on a laptop. It never touches the disk layout.
#
#   update-rollback.sh state <name>   save the state of the system to
#                                     ./v2-report/<name>/ (read-only)
#   update-rollback.sh break-next     make a new snapshot with a
#                                     health-checker check that always
#                                     fails: the next boot must roll back
#                                     by itself (scenario S6)
#   update-rollback.sh unbreak        remove that check from the snapshot
#                                     that runs now, if it is there
#
# Nothing here sends anything anywhere; the report stays on this machine.
# Look through it before you copy it off: it lists package versions and
# unit names, no files from home folders and no secrets.

set -u

REPORT_ROOT=${V2_REPORT:-./v2-report}
# health-checker runs every executable plugin in this directory with the
# argument "check"; a non-zero exit marks the boot as failed.
PLUGIN_DIR=/usr/libexec/health-checker
PLUGIN=$PLUGIN_DIR/zz-vilna-v2-test-fail.sh

die() { echo "update-rollback: $*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "run this as root (sudo $0 $*)"; }

# One command's output into one file; a failing command is recorded, not fatal.
save() {
    local file=$1; shift
    { echo "\$ $*"; "$@" 2>&1; echo "[exit $?]"; } >"$DIR/$file"
}

state() {
    local name=${1:-}
    [ -n "$name" ] || die "give the state a name, for example: $0 state 1-first-boot"
    case $name in */*|.*) die "the name must not contain / or start with a dot" ;; esac
    DIR=$REPORT_ROOT/$name
    mkdir -p "$DIR" || die "cannot make $DIR"

    save time.txt date --iso-8601=seconds
    save os-release.txt cat /etc/os-release
    save uname.txt uname -a
    save cmdline.txt cat /proc/cmdline
    save boot-id.txt cat /proc/sys/kernel/random/boot_id
    # Which snapshot runs now and which starts next.
    save root-mount.txt findmnt -no SOURCE,OPTIONS /
    save snapper-root.txt snapper --no-dbus -c root list
    save btrfs-default.txt btrfs subvolume get-default /
    # systemd-boot entries written by sdbootutil, one per snapshot.
    save bootctl.txt bootctl list --no-pager
    save sdbootutil.txt sdbootutil list-entries
    save efibootmgr.txt efibootmgr
    # Updates and the automatic rollback.
    save timers.txt systemctl list-timers --all --no-pager
    save tu-timer.txt systemctl status --no-pager transactional-update.timer
    save tu-last.txt journalctl -b -1 -u transactional-update.service --no-pager -n 200
    save health-checker.txt systemctl status --no-pager health-checker.service
    save health-checker-log.txt journalctl -b -u health-checker.service --no-pager
    save health-checker-state.txt cat /var/lib/misc/health-checker.state
    # Disk unlock (TPM2 token or passphrase); no key material is printed.
    for dev in $(lsblk -rno PATH,FSTYPE | awk '$2 == "crypto_LUKS" {print $1}'); do
        save "luks-$(basename "$dev").txt" cryptsetup luksDump --dump-json-metadata "$dev"
    done
    save tpm.txt systemd-cryptenroll --tpm2-device=list
    # Our packages and the services that make Vilna Vilna.
    save rpm-vilna.txt sh -c "rpm -qa --qf '%{NAME} %{VERSION}-%{RELEASE}\n' | grep -Ei 'vilna|gloxi|herkos|patterns-vilna|kernel-default|systemd-boot|sdbootutil|transactional-update|health-checker' | sort"
    save failed-units.txt systemctl --failed --no-pager
    save vilna-units.txt systemctl status --no-pager herkosd.socket vilna-llm.service vilna-people-sync.service vilna-netguard.service
    save selinux.txt getenforce
    save df.txt df -h / /home /var
    save btrfs-usage.txt btrfs filesystem usage -T /
    echo "Saved to $DIR"
}

break_next() {
    # The check goes into a new snapshot only; the one that runs now stays
    # good, so health-checker can return to it.
    transactional-update --non-interactive run /bin/sh -c "
        mkdir -p $PLUGIN_DIR &&
        printf '%s\n' '#!/bin/sh' \
            '# Vilna V2 test (image/tests/update-rollback.sh): always fails.' \
            'case \"\$1\" in check) echo \"V2 test: this snapshot is broken on purpose\" >&2; exit 1 ;; *) exit 0 ;; esac' \
            > $PLUGIN && chmod 0755 $PLUGIN" ||
        die "transactional-update could not make the test snapshot"
    echo "The next snapshot has the failing check. Reboot: health-checker should"
    echo "mark it bad, roll back and reboot into the snapshot that runs now."
    echo "Afterwards run: $0 state 6-after-auto-rollback"
}

unbreak() {
    if [ -e "$PLUGIN" ]; then
        transactional-update --non-interactive run rm -f "$PLUGIN" ||
            die "transactional-update could not remove the test check"
        echo "Removed in a new snapshot; it is gone after the next reboot."
    else
        echo "The snapshot that runs now has no test check."
    fi
}

case ${1:-} in
    state) need_root "$@"; state "${2:-}" ;;
    break-next) need_root "$@"; break_next ;;
    unbreak) need_root "$@"; unbreak ;;
    *) sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
