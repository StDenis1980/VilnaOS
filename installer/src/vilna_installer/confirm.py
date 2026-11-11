# SPDX-License-Identifier: GPL-3.0-or-later
"""The Herkos confirmation screen, without GTK: what it checks, shows and sets.

image/docs/installer.md, 1.4 and step 12. Writing to a disk is an R4 action
(herkos/docs/permissions.md): it is confirmed every time, with the exact
changes shown, and only by real input. So:

- The screen is **its own process** (``vilna-installer-confirm``, the
  window in ``confirm_window``), started by the installer after «Check and
  confirm the plan» in the conversation or at the end of the classic wizard.
  Gloxi has no tool that reaches it; its text is made from the plan and a
  fresh survey of the disks (``summary``), never by the model.
- It **looks at the disks again** and runs the validator itself; a plan
  made for disks that changed meanwhile cannot be confirmed.
- With encryption it shows the **recovery key** and asks the person to type
  one group of it back; that alone sets ``recovery_key_acknowledged``.
- When something is erased, the person ticks that they understand; the
  install button wakes up only after ``ARM_SECONDS``, so a double click on
  «Check and confirm» cannot press it.
- Only then it sets ``confirmed_by_user`` and answers on its standard output
  with one JSON line: ``{"answer": "confirmed", "plan_sha256": …,
  "recovery_key": …}`` (or ``{"answer": "back"}``). The installer keeps the
  key in its ``Vault`` for the engine; the engine installs only a plan whose
  SHA-256 is the confirmed one.

The installer calls ``command()`` and ``parse()``; the conversation and the
classic wizard do it the same way (``confirm_window.ask``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vilna_mcp_installer import machine as machines
from vilna_mcp_installer import plan as plans

from vilna_installer import encryption, start, summary

ARM_SECONDS = 3
# The group of the recovery key the person types back (the last one: «the
# last eight letters» is easy to say and to find).
CHECK_GROUP = 7

# Texts of the screen (msgids of locale/*.po).
HERKOS = "Herkos"
TITLE = "Confirm the installation"
LEAD = (
    "Check what will happen. Nothing has changed on the disks yet; after you confirm, "
    "Vilna is installed and this cannot be undone."
)
DISKS = "What happens to the disks"
PLAN_HEAD = "The plan"
PROBLEMS_HEAD = "What has to be fixed first"
PROBLEMS_LEAD = "The plan cannot be installed yet. Go back and change it:"
WARNINGS_HEAD = "Note"
KEY_HEAD = "Recovery key"
KEY_LEAD = (
    "Write this key down or take a photo of it, and keep it away from the computer. You "
    "need it if the disk does not open by itself: after a change of the hardware or of "
    "Secure Boot, or when you forget the passphrase. Nobody else has it, not even Vilna."
)
KEY_TYPE = "To show that you saved it, type the last group of the key (8 letters):"
KEY_FIELD = "The last group of the recovery key"
KEY_WRONG = "That is not the last group. Look at the key again."
KEY_OK = "Right. The key is saved."
KEY_SPOKEN = "Recovery key, letter by letter: {key}"
UNDERSTAND = "I understand that everything on {disk} is erased"
UNDERSTAND_NO_DISK = "I understand that the partitions marked above are erased"
BACK = "Back to the plan"
INSTALL = "Erase and install"
INSTALL_KEEP = "Install"
WAIT = "Read the changes first…"
CHANGED = "The disks changed while this screen was open. Go back and choose the disk again."
FROM_HERKOS = "This screen is shown by Herkos, not by Gloxi. Gloxi cannot press its buttons."

TEXTS = (
    HERKOS,
    TITLE,
    LEAD,
    DISKS,
    PLAN_HEAD,
    PROBLEMS_HEAD,
    PROBLEMS_LEAD,
    WARNINGS_HEAD,
    KEY_HEAD,
    KEY_LEAD,
    KEY_TYPE,
    KEY_FIELD,
    KEY_WRONG,
    KEY_OK,
    KEY_SPOKEN,
    UNDERSTAND,
    UNDERSTAND_NO_DISK,
    BACK,
    INSTALL,
    INSTALL_KEEP,
    WAIT,
    CHANGED,
    FROM_HERKOS,
)


@dataclass(frozen=True)
class Outcome:
    confirmed: bool
    plan_sha256: str | None = None
    recovery_key: str | None = None


def machine_for(demo: str | None) -> machines.Machine:
    return machines.DemoMachine(demo) if demo else machines.Machine()


def encrypted(plan: dict[str, Any]) -> bool:
    enc = (plan.get("disk") or {}).get("encryption") or {}
    return enc.get("method") in ("tpm2", "passphrase")


@dataclass
class Check:
    """Everything the screen shows, from the plan file and the disks as they are now."""

    plan: dict[str, Any]
    survey: dict[str, Any]
    report: dict[str, Any]
    summary: summary.Summary


def check(
    plan_path: Path,
    machine: machines.Machine,
    texts: summary.Texts,
    *,
    stage: str = "alpha",
    tpm: bool | None = True,
) -> Check:
    """Read the plan, survey the disks, validate; ``tpm`` from ``encryption.tpm_can_seal``."""
    plan = plans.load(plan_path)
    try:
        survey = machines.survey(machine.disks())
    except machines.MachineError:
        survey = {"fingerprint": None, "disks": []}
    report = plans.validate(plan, survey, stage=stage)
    extra = []
    method = ((plan.get("disk") or {}).get("encryption") or {}).get("method")
    if method == "tpm2" and tpm is False:
        extra.append({"code": "no_tpm", "field": "disk.encryption.method"})
    return Check(
        plan, survey, report, summary.summarize(plan, report, survey, texts, extra_problems=extra)
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NotConfirmed(Exception):
    """The plan or the disks changed, or the plan is not valid: the screen says what."""


def confirm(
    plan_path: Path,
    machine: machines.Machine,
    *,
    seen: dict[str, Any],
    key_typed_back: bool,
    stage: str = "alpha",
) -> str:
    """Mark the plan the person saw as confirmed; returns the SHA-256 of the file.

    ``seen`` is the plan as the screen showed it: if the file changed since
    (the conversation is still running beside it), nothing is confirmed.
    """
    plan = plans.load(plan_path)
    if _without_marks(plan) != _without_marks(seen):
        raise NotConfirmed("the plan changed while the screen was open")
    if encrypted(plan):
        if not key_typed_back:
            raise NotConfirmed("the recovery key was not typed back")
        plans.acknowledge_recovery_key(plan)
    plan["confirmed_by_user"] = True
    survey = machines.survey(machine.disks())
    report = plans.validate(plan, survey, stage=stage)
    if report["problems"] or report["missing"]:
        codes = [p["code"] for p in report["problems"]] + report["missing"]
        raise NotConfirmed(f"the plan is not valid now: {', '.join(codes)}")
    plans.save(plan, plan_path)
    return sha256(plan_path)


def _without_marks(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan without what only this screen sets."""
    out = json.loads(json.dumps(plan))
    out.pop("confirmed_by_user", None)
    enc = (out.get("disk") or {}).get("encryption")
    if isinstance(enc, dict):
        enc.pop("recovery_key_acknowledged", None)
    return out


# --- between the installer and the screen ---------------------------------------


def command(
    plan_path: Path,
    *,
    demo: str | None = None,
    window: bool = False,
    stage: str = "alpha",
) -> list[str]:
    """How the installer starts the screen: the same Python, its own process."""
    argv = [sys.executable, "-m", "vilna_installer.confirm_window", "--plan", str(plan_path)]
    if demo:
        argv += ["--demo", demo]
    if window:
        argv.append("--window")
    if stage != "alpha":
        argv += ["--stage", stage]
    return argv


def answer(outcome: Outcome) -> str:
    """The one line the screen prints for the installer."""
    if not outcome.confirmed:
        back: dict[str, Any] = {"answer": "back"}
        if outcome.recovery_key:
            back["recovery_key"] = outcome.recovery_key
        return json.dumps(back)
    return json.dumps(
        {
            "answer": "confirmed",
            "plan_sha256": outcome.plan_sha256,
            "recovery_key": outcome.recovery_key,
        }
    )


def parse(stdout: str) -> Outcome:
    """The screen's answer; anything else (it crashed, it was closed) means «back»."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        key = data.get("recovery_key")
        if key is not None and (not isinstance(key, str) or not encryption.RECOVERY_RE.match(key)):
            key = None
        if data.get("answer") != "confirmed":
            return Outcome(False, recovery_key=key)
        digest = data.get("plan_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            return Outcome(False)
        if key is None and data.get("recovery_key") is not None:
            return Outcome(False)
        return Outcome(True, digest, key)
    return Outcome(False)


def texts_for(plan: dict[str, Any]) -> start.Texts:
    code = (plan.get("ui") or {}).get("language", "en")
    return start.Texts(code if code in start.CODES else "en")
