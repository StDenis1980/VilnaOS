#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the KIWI image descriptions in obs/images/ before OBS builds them.

    check_images.py [--rpms DIR [--resolve]]

For each image: KIWI loads and validates the description (schema and
rules, needs the ``kiwi`` Python package: ``zypper in python3-kiwi`` or
``pip install kiwi``), and ``config.sh`` passes ``bash -n``. A pattern
the image names (``<namedCollection>``) that a spec in obs/packages
provides counts with everything it requires. With ``--rpms``, every
Vilna package the image asks for must be among the RPMs built in DIR,
so the image never waits on a package that does not exist.

With ``--resolve`` (openSUSE only, with DIR added as a zypper
repository), zypper solves the image's package list into an empty root
against the configured repositories without installing anything: a
conflict or a missing package fails here, not as "unresolvable" on OBS.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "obs" / "images"
PACKAGES = ROOT / "obs" / "packages"
# Packages of this project; all others come from openSUSE.
OURS = re.compile(
    r"^(Vilna-|vilna-|distribution-logos-Vilna|.*-branding-Vilna$|herkos|gloxi|python3-vilna-|patterns-vilna|gnome-shell-extension-vilna)"
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


def preamble_sections(spec: str) -> list[tuple[str, list[str]]]:
    """The package preambles of a spec: (package name, lines) for the main one and each %package."""
    sections: list[tuple[str, list[str]]] = []
    name = ""
    current: list[str] | None = []
    for line in spec.splitlines():
        words = line.split()
        if words and words[0] == "%package":
            sub = words[-1]
            current = []
            sections.append((sub if "-n" in words else f"{name}-{sub}", current))
        elif words and words[0] in ("%description", "%prep", "%build", "%install", "%files"):
            current = None
        elif current is not None:
            field, _, value = line.partition(":")
            if field == "Name" and not name:
                name = value.strip()
                sections.insert(0, (name, current))
            current.append(line)
    return sections


def our_patterns() -> dict[str, tuple[str, list[str]]]:
    """Patterns provided by specs in obs/packages: name -> (package, requires)."""
    patterns: dict[str, tuple[str, list[str]]] = {}
    for spec in sorted(PACKAGES.glob("*/*.spec")):
        for package, lines in preamble_sections(spec.read_text(encoding="utf-8")):
            fields = [(f.strip(), v.strip()) for f, _, v in (line.partition(":") for line in lines)]
            requires = [v for f, v in fields if f == "Requires"]
            for field, value in fields:
                if field == "Provides" and value.startswith("pattern() ="):
                    patterns[value.split("=", 1)[1].strip()] = (package, requires)
    return patterns


def resolve(image: str, capabilities: list[str]) -> list[str]:
    """Let zypper solve the image's packages into an empty root, installing nothing."""
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        result = subprocess.run(
            [
                "zypper",
                "--non-interactive",
                "--installroot",
                root,
                "install",
                "--dry-run",
                "--no-recommends",
                "--",
                *capabilities,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode == 0:
        print(f"{image}: zypper resolves all packages")
        return []
    detail = (result.stdout + result.stderr).strip()
    return [f"{image}: zypper cannot resolve the packages:\n{detail}"]


def check(image: Path, built: set[str] | None, solve: bool = False) -> list[str]:
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
    capabilities = sorted(wanted)
    patterns = our_patterns()
    for collection in state.get_system_collections():
        capabilities.append(f"pattern() = {collection}")
        if collection in patterns:
            package, requires = patterns[collection]
            wanted |= {package, *requires}
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
    if solve:
        errors += resolve(image.name, capabilities)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpms", type=Path, help="folder with the RPMs built from obs/packages")
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="also let zypper solve each image's packages (needs --rpms as a repository)",
    )
    args = parser.parse_args(argv)
    if args.resolve and not args.rpms:
        parser.error("--resolve needs --rpms")
    built = built_names(args.rpms) if args.rpms else None
    errors: list[str] = []
    for image in sorted(p for p in IMAGES.iterdir() if p.is_dir()):
        errors += check(image, built, args.resolve)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
