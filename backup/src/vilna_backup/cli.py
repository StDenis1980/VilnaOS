# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line of Vilna Backup.

For the person (R3-R4, in a terminal; later the Herkos window):

    vilna-backup setup PLACE          new backups at a place; shows the recovery key once
    vilna-backup recover PLACE        a new device: open old backups with the recovery key
    vilna-backup restore --in-place   bring the home folder back as it was
    vilna-backup secrets on|off       system secrets in backups (separate consent)

For Gloxi through Herkos and the MCP server ``mcp/backup`` (R0-R2), with ``--json``:

    vilna-backup status | overview | snapshots
    vilna-backup set [--preset P] [--add DIR] [--remove DIR] [--history yes|no] [--limit-gb N]
    vilna-backup schedule hourly|daily|weekly|off
    vilna-backup start                start one backup in the background
    vilna-backup restore --target DIR [--snapshot ID] [--path REL]

For systemd timers: ``vilna-backup run --scheduled`` and ``vilna-backup check --auto``.

PLACE is an absolute folder (a USB disk), ``sftp:user@host:/path``,
``s3:https://host/bucket/path`` or ``rest:https://host/user/``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from vilna_backup import __version__, keys
from vilna_backup.restic import ResticError
from vilna_backup.service import Backup, BackupError
from vilna_backup.settings import PRESETS, SCHEDULES, SettingsError, parse_destination

GB = 1000**3


def _out(args: argparse.Namespace, data: Any, text: str | None = None) -> None:
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(text if text is not None else json.dumps(data, ensure_ascii=False, indent=2))


def _human(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} B"


def _ask_credentials(kind: str) -> dict[str, str]:
    names = keys.CREDENTIALS.get(kind)
    if not names:
        return {}
    user = input(f"{names[0]}: ").strip()
    secret = getpass.getpass(f"{names[1]}: ")
    return {names[0]: user, names[1]: secret} if user else {}


def _setup(b: Backup, args: argparse.Namespace) -> int:
    dest = parse_destination(args.place)
    creds = _ask_credentials(dest.kind)
    recovery = b.set_up(dest, creds)
    print(
        "\nКлюч відновлення / Recovery key — запишіть його на аркуш "
        "і зберігайте поза комп'ютером.\n"
        "Без нього копії не відновити, якщо цей пристрій буде втрачено. Ми його не знаємо.\n"
    )
    print(f"    {recovery}\n")
    for _ in range(3):
        typed = input("Введіть ключ з аркуша, щоб підтвердити / Type it back from the sheet: ")
        try:
            b.confirm_recovery(typed)
        except BackupError as e:
            print(f"  {e}")
            continue
        _turn_on(b)
        return 0
    print(
        "\nКлюч не підтверджено: копії вимкнені. Запустіть `vilna-backup confirm`, коли аркуш "
        "буде поруч. / Not confirmed: backups stay off.",
        file=sys.stderr,
    )
    return 1


def _confirm(b: Backup, args: argparse.Namespace) -> int:
    b.confirm_recovery(getpass.getpass("Ключ відновлення / Recovery key: "))
    _turn_on(b)
    return 0


def _turn_on(b: Backup) -> None:
    """After the sheet is confirmed: the timer, and the first backup right away (2a, step 8)."""
    print("\nКопії увімкнено. / Backups are on.")
    print(f"Розклад / Schedule: {b.set_schedule(b.settings.schedule)['note']}")
    started = b.start()
    if started.get("started"):
        print("Перша копія почалася. / The first backup has started: vilna-backup status")


def _recover(b: Backup, args: argparse.Namespace) -> int:
    dest = parse_destination(args.place)
    creds = _ask_credentials(dest.kind)
    typed = getpass.getpass("Ключ відновлення / Recovery key: ")
    b.recover(dest, typed, creds)
    print("Пристрій підключено до копій. / This device can now use the backups.")
    print("Далі: `vilna-backup restore --in-place`, щоб повернути дані.")
    return 0


def _status(b: Backup, args: argparse.Namespace) -> int:
    st = b.status()
    last = st["last_backup"] or {}
    text = (
        f"enabled: {st['enabled']}  place: {st['destination'] or '-'}\n"
        f"what: {st['preset']}  +{len(st['include'])} -{len(st['exclude'])}  "
        f"limit: {_human(st['limit_bytes'])}  schedule: {st['schedule']}\n"
        f"last backup: {'ok' if last.get('ok') else last.get('message', '-')}"
    )
    if st["running"]:
        text += f"\nrunning: {st['running']['percent']}%"
    _out(args, st, text)
    return 0


def _overview(b: Backup, args: argparse.Namespace) -> int:
    data = b.overview()
    lines = [f"{g['group']:<10} {_human(g['bytes'])}" for g in data["groups"]]
    cur = data["current"]
    lines.append(f"chosen: {_human(cur['bytes'])}, fits: {cur['fits']}")
    for p in cur.get("suggest_exclude", []):
        lines.append(f"  leave out: {p}")
    _out(args, data, "\n".join(lines))
    return 0


def _snapshots(b: Backup, args: argparse.Namespace) -> int:
    snaps = b.snapshots()
    text = "\n".join(f"{s['id']}  {s['time'][:19]}  {_human(s['bytes'])}" for s in snaps)
    _out(args, {"snapshots": snaps}, text or "no backups yet")
    return 0


def _set(b: Backup, args: argparse.Namespace) -> int:
    limit: Any = False
    if args.limit_gb is not None:
        limit = None if args.limit_gb == 0 else int(args.limit_gb * GB)
    history = None if args.history is None else args.history == "yes"
    s = b.set_paths(
        preset=args.preset,
        add=args.add or (),
        remove=args.remove or (),
        gloxi_history=history,
        limit_bytes=limit,
        by_assistant=True,
    )
    _out(
        args,
        {
            "preset": s.preset,
            "include": list(s.include),
            "exclude": list(s.exclude),
            "gloxi_history": s.gloxi_history,
            "limit_bytes": s.limit_bytes,
        },
    )
    return 0


def _secrets(b: Backup, args: argparse.Namespace) -> int:
    if args.json or not sys.stdin.isatty():
        print("vilna-backup: the person decides this in a terminal", file=sys.stderr)
        return 1
    on = args.state == "on"
    if on:
        answer = input(
            "Зберігати в копіях паролі Wi-Fi, облікові записи, ключі SSH і GPG? [так/ні] "
        )
        if answer.strip().lower() not in ("так", "yes", "y", "т"):
            return 1
    b.set_paths(system_secrets=on, by_assistant=False)
    print("system secrets in backups:", "on" if on else "off")
    return 0


def _schedule(b: Backup, args: argparse.Namespace) -> int:
    _out(args, b.set_schedule(args.when))
    return 0


def _start(b: Backup, args: argparse.Namespace) -> int:
    _out(args, b.start())
    return 0


def _run(b: Backup, args: argparse.Namespace) -> int:
    rec = b.run(scheduled=args.scheduled)
    _out(args, rec, f"{rec.get('message')} ({rec.get('snapshot', '-')})")
    return 0 if rec.get("ok") else 1


def _check(b: Backup, args: argparse.Namespace) -> int:
    rec = b.check(auto=args.auto, read_data=args.read_data)
    _out(args, rec, rec.get("message") or rec.get("skipped"))
    return 0 if rec.get("ok") else 1


def _restore(b: Backup, args: argparse.Namespace) -> int:
    if args.in_place:
        if args.json or not sys.stdin.isatty():
            print("vilna-backup: restoring over files is confirmed by the person", file=sys.stderr)
            return 1

        def confirm(replaced: list[str], new: int) -> bool:
            print(f"Буде повернуто {new} файлів і замінено {len(replaced)}:")
            for item in replaced[:20]:
                print(f"  {item}")
            if len(replaced) > 20:
                print(f"  ... ще {len(replaced) - 20}")
            return input("Продовжити? [так/ні] ").strip().lower() in ("так", "yes", "y", "т")

        _out(args, b.restore_in_place(snapshot=args.snapshot, confirm=confirm))
        return 0
    if not args.target:
        print("vilna-backup: give --target DIR (a new folder) or --in-place", file=sys.stderr)
        return 2
    _out(args, b.restore_to_new_folder(args.target, snapshot=args.snapshot, path=args.path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vilna-backup", description="Encrypted backups of Vilna")
    p.add_argument("--version", action="version", version=f"vilna-backup {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="new backups at a place (shows the recovery key once)")
    s.add_argument("place")
    s.set_defaults(func=_setup)
    sub.add_parser("confirm", help="confirm the recovery sheet").set_defaults(func=_confirm)
    s = sub.add_parser("recover", help="use existing backups on this new device")
    s.add_argument("place")
    s.set_defaults(func=_recover)

    sub.add_parser("status").set_defaults(func=_status)
    sub.add_parser("overview", help="sizes of what could be backed up").set_defaults(func=_overview)
    sub.add_parser("snapshots", help="list backups").set_defaults(func=_snapshots)

    s = sub.add_parser("set", help="what to back up and how much")
    s.add_argument("--preset", choices=PRESETS)
    s.add_argument("--add", action="append", metavar="DIR")
    s.add_argument("--remove", action="append", metavar="DIR")
    s.add_argument("--history", choices=["yes", "no"], help="Gloxi conversation history")
    s.add_argument("--limit-gb", type=float, help="0 for no limit")
    s.set_defaults(func=_set)
    s = sub.add_parser("secrets", help="system secrets in backups (the person only)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=_secrets)
    s = sub.add_parser("schedule")
    s.add_argument("when", choices=SCHEDULES)
    s.set_defaults(func=_schedule)

    sub.add_parser("start", help="start one backup in the background").set_defaults(func=_start)
    s = sub.add_parser("run", help="make one backup now and wait")
    s.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=_run)
    s = sub.add_parser("check", help="check the backups")
    s.add_argument("--auto", action="store_true", help="read back 5%% once a month")
    s.add_argument("--read-data", action="store_true")
    s.set_defaults(func=_check)

    s = sub.add_parser("restore")
    s.add_argument("--snapshot", default="latest")
    s.add_argument("--target", help="a new, empty folder")
    s.add_argument("--path", help="only this file or folder (relative to the home folder)")
    s.add_argument("--in-place", action="store_true", help="over the home folder (asks first)")
    s.set_defaults(func=_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(Backup.from_env(), args)
    except (BackupError, ResticError, SettingsError, keys.KeysError) as e:
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print(f"vilna-backup: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return 130
