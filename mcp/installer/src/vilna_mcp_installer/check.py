# SPDX-License-Identifier: Apache-2.0
"""Check a plan file against this computer, without the model.

    vilna-plan-check PLAN [--demo empty|windows] [--stage alpha|beta]

Prints the validator's report as JSON. Exit status 0: the plan can be
installed (after the person confirms it); 1: something is missing or wrong;
2: the plan or the computer could not be read. For the classic wizard and
the Herkos confirmation screen, which use the same validator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vilna_mcp_installer import plan as plans
from vilna_mcp_installer.machine import DemoMachine, Machine, MachineError, survey


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-plan-check")
    p.add_argument("plan", type=Path)
    p.add_argument("--demo", choices=("empty", "windows"))
    p.add_argument("--stage", choices=tuple(plans.STAGE_LAYOUTS), default="alpha")
    args = p.parse_args(argv)
    machine = DemoMachine(args.demo) if args.demo else Machine()
    try:
        report = plans.validate(plans.load(args.plan), survey(machine.disks()), stage=args.stage)
    except (plans.PlanError, MachineError) as e:
        print(f"vilna-plan-check: {e}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report["valid"] else 1
