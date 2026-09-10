"""Shared structured CLI boundary for the retained workflow engines."""

import threading
import time

from .events import parse_events
from .policies import PolicyError, Role, build_command, child_environment, require_git_workspace, role_for_agent
from .processes import run_process
from .recovery import RunBudget
from .workspace import fingerprint


def execute(
    prompt: str, executable: str, agent: str, workspace: str,
    *, budget: RunBudget, session_id: str | None = None,
    timeout: float = 600, cancel: threading.Event | None = None,
) -> tuple[str, str, int, float, str | None]:
    started = time.monotonic()
    try:
        remaining = budget.reserve()
        root = require_git_workspace(workspace)
        role = role_for_agent(agent)
        before = fingerprint(root) if role is Role.REVIEW else None
        if role is Role.REVIEW:
            prompt = (
                "Return the complete review as your final response. Do not write any files. "
                "The orchestrator captures your response and writes the review artifact. "
                "Any older instruction to save a file is superseded by this transport contract.\n\n"
                + prompt
            )
        result = run_process(
            build_command(executable, role, workspace, session_id),
            cwd=root, env=child_environment(), input_text=prompt,
            timeout=min(timeout, remaining), cancel=cancel,
        )
        if before is not None and fingerprint(root) != before:
            return "", "candidate changed during review", 1, result.duration, session_id
        if result.returncode in {-1, -2}:
            return "", result.stderr, result.returncode, result.duration, session_id
        parsed = parse_events(result.stdout, result.stderr, result.returncode)
        # Never let a partial final message be treated as an approved result.
        output = parsed.output if parsed.returncode == 0 else ""
        return output, parsed.error, parsed.returncode, result.duration, parsed.session_id or session_id
    except (PolicyError, OSError, ValueError, RuntimeError) as exc:
        # No raw paths, prompts, environment values, or provider output in diagnostics.
        category = "budget exhausted" if str(exc) == "budget exhausted" else "execution policy or transport failure"
        return "", category, -2, time.monotonic() - started, session_id
