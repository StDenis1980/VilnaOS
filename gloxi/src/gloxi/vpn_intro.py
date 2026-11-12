# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run talk about VPN (docs/adr/0027-vpn-proton-mullvad.md, section 2).

"Protect your traffic with a VPN?" Proton VPN is offered first (it has a
free plan), Mullvad for the most anonymity. Gloxi never sees the person's
account: it only opens the provider's own app, or Proton's sign-up page,
and the person signs in there. When a tunnel is up, Gloxi offers the kill
switch of Vilna (Herkos tool ``org.vilna.mcp.vpn``, R2).

Like the backup talk it is a fixed script, not the model: the choices must
be the same for everyone. If the person says "later", Gloxi asks once more
after a week, then never again; they can always ask with ``/vpn``.
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

from gloxi.backup_intro import IntroStopped, Request, Turn, _pick

VPN_TOOL = "org.vilna.mcp.vpn"
REMIND_AFTER = 7 * 86400

# The providers' own apps, by desktop file id. The window launches only these
# (LAUNCHABLE, checked again by gui/window.py and gui/remote.py). Check both
# names in the VM with the packages from the Vilna OBS project (ADR 0027, step 4).
PROTON_APP = "proton.vpn.app.gtk"
MULLVAD_APP = "mullvad-vpn"
PROTON_SIGNUP = "https://protonvpn.com/free-vpn"
LAUNCHABLE = frozenset({PROTON_APP, MULLVAD_APP})


def state_path() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value) if value and os.path.isabs(value) else Path.home() / ".local/state"
    return base / "gloxi" / "vpn-intro.json"


@dataclass
class VpnIntro:
    request: Request
    path: Path = field(default_factory=state_path)
    clock: Callable[[], float] = time.time
    step: str = "idle"
    _provider: str = ""
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
        """Begin the talk. None if the kill switch is already on (nothing to ask)."""
        try:
            status = self._call(
                "status", {}, _("Gloxi checks whether a VPN protects this computer")
            )
        except IntroStopped as e:
            return self._stop(str(e), save=False)
        if status.get("enabled"):
            self._save("done")
            self.step = "end"
            return None
        if status.get("tunnels"):
            self._provider = "connected"
            return self._ask_killswitch()
        self.step = "choose"
        self._last = Turn(
            _(
                "Shall we protect your traffic with a VPN? It hides what you do from the "
                "network you are on (a cafe, a hotel) and your address from websites. The VPN "
                "provider sees where you go, so I only suggest proven ones. Your encrypted DNS "
                "already works without a VPN."
            ),
            (
                _("Sign in to Proton VPN"),
                _("Create a free Proton account (5 countries, 1 device)"),
                _("I have a Mullvad account number or code"),
                _("Later"),
            ),
        )
        return self._last

    def answer(self, text: str) -> Turn:
        handler = getattr(self, f"_on_{self.step}", None)
        if handler is None:
            return Turn(_("We are not talking about a VPN now."), done=True)
        try:
            turn = handler(text.strip())
        except IntroStopped as e:
            turn = self._stop(str(e))
        if turn.choices or turn.done:
            self._last = turn
        return turn

    def _on_choose(self, text: str) -> Turn:
        pick = _pick(text, 4)
        if pick is None:
            return _again(4)
        if pick == 4:
            self._save("later")
            self.step = "end"
            return Turn(
                _("All right. I will ask once more in a week. You can also type /vpn any time."),
                done=True,
            )
        self.step = "wait"
        if pick == 3:
            self._provider = "mullvad"
            return Turn(
                _(
                    "I am opening the Mullvad app. Enter your account number or activation "
                    "code there, not here: I do not see it and do not keep it. Then press "
                    "Connect and tell me when it is connected."
                ),
                (_("Connected"), _("Later")),
                launch=MULLVAD_APP,
            )
        self._provider = "proton"
        if pick == 2:
            return Turn(
                _(
                    "I am opening Proton's sign-up page. You can create the account with a "
                    "Proton address, without another email. Then sign in in the Proton VPN "
                    "app that I open next, press Connect and tell me when it is connected."
                ),
                (_("Connected"), _("Later")),
                open_uri=PROTON_SIGNUP,
                launch=PROTON_APP,
            )
        return Turn(
            _(
                "I am opening the Proton VPN app. Sign in there, not here: I do not see your "
                "password. Then press Connect and tell me when it is connected."
            ),
            (_("Connected"), _("Later")),
            launch=PROTON_APP,
        )

    def _on_wait(self, text: str) -> Turn:
        pick = _pick(text, 2)
        if pick is None:
            return _again(2)
        if pick == 2:
            self._save("later")
            self.step = "end"
            return Turn(
                _("All right. Type /vpn when you want to go on."),
                done=True,
            )
        status = self._call("status", {}, _("Gloxi checks whether the VPN is connected"))
        if not status.get("tunnels"):
            return Turn(
                _(
                    "I do not see a VPN connection yet. Press Connect in the app, wait a "
                    "few seconds and tell me again."
                ),
                (_("Connected"), _("Later")),
            )
        return self._ask_killswitch()

    def _ask_killswitch(self) -> Turn:
        self.step = "killswitch"
        return Turn(
            _(
                "The VPN is connected. Shall I turn on the kill switch? Then nothing leaves "
                "this computer outside the VPN: if the VPN drops, the internet stops until it "
                "is back. Turning it off later always asks you first."
            ),
            (_("Yes, recommended"), _("No")),
        )

    def _on_killswitch(self, text: str) -> Turn:
        pick = _pick(text, 2)
        if pick is None:
            return _again(2)
        lines: list[str] = []
        if pick == 1:
            self._call("killswitch_on", {}, _("Turn on the VPN kill switch"))
            lines.append(_("The kill switch is on."))
        else:
            lines.append(_("The kill switch stays off. You can turn it on any time: type /vpn."))
        if self._provider == "proton":
            lines.append(
                _(
                    "One more step in the Proton VPN app: in its settings turn on "
                    "“Connect at startup”, so the VPN comes up by itself."
                )
            )
        elif self._provider == "mullvad":
            lines.append(
                _(
                    "One more step in the Mullvad app: in its settings turn on "
                    "“Auto-connect”, so the VPN comes up by itself."
                )
            )
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
            _("I could not continue: {why}. We can go on later: type /vpn.").format(why=why),
            done=True,
        )


def _again(n: int) -> Turn:
    return Turn(_("Choose a number from 1 to {n}.").format(n=n))


def vpn_intro(enabled: bool) -> VpnIntro | None:
    """The talk wired to the real Herkos, or None if tools are off or Herkos is missing.

    The Flatpak has no VPN tool (the kill switch is part of Vilna), so no talk there.
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
            tool=VPN_TOOL,
            operation=operation,
            reason=reason,
            resources=resources,
            arguments=arguments,
        )

    return VpnIntro(request, close=close)
