# SPDX-License-Identifier: GPL-3.0-or-later
"""Local crash reports (ADR 0018, Gloxi 0.1).

When Gloxi stops on an unhandled error, it writes a short report to
``$XDG_STATE_HOME/gloxi/crashes/``. Nothing is sent anywhere: ``/feedback``
offers to add the latest one to the feedback form, where the person sees it.

What a report holds: the Gloxi version, the type of the error and the
traceback lines from our own packages only (``gloxi``, ``herkos``,
``privacy_gateway``), as ``package/file.py:line in function``. Never the
error message (it may quote what the person wrote), local variables, other
packages' frames or full paths.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from gloxi import __version__

OUR_PACKAGES = ("gloxi", "herkos", "privacy_gateway")
MAX_REPORTS = 10
MAX_FRAMES = 30
# /feedback offers a report this recent.
RECENT = 7 * 86400


def crash_dir() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value) if value and os.path.isabs(value) else Path.home() / ".local/state"
    return base / "gloxi" / "crashes"


def _our_frame(filename: str) -> str | None:
    """``gloxi/chat.py`` for a file inside one of our packages, else None."""
    parts = Path(filename).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in OUR_PACKAGES:
            return "/".join(parts[i:])
    return None


def report_text(
    exc_type: type[BaseException], tb: TracebackType | None, *, when: float | None = None
) -> str:
    """The report: facts only, in English, as in a bug report."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when or time.time()))
    lines = [f"Gloxi {__version__} crashed at {stamp}", f"Error: {exc_type.__name__}"]
    frames = []
    for frame in traceback.extract_tb(tb):
        where = _our_frame(frame.filename)
        if where:
            frames.append(f"  {where}:{frame.lineno} in {frame.name}")
    lines += frames[-MAX_FRAMES:] or ["  (no frames in Gloxi's own code)"]
    return "\n".join(lines) + "\n"


def save(text: str, folder: Path | None = None, *, when: float | None = None) -> Path | None:
    """Write one report, keeping the newest ``MAX_REPORTS``. None if it cannot."""
    folder = folder or crash_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / time.strftime("crash-%Y%m%d-%H%M%S.txt", time.gmtime(when or time.time()))
        path.write_text(text, encoding="utf-8")
        for old in sorted(folder.glob("crash-*.txt"))[:-MAX_REPORTS]:
            old.unlink(missing_ok=True)
    except OSError:
        return None
    return path


@dataclass(frozen=True)
class Crash:
    path: Path
    time: float
    text: str


def latest(folder: Path | None = None, *, now: float | None = None) -> Crash | None:
    """The newest report from the last ``RECENT`` seconds, if any."""
    folder = folder or crash_dir()
    now = time.time() if now is None else now
    try:
        reports = sorted(folder.glob("crash-*.txt"))
        if not reports:
            return None
        path = reports[-1]
        when = path.stat().st_mtime
        if now - when > RECENT:
            return None
        return Crash(path, when, path.read_text(encoding="utf-8")[:4000])
    except OSError:
        return None


def install(folder: Path | None = None) -> None:
    """Write a report for any unhandled error (any thread), then let Python print it as usual."""
    previous: Callable = sys.excepthook

    def hook(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            save(report_text(exc_type, tb), folder)
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    previous_thread = threading.excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            save(report_text(args.exc_type, args.exc_traceback), folder)
        previous_thread(args)

    threading.excepthook = thread_hook
