# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run talk about backups (backup/docs/design.md, section 2a).

After the first login Gloxi asks a few questions: whether to keep backups,
what to keep (with the real sizes), Gloxi history, how much space, where,
how often. It is a fixed script, not the model: the questions and choices
must be the same for everyone, and folder names and sizes never reach any
model, local or cloud.

Every change goes through Herkos (tool ``org.vilna.mcp.backup``, R0-R2), on
a connection of its own, so the person sees Herkos's window the first time.
Choosing the place and the recovery sheet are R3: Gloxi does not do them,
it hands the person the exact ``vilna-backup setup`` command instead (later
the Herkos window).

If the person says "later", Gloxi asks once more after a week, then never
again; they can always ask with ``/backup``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from gettext import gettext as _
from pathlib import Path
from typing import Any

BACKUP_TOOL = "org.vilna.mcp.backup"
REMIND_AFTER = 7 * 86400
GB = 1000**3

# Herkos request: (operation, arguments, reason, resources) -> Herkos answer.
Request = Callable[[str, dict, str, list], dict]


class IntroStopped(Exception):
    """Herkos said no or failed: the talk ends, the person can come back later."""


@dataclass(frozen=True)
class Turn:
    text: str
    choices: tuple[str, ...] = ()
    done: bool = False
    # What the window does with this turn: open a web page, start an app
    # (a desktop file id). The terminal prints them instead.
    open_uri: str | None = None
    launch: str | None = None

    def render(self) -> str:
        lines = [self.text]
        lines += [f"  {i}. {c}" for i, c in enumerate(self.choices, start=1)]
        return "\n".join(lines)


def human(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} B"


def state_path() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value) if value and os.path.isabs(value) else Path.home() / ".local/state"
    return base / "gloxi" / "backup-intro.json"


GROUP_NAMES = {
    "settings": lambda: _("Settings, passwords, Gloxi and Herkos"),
    "documents": lambda: _("Documents"),
    "desktop": lambda: _("Desktop"),
    "pictures": lambda: _("Pictures"),
    "videos": lambda: _("Videos"),
    "music": lambda: _("Music"),
    "download": lambda: _("Downloads"),
    "home": lambda: _("Everything in the home folder"),
}

WHERE_COMMANDS = {
    "vilna": None,
    "own": "vilna-backup setup s3:https://<host>/<bucket>/vilna",
    "disk": "vilna-backup setup /run/media/$USER/<disk>/vilna",
    "drive": None,
}


@dataclass
class BackupIntro:
    request: Request
    path: Path = field(default_factory=state_path)
    clock: Callable[[], float] = time.time
    step: str = "idle"
    _groups: list[dict] = field(default_factory=list)
    _custom: list[dict] = field(default_factory=list)
    _suggest: list[str] = field(default_factory=list)
    _preset_label: str = ""
    _size: int | None = None
    _where: str = "disk"
    _secrets: bool = False
    _last: Turn | None = None
    close: Callable[[], None] = lambda: None

    # -- when to ask ----------------------------------------------------------

    def _state(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, status: str) -> None:
        old = self._state()
        data = {"status": status, "time": self.clock()}
        if status == "later" and old.get("status") == "later":
            data["status"] = "declined"  # the reminder was the last time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def due(self) -> bool:
        """Ask now? The first time, and once more a week after "later"."""
        st = self._state()
        if not st:
            return True
        if st.get("status") == "later":
            return self.clock() - float(st.get("time", 0)) >= REMIND_AFTER
        return False

    @property
    def active(self) -> bool:
        return self.step not in ("idle", "end")

    # -- the talk ----------------------------------------------------------------

    def start(self) -> Turn | None:
        """Begin the talk. None if backups are already set up (nothing to ask)."""
        self._last = self._start()
        return self._last

    def _start(self) -> Turn | None:
        try:
            status = self._call("status", {}, _("Gloxi checks whether backups are set up"))
        except IntroStopped as e:
            return self._stop(str(e), save=False)
        if status.get("set_up"):
            self._save("done")
            self.step = "end"
            return None
        self.step = "on"
        return Turn(
            _("Shall I keep backups of your data, so you do not lose it along with the computer?"),
            (_("Yes"), _("Later"), _("I had Vilna before: restore from a backup")),
        )

    def answer(self, text: str) -> Turn:
        handler = getattr(self, f"_on_{self.step}", None)
        if handler is None:
            return Turn(_("We are not talking about backups now."), done=True)
        text = self._as_number(text.strip())
        try:
            turn = handler(text)
        except IntroStopped as e:
            turn = self._stop(str(e))
        if turn.choices or turn.done:
            self._last = turn
        return turn

    def _as_number(self, text: str) -> str:
        """A choice may also be typed as words: "yes", "так", or the start of its label."""
        if not self._last or not self._last.choices or text.isdigit():
            return text
        low = text.casefold()
        target = None
        if low in {"yes", "y", "так", "т", _("yes").casefold()}:
            target = _("Yes")
        elif low in {"no", "n", "ні", "н", _("no").casefold()}:
            target = _("No")
        for i, choice in enumerate(self._last.choices, start=1):
            if target and choice == target:
                return str(i)
        hits = [
            i
            for i, c in enumerate(self._last.choices, start=1)
            if len(low) >= 2 and c.casefold().startswith(low)
        ]
        return str(hits[0]) if len(hits) == 1 else text

    def _on_on(self, text: str) -> Turn:
        pick = _pick(text, 3)
        if pick == 2:
            self._save("later")
            self.step = "end"
            return Turn(
                _("All right. I will ask once more in a week. You can also type /backup any time."),
                done=True,
            )
        if pick == 3:
            self._save("done")
            self.step = "end"
            return Turn(
                _(
                    "Have your recovery sheet ready and run this in a terminal:\n"
                    "  vilna-backup recover <where the backups are>\n"
                    "Then: vilna-backup restore --in-place"
                ),
                done=True,
            )
        if pick != 1:
            return self._again(3)
        overview = self._call("overview", {}, _("Gloxi measures how much space your folders take"))
        self._groups = list(overview.get("groups") or [])
        sizes = {g["group"]: g.get("bytes") for g in self._groups}
        lines = [_("What shall we keep? This is how much space it takes now:")]
        for g in self._groups:
            name = GROUP_NAMES.get(g["group"], lambda g=g: g["group"])()
            lines.append(f"  {name}: {human(g.get('bytes'))}")
        docs = sum(sizes.get(k) or 0 for k in ("settings", "documents", "desktop", "pictures"))
        self.step = "what"
        return Turn(
            "\n".join(lines),
            (
                _("Only settings ({size})").format(size=human(sizes.get("settings"))),
                _("Settings and documents, recommended ({size})").format(size=human(docs)),
                _("Everything in the home folder, without caches and Downloads ({size})").format(
                    size=human(sizes.get("home"))
                ),
                _("Choose myself"),
            ),
        )

    def _on_what(self, text: str) -> Turn:
        pick = _pick(text, 4)
        if pick is None:
            return self._again(4)
        if pick == 4:
            self._custom = [
                g for g in self._groups if g["group"] not in ("settings", "home") and g.get("paths")
            ]
            self.step = "custom"
            choices = tuple(
                f"{GROUP_NAMES.get(g['group'], lambda g=g: g['group'])()} ({human(g.get('bytes'))})"
                for g in self._custom
            )
            return Turn(
                _(
                    "Settings are always kept. Which folders to add? "
                    "Type their numbers, for example: 1 3"
                ),
                choices,
            )
        preset = {1: "settings", 2: "documents", 3: "home"}[pick]
        self._preset_label = {
            1: _("settings"),
            2: _("settings and documents"),
            3: _("everything in the home folder"),
        }[pick]
        self._call("set_paths", {"preset": preset}, _("Choose what goes into backups"))
        return self._ask_history()

    def _on_custom(self, text: str) -> Turn:
        picks = [int(x) for x in text.replace(",", " ").split() if x.isdigit()]
        if not picks or any(not 1 <= p <= len(self._custom) for p in picks):
            return Turn(_("Type the numbers of the folders, for example: 1 3"))
        add = [self._custom[p - 1]["paths"][0] for p in sorted(set(picks))]
        self._call(
            "set_paths", {"preset": "custom", "add": add}, _("Choose what goes into backups")
        )
        self._preset_label = _("settings and the folders you chose")
        return self._ask_history()

    def _ask_history(self) -> Turn:
        self.step = "history"
        return Turn(_("Keep your conversations with Gloxi in backups too?"), (_("No"), _("Yes")))

    def _on_history(self, text: str) -> Turn:
        pick = _pick(text, 2)
        if pick is None:
            return self._again(2)
        self._call(
            "set_paths",
            {"gloxi_history": pick == 2},
            _("Keep Gloxi conversations in backups") if pick == 2 else _("Leave out Gloxi history"),
        )
        self.step = "secrets"
        return Turn(
            _("And Wi-Fi passwords, online accounts, SSH and GPG keys?"), (_("No"), _("Yes"))
        )

    def _on_secrets(self, text: str) -> Turn:
        pick = _pick(text, 2)
        if pick is None:
            return self._again(2)
        # Only the person can switch this on, in a terminal: noted for the summary.
        self._secrets = pick == 2
        self.step = "limit"
        return Turn(
            _("How much space are you ready to give to backups?"),
            ("50 GB", "200 GB", "1 TB", _("No limit")),
        )

    def _on_limit(self, text: str) -> Turn:
        pick = _pick(text, 4)
        if pick is not None:
            limit = {1: 50, 2: 200, 3: 1000, 4: 0}[pick]
        else:
            number = text.lower().removesuffix("gb").strip().replace(",", ".")
            try:
                limit = float(number)
            except ValueError:
                return Turn(_("Choose a number from the list, or type an amount in GB, e.g. 120"))
            if limit < 0:
                return Turn(_("Type an amount in GB, e.g. 120"))
        self._call("set_paths", {"limit_gb": limit}, _("Set how much space backups may take"))
        return self._check_fit()

    def _check_fit(self) -> Turn:
        overview = self._call("overview", {}, _("Gloxi checks that the backup fits"))
        current = overview.get("current") or {}
        self._size = current.get("bytes")
        if current.get("fits", True):
            return self._ask_where()
        self._suggest = list(current.get("suggest_exclude") or [])
        if not self._suggest:
            self.step = "limit"
            return Turn(
                _("{size} does not fit. Choose a larger amount.").format(
                    size=human(current.get("bytes"))
                ),
                ("50 GB", "200 GB", "1 TB", _("No limit")),
            )
        self.step = "fit"
        folders = "\n".join(f"  {_short(p)}" for p in self._suggest)
        return Turn(
            _(
                "{size} does not fit into {limit}. Leave out these large folders "
                "(about {after} remains)?\n{folders}"
            ).format(
                size=human(current.get("bytes")),
                limit=human(current.get("limit")),
                after=human(current.get("bytes_after")),
                folders=folders,
            ),
            (_("Yes, leave them out"), _("No, I will give more space")),
        )

    def _on_fit(self, text: str) -> Turn:
        pick = _pick(text, 2)
        if pick is None:
            return self._again(2)
        if pick == 2:
            self.step = "limit"
            return Turn(
                _("How much space are you ready to give to backups?"),
                ("50 GB", "200 GB", "1 TB", _("No limit")),
            )
        self._call("set_paths", {"remove": self._suggest}, _("Leave large folders out of backups"))
        return self._check_fit()

    def _ask_where(self) -> Turn:
        self.step = "where"
        return Turn(
            _("Where shall we keep them? Everything is encrypted on this computer first."),
            (
                _("Vilna cloud (EU)"),
                _("My own storage (S3, SFTP)"),
                _("An external disk"),
                _("Dropbox, OneDrive, Google Drive"),
            ),
        )

    def _on_where(self, text: str) -> Turn:
        pick = _pick(text, 4)
        if pick is None:
            return self._again(4)
        if pick == 1:
            return Turn(
                _("The Vilna cloud is not open yet in this preview. Choose another place."),
                self._ask_where().choices,
            )
        if pick == 4:
            return Turn(
                _(
                    "These are US companies: they see your account, the size and time of "
                    "backups (not the contents), and a broken device could delete the backups. "
                    "They are not in this preview yet. Choose another place."
                ),
                self._ask_where().choices,
            )
        self._where = {2: "own", 3: "disk"}[pick]
        self.step = "often"
        return Turn(
            _("How often?"), (_("Every day, recommended"), _("Every hour"), _("Every week"))
        )

    def _on_often(self, text: str) -> Turn:
        pick = _pick(text, 3)
        if pick is None:
            return self._again(3)
        schedule = {1: "daily", 2: "hourly", 3: "weekly"}[pick]
        self._call("set_schedule", {"schedule": schedule}, _("Set how often backups are made"))
        how = {1: _("every day"), 2: _("every hour"), 3: _("every week")}[pick]
        where = {"own": _("in your own storage"), "disk": _("on the external disk")}[self._where]
        lines = [
            _("I will back up {what} (about {size}) {how} {where}.").format(
                what=self._preset_label, size=human(self._size), how=how, where=where
            ),
            "",
            _(
                "The last step is yours, because it is about keys: run this in a terminal. "
                "It shows the recovery key once; write it on paper and keep it away from the "
                "computer. Without it, backups cannot be restored if this computer is lost. "
                "The first backup starts right after."
            ),
            f"  {WHERE_COMMANDS[self._where]}",
        ]
        if self._secrets:
            lines += [
                "",
                _("Passwords and accounts are switched on by you only:"),
                "  vilna-backup secrets on",
            ]
        self._save("done")
        self.step = "end"
        return Turn("\n".join(lines), done=True)

    # -- helpers -----------------------------------------------------------------

    def _call(self, operation: str, arguments: dict, reason: str) -> dict[str, Any]:
        try:
            answer = self.request(operation, arguments, reason, [])
        except Exception as e:  # Herkos is not running, the socket broke
            raise IntroStopped(_("Herkos, the permission service, is not available")) from e
        if not isinstance(answer, dict):
            raise IntroStopped(_("Herkos sent an answer Gloxi does not understand"))
        if answer.get("decision") != "allowed" or answer.get("code") != "allowed":
            raise IntroStopped(str(answer.get("message") or answer.get("code") or "denied"))
        result = answer.get("result")
        return result if isinstance(result, dict) else {}

    def _stop(self, why: str, *, save: bool = True) -> Turn:
        if save:
            self._save("later")
        self.step = "end"
        return Turn(
            _("I could not continue: {why}. We can go on later: type /backup.").format(why=why),
            done=True,
        )

    @staticmethod
    def _again(n: int) -> Turn:
        return Turn(_("Choose a number from 1 to {n}.").format(n=n))


def _pick(text: str, n: int) -> int | None:
    text = text.strip().rstrip(".")
    if text.isdigit() and 1 <= int(text) <= n:
        return int(text)
    return None


def _short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def backup_intro(enabled: bool) -> BackupIntro | None:
    """The talk wired to the real Herkos, or None if tools are off or Herkos is missing.

    The Flatpak has no backup tool (backups are part of Vilna), so no talk there.
    """
    from gloxi.herkos_local import in_flatpak

    if not enabled or in_flatpak():
        return None
    try:
        from herkos.client import HerkosClient
        from herkos.config import ConfigError, socket_path
    except ImportError:
        return None
    try:
        sock = socket_path()
    except ConfigError:
        return None
    if not sock.exists():
        return None
    client: list[Any] = []

    def close() -> None:
        while client:
            client.pop().close()

    def request(operation: str, arguments: dict, reason: str, resources: list) -> dict:
        if not client:
            client.append(HerkosClient.connect(sock))
        return client[0].request_action(
            tool=BACKUP_TOOL,
            operation=operation,
            reason=reason,
            resources=resources,
            arguments=arguments,
        )

    return BackupIntro(request, close=close)
