# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice models on disk: downloaded only after a yes, checked, unpacked safely.

Layout, next to the language model (``gloxi.engine.models_dir``)::

    ~/.local/share/gloxi/models/voice/
        sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/   unpacked archive
        silero-vad/silero_vad.onnx                    single file
        <folder>/.gloxi-pack                          the SHA-256 it came from

An archive is checked against its pinned SHA-256 before it is opened
(``gloxi.engine.download``), unpacked into a temporary folder and only then
moved into place, so a folder that exists is complete. Unpacking refuses
links, devices, absolute paths and ``..`` (the tar "data" filter) and stops
past a size limit, even though the archive's hash is already known.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

from gloxi.engine import EngineError, Progress, download, models_dir
from gloxi.voice.models import Pack

MARKER = ".gloxi-pack"
# Unpacked models are 1.0-1.4 times their archive; more means something is wrong.
MAX_UNPACKED_RATIO = 3


def voice_dir(env: dict[str, str] | None = None) -> Path:
    return models_dir(env) / "voice"


def installed(pack: Pack, base: Path) -> Path | None:
    """The pack's folder if it was installed whole from the pinned file."""
    folder = base / pack.folder
    try:
        if (folder / MARKER).read_text(encoding="utf-8").strip() == pack.file.sha256:
            return folder
    except OSError:
        return None
    return None


def missing(packs: tuple[Pack, ...], base: Path) -> tuple[Pack, ...]:
    return tuple(p for p in packs if installed(p, base) is None)


def install(
    pack: Pack,
    base: Path,
    *,
    progress: Progress = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    fetch: Callable[..., Path] = download,
) -> Path:
    """Download ``pack`` (resuming), verify it and put it in place."""
    found = installed(pack, base)
    if found is not None:
        return found
    downloads = base / ".downloads"
    path = fetch(pack.file, downloads, progress=progress, cancelled=cancelled)
    folder = base / pack.folder
    tmp = base / f".{pack.folder}.unpacking"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        if pack.archive:
            unpack(path, tmp, pack.folder, limit=pack.size * MAX_UNPACKED_RATIO)
        else:
            tmp.mkdir(parents=True)
            shutil.copyfile(path, tmp / pack.file.filename)
        (tmp / MARKER).write_text(pack.file.sha256 + "\n", encoding="utf-8")
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(tmp, folder)
    except OSError as exc:
        raise EngineError(f"the voice model could not be unpacked: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    path.unlink(missing_ok=True)
    return folder


def unpack(archive: Path, dest: Path, top: str, *, limit: int) -> None:
    """Unpack the ``top/`` folder of a .tar.bz2 into ``dest`` (without ``top``)."""
    dest.mkdir(parents=True)
    total = 0
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                if name == top or not name.startswith(top + "/"):
                    continue
                if not (member.isfile() or member.isdir()):
                    raise EngineError(f"unexpected entry in the voice model: {member.name}")
                total += max(member.size, 0)
                if total > limit:
                    raise EngineError("the voice model is larger than expected")
                member.name = name.removeprefix(top + "/")
                tar.extract(member, dest, filter="data")
    except (tarfile.TarError, EOFError) as exc:
        raise EngineError(f"the voice model archive is damaged: {exc}") from exc


def remove(pack: Pack, base: Path) -> None:
    """Delete an installed pack (the "Remove voice models" button)."""
    shutil.rmtree(base / pack.folder, ignore_errors=True)
