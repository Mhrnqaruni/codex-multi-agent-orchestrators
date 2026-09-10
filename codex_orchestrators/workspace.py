"""Bounded candidate fingerprints; changes invalidate review evidence."""

import hashlib
import os
from pathlib import Path
import stat
import tempfile

from .policies import PolicyError


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in dirs + sorted(files):
            path = Path(directory) / name
            if path == root / ".git":
                continue
            info = path.lstat()
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise PolicyError("Candidate contains a link/reparse point")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PolicyError("Candidate contains a non-regular or hard-linked file")
            count += 1
            total += info.st_size
            if count > 20000 or total > 128 * 1024 * 1024:
                raise PolicyError("Candidate exceeds review size limits")
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(str(stat.S_IMODE(info.st_mode)).encode("ascii") + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def write_government_review(root: Path, step: str, phase: int, output: str) -> None:
    """Host-owned artifact destination; never extracted from model text."""
    if step == "init":
        return
    if step == "master_plan":
        relative = Path("master_plan_review.md")
    elif step in {"plan", "exec"} and type(phase) is int and 0 <= phase <= 100:
        relative = Path(f"phase_{phase}") / ("plan_review.md" if step == "plan" else "exec_review.md")
    else:
        raise PolicyError("Unsupported review phase")
    root = root.resolve(strict=True)
    path = root / relative
    for node in (path, *path.parents):
        if node == root:
            break
        if node.is_symlink() or getattr(node, "is_junction", lambda: False)():
            raise PolicyError("Review destination contains a link")
        if node.is_file() and node.stat().st_nlink != 1:
            raise PolicyError("Review destination is hard-linked")
    if len(output.encode("utf-8")) > 2 * 1024 * 1024:
        raise PolicyError("Review artifact exceeds size limit")
    path.parent.mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".review-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
