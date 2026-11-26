#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the KIWI images (``obs/images/``) against the OBS packages (``obs/packages/``).

A static check with the standard library only: KIWI, rpm and zypper are not
needed (``obs/tools/check_images.py`` runs KIWI itself in the OBS workflow).

For every tracked ``*.kiwi`` file the checker verifies:

* it is well-formed XML with the root ``<image>`` that has ``schemaversion``
  and ``name`` (equal to the image folder, as OBS and obs_sync.py name it),
  a ``<description>`` and ``<preferences>`` with a ``<version>``;
* every ``<package name>`` that is ours (it starts with vilna-, Vilna-, gloxi,
  herkos, patterns-vilna, python3-vilna-, distribution-logos-Vilna,
  gnome-shell-extension-vilna, ends with -branding-Vilna, or is the name of a
  folder in obs/packages) is built by a spec in obs/packages: its ``Name`` or
  a ``%package`` (``-n name`` or ``<Name>-<suffix>``) that has ``%files``;
  ``tik-osimage-*`` is an image folder (OBS wraps that image into an RPM);
* every ``<namedCollection>``/``<collection>`` whose name starts with
  ``vilna`` is a pattern a spec provides (``Provides: pattern() = <name>``);
* the ``Requires:`` and ``Recommends:`` of every pattern package (such as
  ``patterns-vilna-base``) pass the same check as the packages of an image;
* a package named twice in one image is a warning;
* the other way round: every package folder in obs/packages builds at least
  one RPM that an image or a pattern installs, directly or through the
  ``Requires``/``Recommends`` of our packages; if not, a warning.

At the end it prints a summary: for each image, its type and which of our
packages it gets, directly or through a pattern.

Specs are read as text: ``Name``, ``%package``, ``%files``, ``Provides``,
``Requires``, ``Recommends``, ``%define``/``%global`` with ``%{name}``. The
spec reading follows ``tools/check_spec.py`` but is kept here so that this
tool works on its own.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check.

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_kiwi.py
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

IMAGES = "obs/images"
PACKAGES = "obs/packages"
OUR_PREFIXES = (
    "vilna-",
    "Vilna-",
    "gloxi",
    "herkos",
    "patterns-vilna",
    "python3-vilna-",
    "distribution-logos-Vilna",
    "gnome-shell-extension-vilna",
)
OUR_SUFFIXES = ("-branding-Vilna",)
# Images OBS wraps into RPMs of their own name (obs/project/_prjconf).
IMAGE_RPM = "tik-osimage-"
OUR_COLLECTION = "vilna"
PATTERN = re.compile(r"pattern\(\)\s*=\s*(\S+)")
# The sections of a spec that end a preamble (check_spec.py has the full list).
SECTIONS = frozenset(
    {
        "%package",
        "%description",
        "%prep",
        "%generate_buildrequires",
        "%conf",
        "%build",
        "%install",
        "%check",
        "%clean",
        "%files",
        "%changelog",
        "%pre",
        "%post",
        "%preun",
        "%postun",
        "%pretrans",
        "%posttrans",
        "%verifyscript",
        "%triggerin",
        "%triggerun",
        "%triggerpostun",
        "%filetriggerin",
        "%filetriggerun",
        "%transfiletriggerin",
    }
)
TAG = re.compile(r"([A-Za-z][A-Za-z0-9]*)(\([^)]*\))?:\s*(.*)")
DEFINE = re.compile(r"%(?:define|global)\s+(\w+)\s+(.*\S)")
MACRO = re.compile(r"%\{\??(\w+)\}|%(\w+)")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


@dataclass
class Rpm:
    """One binary package of a spec, with what the image check needs."""

    name: str
    spec: str
    files: bool = False
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)  # Requires and Recommends

    @property
    def patterns(self) -> list[str]:
        return [m.group(1) for p in self.provides if (m := PATTERN.fullmatch(p))]


@dataclass
class Image:
    """What the checker reads from a .kiwi file."""

    path: str
    name: str
    build_type: str
    packages: list[str]  # <package name>, in order, with repeats
    collections: list[str]


def expand(value: str, macros: dict[str, str]) -> str:
    for _ in range(5):
        new = MACRO.sub(lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def section_name(words: list[str], main: str, macros: dict[str, str]) -> str:
    """The package of a ``%package``/``%files`` line: ``-n full`` or ``<Name>-<suffix>``."""
    names: list[str] = []
    full = skip = False
    for arg in words[1:]:
        if skip:
            skip = False
        elif arg == "-n":
            full = True
        elif arg == "-f":
            skip = True
        elif not arg.startswith("-"):
            names.append(arg)
    if not names:
        return main
    name = expand(names[0], macros)
    return name if full else f"{main}-{name}"


def parse_spec(path: str, text: str) -> list[Rpm]:
    """The binary packages a spec declares (``files`` tells which ones rpm builds)."""
    macros: dict[str, str] = {}
    main = Rpm("", path)
    rpms = {"": main}
    current: Rpm | None = main
    files: list[str] = []
    lang = python = False
    for line in text.splitlines():
        words = line.split()
        keyword = words[0] if words else ""
        if keyword in SECTIONS:
            current = None
            if keyword == "%package":
                current = Rpm(section_name(words, main.name, macros), path)
                rpms[current.name] = current
            elif keyword == "%files":
                python_files = "%{python_files}" in words
                files.append(main.name if python_files else section_name(words, main.name, macros))
            continue
        if define := DEFINE.match(line.strip()):
            macros[define.group(1)] = define.group(2)
            continue
        lang = lang or keyword == "%lang_package"
        python = python or keyword == "%python_subpackages"
        tag = TAG.fullmatch(line.strip())
        if current is None or not tag or line.startswith((" ", "\t", "#", "%")):
            continue
        key, value = tag.group(1).lower(), expand(tag.group(3).strip(), macros)
        if key in ("name", "version") and current is main:
            macros.setdefault(key, value)
            if key == "name":
                main.name = macros["name"]
        elif key == "provides":
            current.provides.append(value)
        elif key in ("requires", "recommends"):
            current.requires.append(value)
    rpms[main.name] = rpms.pop("")
    if lang:
        rpms[f"{main.name}-lang"] = Rpm(f"{main.name}-lang", path)
    for name in files:
        if name in rpms:
            rpms[name].files = True
    if python and main.name.startswith("python-"):
        del rpms[main.name]
        for flavor in macros.get("pythons", "python3").split():
            name = f"{flavor}{main.name[len('python') :]}"
            rpms[name] = Rpm(name, path, main.files, main.provides, main.requires)
    return list(rpms.values())


RICH_OPERATORS = frozenset({"and", "or", "if", "else", "with", "without", "unless"})
COMPARATORS = frozenset({"<", ">", "=", "<=", ">=", "=="})


def capabilities(value: str) -> list[str]:
    """The package names of a Requires value; files and ``name(...)`` capabilities are skipped.

    A rich dependency such as ``(herkos-selinux if selinux-policy-targeted)``
    gives every package name in it.
    """
    if value.startswith("("):
        words = re.findall(r"[^\s()]+", value)
    else:
        words = [w for w in value.split()[:1] if "(" not in w]
    names: list[str] = []
    after_comparator = False
    for word in words:
        if word in COMPARATORS:
            after_comparator = True
        elif after_comparator or word in RICH_OPERATORS:
            after_comparator = False
        elif not word.startswith("/") and "%" not in word:
            names.append(word)
    return names


def is_ours(name: str, folders: Iterable[str]) -> bool:
    return name.startswith(OUR_PREFIXES) or name.endswith(OUR_SUFFIXES) or name in folders


def parse_image(path: str, text: str) -> tuple[Image | None, list[Problem]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        return None, [Problem(path, f"not well-formed XML: {err}")]
    if root.tag != "image":
        return None, [Problem(path, f"the root element is <{root.tag}>, not <image>")]
    problems: list[Problem] = []
    folder = PurePosixPath(path).parent.name
    for attribute in ("schemaversion", "name"):
        if not (root.get(attribute) or "").strip():
            problems.append(Problem(path, f"<image> has no {attribute}"))
    name = (root.get("name") or "").strip()
    if name and name != folder:
        problems.append(Problem(path, f"<image name={name!r}> is not the folder name {folder!r}"))
    if root.find("description") is None:
        problems.append(Problem(path, "no <description>"))
    preferences = root.findall("preferences")
    if not preferences:
        problems.append(Problem(path, "no <preferences>"))
    elif not any((v.text or "").strip() for p in preferences for v in p.findall("version")):
        problems.append(Problem(path, "<preferences> has no <version>"))
    types = [t.get("image", "?") for p in preferences for t in p.findall("type")]
    packages = [p.get("name", "").strip() for p in root.iter("package")]
    collections = [
        c.get("name", "").strip()
        for tag in ("namedCollection", "collection")
        for c in root.iter(tag)
    ]
    for kind, names in (("<package>", packages), ("collection", collections)):
        if any(not n for n in names):
            problems.append(Problem(path, f"a {kind} without a name"))
    image = Image(path, name or folder, "/".join(types) or "?", packages, collections)
    return image, problems


@dataclass
class Repo:
    """The specs and images of the repo, read once."""

    folders: list[str]  # package folder names in obs/packages
    images: list[str]  # image folder names in obs/images
    rpms: dict[str, Rpm]  # every binary package a spec declares
    patterns: dict[str, Rpm]  # pattern name -> the package that provides it

    def built(self, name: str) -> bool:
        return name in self.rpms and self.rpms[name].files


def check_names(path: str, names: Iterable[str], repo: Repo, where: str) -> list[Problem]:
    """Our package names must be built by a spec; tik-osimage-* must be an image."""
    problems: list[Problem] = []
    for name in names:
        if name.startswith(IMAGE_RPM):
            if name not in repo.images:
                problems.append(Problem(path, f"{where}{name} is not an image in {IMAGES}"))
        elif is_ours(name, repo.folders) and not repo.built(name):
            hint = (
                f" ({repo.rpms[name].spec} declares it but has no %files for it)"
                if name in repo.rpms
                else ""
            )
            problems.append(
                Problem(path, f"{where}{name} is not built by any spec in {PACKAGES}{hint}")
            )
    return problems


def check_image(image: Image, repo: Repo) -> list[Problem]:
    problems = check_names(image.path, image.packages, repo, "package ")
    for name in image.collections:
        if name.startswith(OUR_COLLECTION) and name not in repo.patterns:
            problems.append(
                Problem(image.path, f"pattern {name} is not provided by any spec in {PACKAGES}")
            )
    seen: set[str] = set()
    for name in image.packages:
        if name in seen:
            problems.append(Problem(image.path, f"{name} is named twice", warning=True))
        seen.add(name)
    return problems


def check_patterns(repo: Repo) -> list[Problem]:
    problems: list[Problem] = []
    for pattern, rpm in sorted(repo.patterns.items()):
        names = [n for v in rpm.requires for n in capabilities(v)]
        problems += check_names(rpm.spec, names, repo, f"pattern {pattern} requires ")
    return problems


def installed(images: Sequence[Image], repo: Repo) -> dict[str, set[str]]:
    """Image name -> every one of our RPMs it gets (directly, by patterns, by Requires)."""
    provided = {p: rpm.name for rpm in repo.rpms.values() for p in rpm.provides}
    result: dict[str, set[str]] = {}
    for image in images:
        todo = [
            *image.packages,
            *(repo.patterns[c].name for c in image.collections if c in repo.patterns),
        ]
        got: set[str] = set()
        while todo:
            name = todo.pop()
            name = name if name in repo.rpms else provided.get(name, name)
            if name in got or not repo.built(name):
                continue
            got.add(name)
            todo += [n for v in repo.rpms[name].requires for n in capabilities(v)]
        result[image.name] = got
    return result


def check_unused(images: Sequence[Image], repo: Repo) -> list[Problem]:
    everywhere = set().union(*installed(images, repo).values()) if images else set()
    problems: list[Problem] = []
    for folder in repo.folders:
        spec = f"{PACKAGES}/{folder}/{folder}.spec"
        built = [r.name for r in repo.rpms.values() if r.spec == spec and r.files]
        if built and not everywhere.intersection(built):
            problems.append(
                Problem(
                    spec,
                    f"no image or pattern installs {', '.join(sorted(built))} (nobody installs "
                    "the package)",
                    warning=True,
                )
            )
    return problems


def summary(images: Sequence[Image], repo: Repo) -> list[str]:
    lines = []
    got = installed(images, repo)
    for image in images:
        direct = {n for n in image.packages if repo.built(n)}
        via = sorted(got[image.name] - direct)
        lines.append(
            f"{image.name} ({image.build_type}): {len(image.packages)} packages, "
            f"{', '.join(image.collections) or 'no patterns'}; ours directly: "
            f"{', '.join(sorted(direct)) or '-'}; ours through patterns and Requires: "
            f"{len(via)} ({', '.join(via) or '-'})"
        )
    return lines


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [Problem(rel, "the file does not exist")]
    except OSError as err:
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}")]
    except UnicodeDecodeError:
        return None, [Problem(rel, "the file is not valid UTF-8")]
    if text.startswith("\ufeff"):
        return text[1:], [Problem(rel, "the file starts with a byte order mark (BOM)")]
    return text, []


def folders_in(tracked: Iterable[str], base: str) -> list[str]:
    found = set()
    for file in tracked:
        parts = PurePosixPath(file).parts
        if len(parts) > 3 and "/".join(parts[:2]) == base:
            found.add(parts[2])
    return sorted(found)


def load_repo(root: Path, tracked: Sequence[str]) -> tuple[Repo, list[Problem]]:
    problems: list[Problem] = []
    rpms: dict[str, Rpm] = {}
    folders = folders_in(tracked, PACKAGES)
    for folder in folders:
        path = f"{PACKAGES}/{folder}/{folder}.spec"
        if path not in tracked:
            continue  # tools/check_spec.py reports a folder without its spec
        text, found = read_text(root, path)
        problems += found
        for rpm in parse_spec(path, text or ""):
            rpms.setdefault(rpm.name, rpm)
    patterns = {p: rpm for rpm in rpms.values() if rpm.files for p in rpm.patterns}
    return Repo(folders, folders_in(tracked, IMAGES), rpms, patterns), problems


def check_repo(root: Path, tracked: Sequence[str]) -> tuple[list[Problem], list[str]]:
    """Check every tracked .kiwi against the specs; also return the summary lines."""
    repo, problems = load_repo(root, tracked)
    images: list[Image] = []
    for path in sorted(f for f in tracked if f.startswith(f"{IMAGES}/") and f.endswith(".kiwi")):
        text, found = read_text(root, path)
        problems += found
        if text is None:
            continue
        image, found = parse_image(path, text)
        problems += found
        if image is not None:
            images.append(image)
            problems += check_image(image, repo)
    problems += check_patterns(repo)
    problems += check_unused(images, repo)
    return problems, summary(images, repo)


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f"{IMAGES}/", f"{PACKAGES}/"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: python3 tools/check_kiwi.py", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    kiwis = [f for f in tracked if f.endswith(".kiwi")]
    if not kiwis:
        print(f"no tracked *.kiwi files in {IMAGES}", file=sys.stderr)
        return 2
    problems, lines = check_repo(ROOT, tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    for line in lines:
        print(line)
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(kiwis)} images and {len(folders_in(tracked, PACKAGES))} package folders checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
