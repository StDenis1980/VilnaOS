#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the KIWI image descriptions in obs/images/ before OBS builds them.

    check_images.py [--rpms DIR]

For each image: KIWI loads and validates the description (schema and
rules, needs the ``kiwi`` Python package: ``zypper in python3-kiwi`` or
``pip install kiwi``), and ``config.sh`` passes ``bash -n``. With
``--rpms``, every Vilna package the image asks for must be among the
RPMs built in DIR, so the image never waits on a package that does not
exist.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "obs" / "images"
# Packages of this project; all others come from openSUSE.
OURS = re.compile(
    r"^(Vilna-|vilna-|distribution-logos-Vilna|.*-branding-Vilna$|herkos|gloxi|python3-vilna-)"
)


def built_names(rpms: Path) -> set[str]:
    files = sorted(str(p) for p in rpms.rglob("*.rpm") if not p.name.endswith(".src.rpm"))
    if not files:
        raise SystemExit(f"no RPMs in {rpms}")
    out = subprocess.run(
        ["rpm", "-qp", "--qf", "%{NAME}\n", *files],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def check(image: Path, built: set[str] | None) -> list[str]:
    from kiwi.xml_description import XMLDescription
    from kiwi.xml_state import XMLState

    errors: list[str] = []
    kiwi = image / f"{image.name}.kiwi"
    if not kiwi.is_file():
        return [f"{image.name}: no {kiwi.name}"]
    state = XMLState(XMLDescription(str(kiwi)).load())
    config = image / "config.sh"
    if config.is_file():
        result = subprocess.run(
            ["bash", "-n", str(config)], check=False, capture_output=True, text=True
        )
        if result.returncode != 0:
            errors.append(f"{image.name}: config.sh: {result.stderr.strip()}")
    wanted = set(state.get_system_packages()) | set(state.get_bootstrap_packages())
    ours = sorted(name for name in wanted if OURS.match(name))
    print(
        f"{image.name}: {state.get_build_type_name()}, {len(wanted)} packages, ours: {', '.join(ours)}"
    )
    if built is not None:
        errors += [
            f"{image.name}: {name} is not built from obs/packages"
            for name in ours
            if name not in built
        ]
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpms", type=Path, help="folder with the RPMs built from obs/packages")
    args = parser.parse_args(argv)
    built = built_names(args.rpms) if args.rpms else None
    errors: list[str] = []
    for image in sorted(p for p in IMAGES.iterdir() if p.is_dir()):
        errors += check(image, built)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
