"""Fail-closed authority selection, independent of model/repository text."""

from enum import Enum
import os
from pathlib import Path
import re


class PolicyError(ValueError):
    """Requested execution does not satisfy the supported policy."""


class Role(str, Enum):
    EDIT = "edit"
    REVIEW = "review"


def role_for_agent(agent: str) -> Role:
    if agent in {"researcher", "executor"}:
        return Role.EDIT
    if agent in {"inspector", "inspector_1", "inspector_2", "inspector2"}:
        return Role.REVIEW
    raise PolicyError("Unknown agent role; no authority granted")


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep launcher/auth-location variables, never arbitrary parent secrets.

    This is not filesystem isolation. Codex authentication remains accessible to
    Codex; use a dedicated OS account or VM when handling untrusted repositories.
    API-key authentication is deliberately not inherited.
    """
    source = os.environ if source is None else source
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "CODEX_HOME",
        "LANG", "LC_ALL",
    }
    result = {key: value for key, value in source.items() if key.upper() in allowed}
    result["PYTHONIOENCODING"] = "utf-8"
    result["NO_COLOR"] = "1"
    return result


def build_command(
    executable: str,
    role: Role,
    workspace: str,
    session_id: str | None = None,
) -> list[str]:
    """Build argv without a shell, arbitrary config, or permission bypass.

    Native executables are required on Windows. A batch launcher introduces
    cmd.exe argument interpretation and is intentionally not supported.
    """
    if not isinstance(role, Role):
        raise PolicyError("Role must be an enumerated policy")
    if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise PolicyError("Select the native Codex executable, not a shell launcher")
    if not executable or "\x00" in executable:
        raise PolicyError("Invalid executable")
    if session_id is not None and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", session_id):
        raise PolicyError("Invalid session identifier")
    sandbox = "read-only" if role is Role.REVIEW else "workspace-write"
    command = [
        executable, "exec", "--json", "--ignore-user-config", "--ignore-rules",
        "-c", 'approval_policy="never"',
        "-c", f'sandbox_mode="{sandbox}"',
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'web_search="disabled"',
    ]
    if session_id:
        command += ["resume", session_id]
    else:
        command += ["--cd", str(Path(workspace).resolve())]
    return command + ["-"]


def require_git_workspace(workspace: str) -> Path:
    """Reject unsupported roots and project-local executable Codex config."""
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir() or not (root / ".git").exists():
        raise PolicyError("A Git repository/worktree root is required")
    # The selected directory must be a Git root, not a nested subdirectory.
    # User-home config is handled by --ignore-user-config, not this check.
    if (root / ".codex").exists():
        raise PolicyError("Project .codex configuration requires isolation and review")
    return root
