"""Check tracked working files or exact staged blobs without printing secrets.

This deliberately narrow gate is not a history/PII scanner. Run a dedicated
scanner and manual review before publication. No network or agents are used.
"""

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


MAX_BYTES = 2 * 1024 * 1024
FORBIDDEN_PARTS = {
    "logs",
    ".government",
    ".claude",
    ".codex-orchestrators",
    "congress_rounds",
    "__pycache__",
    ".venv",
    "node_modules",
}
FORBIDDEN_NAMES = {
    "update.md",
    "session.json",
    "session_request.md",
    "congress_state.json",
    "congress.lock",
    "accessTokens.json",
}
FORBIDDEN_SUFFIXES = {".bundle", ".pem", ".key", ".p12", ".pfx", ".apk"}
PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "google-api-key": re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
    "github-token": re.compile(rb"gh[pousr]_[0-9A-Za-z]{30,}"),
    "github-fine-grained-token": re.compile(rb"github_pat_[0-9A-Za-z_]{40,}"),
    "aws-access-key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}


def inspect_file(name, data, mode="100644"):
    """Return category names only; never return matched secret values."""
    path = PurePosixPath(name)
    findings = []
    if (
        set(path.parts) & FORBIDDEN_PARTS
        or path.name in FORBIDDEN_NAMES
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
        or (path.name.startswith(".env") and path.name != ".env.example")
    ):
        findings.append("private-artifact-path")
    if mode not in {"100644", "100755"}:
        findings.append("unsupported-file-mode")
    if len(data) > MAX_BYTES:
        findings.append("oversized-file")
    if b"\x00" in data:
        findings.append("binary-requires-review")
    findings.extend(label for label, pattern in PATTERNS.items() if pattern.search(data))
    return findings


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=30)


def check_tree(root, staged=False):
    """Inspect the entire index inventory, not only files changed in a PR."""
    findings = []
    root = Path(root).resolve()
    records = git(root, "ls-files", "--stage", "-z").split(b"\0")
    for record in filter(None, records):
        metadata, raw_name = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8", errors="surrogateescape")
        if stage != "0":
            findings.append((name, "unmerged-index"))
            continue
        if mode not in {"100644", "100755"}:
            findings.append((name, "unsupported-file-mode"))
            continue
        if staged:
            size = int(git(root, "cat-file", "-s", oid))
            if size > MAX_BYTES:
                findings.append((name, "oversized-file"))
                continue
            data = git(root, "cat-file", "blob", oid)
        else:
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                findings.append((name, "symlink-or-escaped-path"))
                continue
            if not path.exists():
                continue  # Pending deletion; --staged validates actual commit content.
            if path.stat().st_size > MAX_BYTES:
                findings.append((name, "oversized-file"))
                continue
            data = path.read_bytes()
        findings.extend((name, label) for label in inspect_file(name, data, mode))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="inspect exact index blobs")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        findings = check_tree(root, staged=args.staged)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Tree inspection failed: {type(error).__name__}", file=sys.stderr)
        return 2
    for name, category in findings:
        print(f"{ascii(name)}: {category}")
    print(f"Tracked-tree findings: {len(findings)}; history not scanned.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
