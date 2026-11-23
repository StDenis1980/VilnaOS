# SPDX-License-Identifier: GPL-3.0-or-later
"""What the engine can lay out from the partition editor's plan (``disk.layout = "manual"``).

The validator (``vilna_mcp_installer.plan``) says whether a manual layout
makes sense; this module says whether this version of the engine can write
it, so that the wizard's summary and the Herkos screen say so before the
person confirms, not the progress screen after (KLX laptop, 23.11.2026).

What the engine writes for a manual layout:

- **system**: an existing partition (always formatted) is wiped and deleted,
  and ``systemd-repart`` makes the new LUKS2 system partition in the space
  it leaves, at least as big as it was; or a new one of ``size`` bytes in
  free space. Every other partition stays as it is: repart only adds
  partitions in free space and never changes one it did not make.
- **efi**: an existing partition, kept with its files (shared with Windows)
  or formatted as FAT32; or a new one of ``size`` bytes when the disk has no
  EFI system partition at all (with one there, repart would take that one
  for its definition instead of making a new one).
- **home** and **swap**: not yet; the problem says so.
"""

from __future__ import annotations

from typing import Any

# Codes for the wizard's summary and the Herkos screen (``wizard.PROBLEM_TEXT``,
# ``summary.PROBLEMS``); the engine refuses the same plans.
LATER_HOME_SWAP = "manual_home_swap_later"
NEW_EFI_BESIDE_EFI = "manual_new_efi_beside_efi"

MESSAGES = {
    LATER_HOME_SWAP: "this version installs only an EFI and a system partition; "
    "leave home folders and swap unused for now",
    NEW_EFI_BESIDE_EFI: "the disk has an EFI system partition already; choose it "
    "(formatted or not) instead of a new one",
}


def items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    disk = plan.get("disk") or {}
    out = disk.get("partitions")
    return [i for i in out if isinstance(i, dict)] if isinstance(out, list) else []


def target(plan: dict[str, Any], survey: dict[str, Any]) -> dict[str, Any] | None:
    disk_id = (plan.get("disk") or {}).get("target")
    return next((d for d in survey.get("disks", []) if d.get("id") == disk_id), None)


def problems(plan: dict[str, Any], survey: dict[str, Any]) -> list[dict[str, str]]:
    """Why the engine cannot write this manual layout yet; empty when it can (or not manual)."""
    disk = plan.get("disk") or {}
    if disk.get("layout") != "manual":
        return []
    out = []
    chosen = items(plan)
    if any(i.get("use") in ("home", "swap") for i in chosen):
        out.append(LATER_HOME_SWAP)
    efi = next((i for i in chosen if i.get("use") == "efi"), None)
    disk_info = target(plan, survey) or {}
    if (
        efi is not None
        and efi.get("partition") is None
        and any(p.get("kind") == "efi" for p in disk_info.get("partitions", []))
    ):
        out.append(NEW_EFI_BESIDE_EFI)
    return [{"code": c, "field": "disk.partitions", "message": MESSAGES[c]} for c in out]
