#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that SECURITY.md and docs/ answer what the Cyber Resilience Act asks a maker to say.

The checker reads ``SECURITY.md`` and the tracked Markdown files of ``docs/`` (not
``docs/design/`` and not ``docs/roadmap.md``: a plan is not a policy) and looks, block by
block (a block is a paragraph, a list or a table, a heading starts a new one), for twelve
statements. The references are the checker author's reading of Regulation (EU) 2024/2847
and are not legal advice; ``Annex I II(n)`` is Annex I, Part II, point n:

* ``contact``: a private way to report a vulnerability (a GitHub advisory form, a real
  e-mail address, a reporting URL) - Annex I II(5) and II(6), Art. 13(17), Annex II(2);
* ``contact-choice``: at least two kinds of channel, because the point of contact must let
  users choose how to write and must not be only an automated tool - Art. 13(17);
* ``cvd-policy``: a coordinated vulnerability disclosure policy (the word, or disclosure
  together with a time frame) - Annex I II(5), Art. 13(8), Annex II(2);
* ``support-period``: the end of the support period with at least month and year
  (``2031-12``, ``12/2031``, ``грудень 2031``, ``December 2031``, ``diciembre de 2031``) -
  Art. 13(8), Art. 13(19), Annex II(7);
* ``support-minimum``: a stated support period shorter than five years - Art. 13(8);
* ``fix-process``: how security fixes are made and released - Annex I II(2) and II(8);
* ``free-updates``: security updates are free of charge - Annex I II(8);
* ``update-retention``: a security update stays available at least ten years or for the rest
  of the support period - Art. 13(9);
* ``advisories``: fixed vulnerabilities are published (advisory, CVE, GHSA) - Annex I II(4);
* ``update-integrity``: updates are distributed in a secure way (signed packages or
  repository) - Annex I II(7);
* ``sbom``: where the SBOM can be found (a link or a path) - Annex I II(1), Annex II(9);
* ``exploited-reporting``: how actively exploited vulnerabilities are reported to the CSIRT
  and ENISA (Art. 14 applies since 2026-09-11).

A statement that is not found is a warning that names the item (``path: warning: ...``).
If ``docs/roadmap.md`` only promises it, the warning says where. An error is only a certain
fault: ``SECURITY.md`` is not readable UTF-8, it still holds a placeholder (``TODO``,
``example.com``), a ``mailto:`` without an address, or a link in a block that answers
``sbom``, ``cvd-policy`` or ``support-period`` points at a file that is not tracked. An end
date of the support period in the past is a warning. Fenced code and unchecked task items
(``- [ ]``) do not count as evidence. Only ``git ls-files`` is run; nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_security_docs.py [--report] [--strict] [--today YYYY-MM-DD]
``--report`` lists every statement with the place that answers it; ``--strict`` makes
warnings fail the check too. Exit code: 0 all good (warnings allowed), 1 problems found,
2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SECURITY = "SECURITY.md"
ROADMAP = "docs/roadmap.md"
SKIPPED_PREFIXES = ("docs/design/",)
MIN_SUPPORT_YEARS = 5
MIN_RETENTION_YEARS = 10

FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}#{1,6}\s")
UNCHECKED_ITEM = re.compile(r"^(\s*)[-*+]\s+\[ \]\s")
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
MAILTO = re.compile(r"mailto:([^)\s>\"'<]*)", re.IGNORECASE)
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PLACEHOLDER_HOST = re.compile(
    r"@(?:[\w.-]*\.)?(?:example\.(?:com|org|net)|localhost|invalid)\b", re.I
)
PLACEHOLDER = re.compile(
    r"\b(?:TODO|TBD|FIXME|REPLACE_ME|CHANGEME)\b|<your[- ][^>]*>|\[your[- ][^\]]*\]"
    r"|example\.(?:com|org|net)\b|your-email|email@domain|ЗАПОВНИТИ|ВКАЖІТЬ",
    re.IGNORECASE,
)
ADVISORY_FORM = re.compile(r"github\.com/[\w.-]+/[\w.-]+/security/advisories/new", re.I)
PUBLIC_TRACKER = re.compile(r"github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)", re.IGNORECASE)
URL = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r"|січ\w*|лют\w*|берез\w*|квіт\w*|трав\w*|черв\w*|лип\w*|серп\w*|верес\w*|жовт\w*"
    r"|листопад\w*|груд\w*"
    r"|ene(?:ro)?|febrero|marzo|abr(?:il)?|mayo|jun(?:io)?|jul(?:io)?|ago(?:sto)?"
    r"|se(?:p)?t(?:iembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?"
)
MONTH_YEAR = (
    re.compile(r"(?<!\d)(20\d\d)-(0[1-9]|1[0-2])(?!\d)"),
    re.compile(r"(?<![\d/.])(0?[1-9]|1[0-2])[./](20\d\d)(?!\d)"),
    re.compile(rf"(?<!\w)(?:{_MONTHS})\.?\s+(?:de\s+|del\s+)?(20\d\d)(?!\d)", re.IGNORECASE),
)
ANY_YEAR = re.compile(r"(?<![\d-])(20[2-9]\d)(?![\d-])")
NUMBER_WORDS = {
    "two": 2, "два": 2, "дві": 2, "dos": 2,
    "three": 3, "три": 3, "tres": 3,
    "four": 4, "чотири": 4, "cuatro": 4,
    "five": 5, "п'ять": 5, "п’ять": 5, "пʼять": 5, "cinco": 5,
    "six": 6, "шість": 6, "seis": 6,
    "seven": 7, "сім": 7, "siete": 7,
    "eight": 8, "вісім": 8, "ocho": 8,
    "ten": 10, "десять": 10, "diez": 10,
}  # fmt: skip
_UNITS = r"(?:years?|років|роки|рік|року|años?)"
DURATION = re.compile(
    rf"(?<![\w.-])(\d{{1,2}}|{'|'.join(map(re.escape, NUMBER_WORDS))})[\s-]*{_UNITS}(?!\w)",
    re.IGNORECASE,
)


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


VULN = rx(r"vulnerab|вразливост|vulnerabilidad|security|безпек|seguridad")
UPDATE = rx(r"updat|оновлен|actualiz|patch|парч|виправл|\bfix|parche")
SECURITY_WORD = rx(r"secur|безпек|вразливост|seguridad|vulnerab")
SUPPORT_KEYWORD = rx(
    r"support period|supported until|end[- ]of[- ](?:life|support)|security support"
    r"|periodo de soporte|per[ií]odo de soporte|soporte (?:hasta|de seguridad)|fin del soporte"
    r"|строк\w* підтримки|період\w* підтримки|підтримуємо|підтримк\w* (?:до|триває|діє)"
    r"|підтримується|кінець підтримки|завершення підтримки"
    r"|supported (?:version|release)s?|підтримувані версії|versiones soportadas"
)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    line: int = 0
    warning: bool = False

    def __str__(self) -> str:
        line = f" line {self.line}:" if self.line else ""
        kind = " warning:" if self.warning else ""
        return f"{self.path}:{line}{kind} {self.message}"


@dataclass(frozen=True)
class Block:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class Requirement:
    key: str
    ref: str
    summary: str
    matches: Callable[[Block], bool]


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    # requirement key -> (path, line) of the first block that answers it
    answered: dict[str, tuple[str, int]] = field(default_factory=dict)


def all_of(*patterns: re.Pattern[str]) -> Callable[[Block], bool]:
    return lambda block: all(p.search(block.text) for p in patterns)


def any_alt(*alternatives: Callable[[Block], bool]) -> Callable[[Block], bool]:
    return lambda block: any(alt(block) for alt in alternatives)


def real_emails(text: str) -> list[str]:
    return [m.group(0) for m in EMAIL.finditer(text) if not PLACEHOLDER_HOST.search(m.group(0))]


def channel_kinds(text: str) -> set[str]:
    """The kinds of reporting channel a block offers: advisory-form, email, url."""
    kinds: set[str] = set()
    if ADVISORY_FORM.search(text):
        kinds.add("advisory-form")
    if real_emails(text):
        kinds.add("email")
    reporting = rx(r"report|повідом|reportar|notific|contact|зв['’]яз|contacto")
    for match in URL.finditer(text):
        url = match.group(0)
        if (
            reporting.search(text)
            and not ADVISORY_FORM.search(url)
            and not PUBLIC_TRACKER.search(url)
        ):
            kinds.add("url")
    return kinds


def is_contact(block: Block) -> bool:
    return bool(VULN.search(block.text)) and bool(channel_kinds(block.text))


def durations(text: str) -> list[int]:
    result = []
    for match in DURATION.finditer(text):
        word = match.group(1).casefold()
        result.append(int(word) if word.isdigit() else NUMBER_WORDS[word])
    return result


def month_years(text: str) -> list[tuple[int, int]]:
    """(year, month) for every month-and-year date; month 0 when only a name is known."""
    found: list[tuple[int, int]] = []
    for match in MONTH_YEAR[0].finditer(text):
        found.append((int(match.group(1)), int(match.group(2))))
    for match in MONTH_YEAR[1].finditer(text):
        found.append((int(match.group(2)), int(match.group(1))))
    for match in MONTH_YEAR[2].finditer(text):
        found.append((int(match.group(1)), 0))
    return found


def is_support_end(block: Block) -> bool:
    return bool(SUPPORT_KEYWORD.search(block.text)) and bool(month_years(block.text))


def is_short_support(block: Block) -> bool:
    if not SUPPORT_KEYWORD.search(block.text):
        return False
    stated = durations(block.text)
    return bool(stated) and max(stated) < MIN_SUPPORT_YEARS


def is_retention(block: Block) -> bool:
    stated = durations(block.text)
    return (
        bool(UPDATE.search(block.text))
        and bool(SECURITY_WORD.search(block.text))
        and any(n >= MIN_RETENTION_YEARS for n in stated)
    )


def is_sbom(block: Block) -> bool:
    keyword = rx(
        r"\bsboms?\b|\bspdx\b|cyclonedx|software bill of materials|перелік\w* компонент"
        r"|lista de materiales de software"
    )
    location = re.compile(r"https?://|\]\(|`[^`\n]*\.(?:json|md|spdx)`|`[^`\n]*sbom[^`\n]*`", re.I)
    return bool(keyword.search(block.text)) and bool(location.search(block.text))


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "contact",
        "Annex I II(5), II(6); Art. 13(17); Annex II(2)",
        "no private way to report a vulnerability (advisory form, e-mail or a reporting URL)",
        is_contact,
    ),
    Requirement(
        "cvd-policy",
        "Annex I II(5); Art. 13(8); Annex II(2)",
        "no coordinated vulnerability disclosure policy (or disclosure with a time frame)",
        any_alt(
            all_of(
                rx(
                    r"coordinated|скоординован|responsible disclosure|відповідальн\w+ розкритт"
                    r"|divulgaci\w+ (?:coordinada|responsable)|\bCVD\b"
                ),
                VULN,
            ),
            all_of(
                rx(r"disclos|розкри|розголош|divulg"),
                rx(r"\bdays\b|днів|дні\b|дня\b|тижн|\bweeks?\b|días|semanas|embargo"),
                VULN,
            ),
        ),
    ),
    Requirement(
        "support-period",
        "Art. 13(8), Art. 13(19); Annex II(7)",
        "no end date of the support period with at least month and year",
        is_support_end,
    ),
    Requirement(
        "support-minimum",
        "Art. 13(8)",
        "the support period is shorter than five years",
        is_short_support,
    ),
    Requirement(
        "fix-process",
        "Annex I II(2), II(8)",
        "no statement of how security fixes are made and released",
        all_of(
            rx(
                r"security (?:update|fix|patch|release)|patch(?:es)? for vulnerab"
                r"|виправлення (?:вразливост|безпеки)|оновлення безпеки"
                r"|actualizaci\w+ de seguridad|parche\w* de seguridad"
            ),
            rx(
                r"releas|випуска|випуск|publish|опублік|публікуєм|ship|deliver|доставля"
                r"|розповсюдж|distribut|publicamos|lanzamos|канал|channel"
            ),
        ),
    ),
    Requirement(
        "free-updates",
        "Annex I II(8)",
        "no statement that security updates are free of charge",
        all_of(
            UPDATE,
            SECURITY_WORD,
            rx(
                r"free of charge|at no (?:extra )?cost|without charge|безкоштовн|без оплати"
                r"|gratis|gratuit|sin (?:coste|cargo)"
            ),
        ),
    ),
    Requirement(
        "update-retention",
        "Art. 13(9)",
        "no statement that a security update stays available for ten years or the support period",
        is_retention,
    ),
    Requirement(
        "advisories",
        "Annex I II(4)",
        "no statement that fixed vulnerabilities are published (advisory, CVE, GHSA)",
        all_of(
            rx(
                r"advisor|бюлетен|повідомлен\w* про вразливост|GHSA-|CVE-|\bOSV\b"
                r"|avisos? de seguridad"
            ),
            rx(
                r"publish|опублік|публікуєм|publicamos|public disclosure|disclos|розкрива"
                r"|розкриваєм|divulg"
            ),
            rx(r"fix\w*|patch|виправл|після|after|once|corregid"),
        ),
    ),
    Requirement(
        "update-integrity",
        "Annex I II(7)",
        "no statement that updates are distributed in a secure way (for example signed)",
        all_of(
            rx(r"\bsigned\b|signature|підпис|\bgpg\b|\bpgp\b|cosign|sigstore|firmad|firma\b"),
            rx(r"updat|оновлен|actualiz"),
        ),
    ),
    Requirement(
        "sbom",
        "Annex I II(1); Annex II(9)",
        "no statement of where the SBOM can be found (a link or a path)",
        is_sbom,
    ),
    Requirement(
        "exploited-reporting",
        "Art. 14 (applies since 2026-09-11)",
        "no statement of how actively exploited vulnerabilities are reported (CSIRT, ENISA)",
        any_alt(
            all_of(rx(r"\bENISA\b|\bCSIRT\b"), VULN),
            all_of(
                rx(r"actively exploited|активно експлуатован|activamente explotad"),
                rx(r"report|повідом|notif|informar"),
            ),
        ),
    ),
)
REQUIREMENT_KEYS = frozenset(req.key for req in REQUIREMENTS)
# Blocks that answer these must not point at a file that is not tracked.
LINK_CHECKED = ("sbom", "cvd-policy", "support-period")
# A statement that is a fault when it IS found (all others are faults when missing).
FAULT_WHEN_FOUND = frozenset({"support-minimum"})
# For these a single roadmap line that matches the statement is a fair hint that it is only planned.
NO_PLAN_HINT = frozenset({"free-updates", "support-minimum"})


def iter_unfenced(text: str) -> Iterator[tuple[int, str]]:
    closing: str | None = None
    for index, line in enumerate(text.splitlines(), 1):
        opened = FENCE_OPEN.match(line)
        if closing is not None:
            if opened and opened.group(1)[0] == closing[0] and len(opened.group(1)) >= len(closing):
                closing = None
            continue
        if opened:
            closing = opened.group(1)
            continue
        yield index, line


def split_blocks(path: str, text: str, skip_planned: bool = True) -> list[Block]:
    """Paragraphs, lists and tables outside fenced code; a heading starts a new block."""
    blocks: list[Block] = []
    lines: list[str] = []
    start = 0
    planned_indent: int | None = None

    def flush() -> None:
        nonlocal lines, start
        if lines:
            blocks.append(Block(path, start, "\n".join(lines)))
        lines, start = [], 0

    for index, line in iter_unfenced(text):
        if not line.strip():
            flush()
            planned_indent = None
            continue
        indent = len(line) - len(line.lstrip())
        if skip_planned:
            if planned_indent is not None and indent > planned_indent:
                continue
            planned_indent = None
            unchecked = UNCHECKED_ITEM.match(line)
            if unchecked:
                planned_indent = len(unchecked.group(1))
                continue
        if HEADING.match(line):
            flush()
        if not lines:
            start = index
        lines.append(line)
    flush()
    return blocks


def read_text(root: Path, rel: str) -> tuple[str | None, str | None]:
    try:
        data = (root / rel).read_bytes()
    except OSError as err:
        return None, f"cannot read the file: {err}"
    if data.startswith(b"\xef\xbb\xbf"):
        return None, "the file starts with a byte order mark (BOM)"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "the file is not valid UTF-8"


def doc_paths(tracked: Sequence[str]) -> list[str]:
    paths = []
    for rel in sorted({p.replace("\\", "/") for p in tracked}):
        in_docs = (
            rel.startswith("docs/")
            and rel.endswith(".md")
            and rel != ROADMAP
            and not rel.startswith(SKIPPED_PREFIXES)
        )
        if rel == SECURITY or in_docs:
            paths.append(rel)
    return paths


def tracked_targets(tracked: Sequence[str]) -> tuple[set[str], set[str]]:
    files = {posixpath.normpath(p.replace("\\", "/")) for p in tracked}
    dirs: set[str] = set()
    for rel in files:
        parent = posixpath.dirname(rel)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return files, dirs


def bad_links(block: Block, files: set[str], dirs: set[str]) -> list[str]:
    missing = []
    for match in LINK.finditer(block.text):
        target = match.group(1)
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith(("#", "//")):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        joined = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.join(posixpath.dirname(block.path), target)
        )
        normal = posixpath.normpath(joined).rstrip("/")
        if normal not in files and normal not in dirs:
            missing.append(match.group(1))
    return missing


def security_file_faults(text: str) -> list[Problem]:
    problems = []
    for index, line in iter_unfenced(text):
        found = PLACEHOLDER.search(line)
        if found:
            problems.append(
                Problem(SECURITY, f"placeholder left in the text: {found.group(0)!r}", index)
            )
        for mail in MAILTO.finditer(line):
            address = mail.group(1).split("?", 1)[0]
            if not EMAIL.fullmatch(address):
                problems.append(
                    Problem(
                        SECURITY, f"mailto link without a valid address: {mail.group(0)!r}", index
                    )
                )
    return problems


def parse_today(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def support_end_problem(blocks: Sequence[Block], today: date) -> Problem | None:
    """A warning if every end date of the blocks that answer ``support-period`` has passed."""
    dated = [(block, month_years(block.text)) for block in blocks if is_support_end(block)]
    if not dated:
        return None
    latest: tuple[int, int] | None = None
    place = dated[0][0]
    for block, stamps in dated:
        for year, month in stamps:
            key = (year, month if month else 12)
            if latest is None or key > latest:
                latest, place = key, block
    if latest is not None and latest < (today.year, today.month):
        return Problem(
            place.path,
            f"the support period ends {latest[0]}-{latest[1]:02d}, which is already over "
            f"(Art. 13(8), Art. 13(19))",
            place.line,
            warning=True,
        )
    return None


def year_only_note(blocks: Sequence[Block]) -> str:
    for block in blocks:
        partial = SUPPORT_KEYWORD.search(block.text) and not month_years(block.text)
        if partial and (ANY_YEAR.search(block.text) or durations(block.text)):
            return f" ({block.path}:{block.line} names a year or a length, but not month and year)"
    return ""


def check_files(root: Path, tracked: Sequence[str], today: date | None = None) -> Report:
    today = today or date.today()
    report = Report()
    files, dirs = tracked_targets(tracked)
    paths = doc_paths(tracked)
    blocks: list[Block] = []

    if SECURITY not in paths:
        report.problems.append(
            Problem(
                SECURITY,
                "the policy file is missing: no SECURITY.md is tracked (Annex II(2), Art. 13(17))",
                warning=True,
            )
        )
    for rel in paths:
        text, failure = read_text(root, rel)
        if text is None:
            report.problems.append(
                Problem(rel, failure or "cannot read the file", warning=rel != SECURITY)
            )
            continue
        if rel == SECURITY:
            report.problems.extend(security_file_faults(text))
        blocks.extend(split_blocks(rel, text))

    plans: list[Block] = []
    if ROADMAP in {p.replace("\\", "/") for p in tracked}:
        text, _failure = read_text(root, ROADMAP)
        if text is not None:
            plans = [
                Block(ROADMAP, index, line) for index, line in iter_unfenced(text) if line.strip()
            ]

    contact_kinds: set[str] = set()
    for req in REQUIREMENTS:
        hits = [block for block in blocks if req.matches(block)]
        if hits:
            report.answered[req.key] = (hits[0].path, hits[0].line)
        if req.key == "contact":
            for block in hits:
                if block.path == SECURITY:
                    contact_kinds |= channel_kinds(block.text)
        if req.key in LINK_CHECKED:
            for block in hits:
                for target in bad_links(block, files, dirs):
                    report.problems.append(
                        Problem(
                            block.path,
                            f"the link {target!r} in the block that answers {req.key} "
                            f"points at a file that is not tracked",
                            block.line,
                        )
                    )
        if req.key in FAULT_WHEN_FOUND:
            for block in hits:
                report.problems.append(
                    Problem(
                        block.path,
                        f"{req.summary} ({req.ref}); the CRA allows a shorter period only when "
                        f"the product is expected to be used for less time",
                        block.line,
                        warning=True,
                    )
                )
            continue
        if hits:
            continue
        message = f"{req.summary} (CRA {req.ref})"
        if req.key == "support-period":
            message += year_only_note(blocks)
        for plan in () if req.key in NO_PLAN_HINT else plans:
            if req.matches(plan) or (req.key == "sbom" and rx(r"\bsbom").search(plan.text)):
                message += f"; {plan.path}:{plan.line} only plans it"
                break
        report.problems.append(Problem(SECURITY, message, warning=True))

    if "contact" in report.answered and len(contact_kinds) == 1:
        (kind,) = contact_kinds
        report.problems.append(
            Problem(
                SECURITY,
                f"only one kind of reporting channel ({kind}); the single point of contact "
                f"should let users choose how to write and not be only an automated tool "
                f"(Art. 13(17))",
                report.answered["contact"][1],
                warning=True,
            )
        )
    expired = support_end_problem(blocks, today)
    if expired is not None:
        report.problems.append(expired)
    return report


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(path for path in out.split("\0") if path)


def format_report(report: Report) -> str:
    lines = ["requirement | status | where | CRA"]
    for req in REQUIREMENTS:
        place = report.answered.get(req.key)
        if req.key in FAULT_WHEN_FOUND:
            status = "found" if place else "ok"
        else:
            status = "answered" if place else "missing"
        where = f"{place[0]}:{place[1]}" if place else "-"
        lines.append(f"{req.key} | {status} | {where} | {req.ref}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python3 tools/check_security_docs.py [--report] [--strict] [--today YYYY-MM-DD]"
    show_report = strict = False
    today: date | None = None
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            print(usage)
            return 0
        if arg == "--report":
            show_report = True
        elif arg == "--strict":
            strict = True
        elif arg == "--today" and args:
            today = parse_today(args.pop(0))
            if today is None:
                print(f"--today needs a date like 2026-11-27\n{usage}", file=sys.stderr)
                return 2
        else:
            print(usage, file=sys.stderr)
            return 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    report = check_files(ROOT, tracked, today)
    for problem in report.problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in report.problems)
    warnings = sum(p.warning for p in report.problems)
    if show_report:
        print(format_report(report))
    scanned = len(doc_paths(tracked))
    print(f"{scanned} security and docs files checked: {errors} errors, {warnings} warnings")
    return 1 if errors or (strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
