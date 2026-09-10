"""Offline-first package commands. Live engines have separate entry points."""

import argparse
import json
from pathlib import Path
import re
import time

from . import __version__
from .demo import run_demo
from .metadata import state_base
from .policies import Role, build_command
from .workspace import is_linklike, prepare_worktree


def purge_diagnostics(*, older_than_days: int, apply: bool = False) -> int:
    """Only remove known metadata files from eligible app-owned run folders.

    Unknown files, links and unexpected names are left untouched. This never
    removes target-project results, resume state, or Codex's own session data.
    """
    if not 1 <= older_than_days <= 3650:
        raise ValueError("Retention must be between 1 and 3650 days")
    root = state_base() / "diagnostics"
    if not root.exists():
        return 0
    if any(is_linklike(node) for node in (root, *root.parents)):
        raise ValueError("Refusing linked diagnostics directory")
    cutoff = time.time() - older_than_days * 86400
    count = 0
    for directory in root.iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", directory.name):
            continue
        if is_linklike(directory) or not directory.is_dir():
            continue
        files = list(directory.iterdir())
        if len(files) != 1 or files[0].name != "events.jsonl":
            continue
        artifact = files[0]
        if is_linklike(artifact) or not artifact.is_file() or artifact.stat().st_nlink != 1:
            continue
        if artifact.stat().st_mtime >= cutoff:
            continue
        count += 1
        if apply:
            artifact.unlink()
            directory.rmdir()
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex orchestration contracts and offline diagnostics")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run synthetic offline boundary checks; no account or network")
    policy = subparsers.add_parser("policy", help="Explain effective role policy without starting Codex")
    policy.add_argument("--role", choices=[role.value for role in Role], default="review")
    policy.add_argument("--workspace", type=Path, default=Path("."))
    prepare = subparsers.add_parser(
        "prepare-worktree", help="Create one isolated branch from a clean Git checkout"
    )
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--destination", type=Path, required=True)
    prepare.add_argument("--branch", required=True, help="orchestrators/<run-name>")
    purge = subparsers.add_parser("purge", help="Preview removal of old diagnostic metadata only")
    purge.add_argument("--older-than-days", type=int, default=30)
    purge.add_argument(
        "--apply", action="store_true", help="Remove eligible metadata; default only counts it"
    )
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(json.dumps(run_demo(), indent=2))
    elif args.command == "policy":
        print(
            json.dumps(
                {
                    "role": args.role,
                    "argv": build_command("codex", Role(args.role), str(args.workspace)),
                    "project_verification": "blocked",
                    "diagnostic_content": "metadata-only",
                    "max_calls_per_instance": 24,
                    "max_call_seconds": 600,
                    "max_instance_seconds": 7200,
                    "dollar_budget": "not enforced; use provider spend limits",
                },
                indent=2,
            )
        )
    elif args.command == "prepare-worktree":
        try:
            base = prepare_worktree(args.source, args.destination, args.branch)
        except (ValueError, OSError) as exc:
            parser.exit(2, f"Workspace refused: {exc}\n")
        print(json.dumps({"base_commit": base, "branch": args.branch, "original_checkout_modified": False}))
    else:
        try:
            count = purge_diagnostics(older_than_days=args.older_than_days, apply=args.apply)
        except (ValueError, OSError) as exc:
            parser.exit(2, f"Purge refused: {type(exc).__name__}\n")
        print(
            json.dumps({"eligible_runs": count, "removed": args.apply, "scope": "diagnostic metadata only"})
        )
    return 0
