"""Version preflight for the native CLI contract inspected during hardening."""

from pathlib import Path
import re

from .policies import PolicyError, child_environment
from .processes import run_process

# Narrow by design: broaden only with recorded command/event/sandbox evidence.
INSPECTED_CODEX_VERSION = "0.153.4"


def check_version(executable: str, cwd: Path) -> str:
    if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise PolicyError("A native Codex executable is required")
    result = run_process(
        [executable, "--version"], cwd=cwd, env=child_environment(), timeout=10, max_output_bytes=8192
    )
    match = re.fullmatch(r"codex-cli\s+(\d+\.\d+\.\d+)\s*", result.stdout)
    if result.returncode != 0 or not match or match[1] != INSPECTED_CODEX_VERSION:
        raise PolicyError("Unsupported Codex CLI version; see docs/COMPATIBILITY.md")
    return match[1]
