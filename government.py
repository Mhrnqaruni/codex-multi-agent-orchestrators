"""
GOVERNMENT - Multi-AI Phased Execution System
===============================================
Two persistent Codex agents (Executor + Inspector) work in a loop to plan,
execute, and verify project phases. Each agent maintains a continuous chat
session via codex resume <session_id>.

Usage:
    python government.py
    python government.py --workdir="C:\\MyProject"
    python government.py --max-review-rounds=10
    python government.py --source="C:\\specs\\plan.md"
"""

import subprocess
import sys
import os
import re
import time
import shutil
import threading
import queue
import json
import socket
from pathlib import Path
from datetime import datetime


# ============================================================================
# CONSTANTS
# ============================================================================

MAX_REVIEW_ROUNDS = 7          # soft stop after this many rounds per sub-loop
SILENCE_TIMEOUT = 600          # kill if ZERO output on both pipes for 10 min
STARTUP_TIMEOUT = 120          # kill if no output within 2 min of launch
COOLDOWN_BETWEEN = 5           # seconds between codex calls
FILE_VERIFY_RETRIES = 3        # retry count if expected file not found
AUTO_CONTINUE_TIMEOUT = 300    # auto-continue to next phase after 5 min (0=disabled)
APPROVAL_FLAG = "--dangerously-bypass-approvals-and-sandbox"

# Rate limit / billing keywords (case-insensitive check against stderr)
RATE_LIMIT_SIGNALS = ("usage limit", "rate limit", "rate_limit", "quota",
                      "billing", "try again at", "too many requests", "429")

# Network error keywords (case-insensitive check against stderr)
NETWORK_ERROR_SIGNALS = ("network error", "connection refused", "connection reset",
                         "dns resolution", "etimedout", "econnrefused", "enotfound",
                         "socket hang up", "fetch failed", "econnreset",
                         "unable to connect", "network is unreachable",
                         "no internet", "getaddrinfo")

MAX_RECOVERY_RETRIES = 3       # max retries for network recovery
RATE_LIMIT_AUTO_RETRY_SECONDS = 3600  # auto-retry once per hour while waiting on rate limits

# Terminal colors
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_DIM     = "\033[2m"
C_RED     = "\033[91m"
C_GREEN   = "\033[92m"
C_YELLOW  = "\033[93m"
C_BLUE    = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN    = "\033[96m"

# Box drawing
BOX_H  = "\u2500"
BOX_V  = "\u2502"
BOX_TL = "\u250c"
BOX_TR = "\u2510"
BOX_BL = "\u2514"
BOX_BR = "\u2518"


# ============================================================================
# STDIO HELPERS
# ============================================================================

def _configure_stdio():
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True, write_through=True)
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True, write_through=True)
    except Exception:
        pass


def _print_safe(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args)
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(end.encode("utf-8", errors="replace"))
        sys.stdout.flush()


def _kbhit() -> bool:
    """Non-blocking check if a key has been pressed (cross-platform)."""
    try:
        if os.name == "nt":
            import msvcrt
            return bool(msvcrt.kbhit())
        else:
            if not sys.stdin.isatty():
                return False  # piped/redirected stdin — let auto-continue run
            import select
            return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def _read_key() -> str:
    """Read one keypress, return lowercase char. Returns '' for special keys."""
    try:
        if os.name == "nt":
            import msvcrt
            ch = msvcrt.getch()
            if ch in (b'\xe0', b'\x00'):
                msvcrt.getch()  # consume second byte of special key
                return ""
            return ch.decode("utf-8", errors="replace").lower()
        else:
            ch = sys.stdin.read(1)
            return ch.lower() if ch else ""
    except Exception:
        return ""


def _consume_key():
    """Consume one keypress from the input buffer (discard it)."""
    _read_key()


def _flush_input():
    """Flush any buffered keypresses to avoid stale input triggering menus."""
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            if not sys.stdin.isatty():
                return
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def _enter_cbreak():
    """Enter cbreak mode on Unix for single-keypress detection. Returns old settings or None."""
    if os.name == "nt" or not sys.stdin.isatty():
        return None
    try:
        import termios, tty
        old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        return old
    except Exception:
        return None


def _exit_cbreak(old_settings):
    """Restore terminal settings after cbreak mode."""
    if old_settings is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


def _check_internet(timeout=3) -> bool:
    """Quick TCP connectivity check to Anthropic API server."""
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def _strip_ansi(text: str) -> str:
    return re.sub(r'\033\[[0-9;]*m', '', text)


def _is_rate_limited(stderr: str, rc: int) -> tuple[bool, str]:
    """Check if stderr indicates a rate limit / billing error.
    Returns (is_limited, retry_info_string)."""
    if rc == 0:
        return False, ""
    stderr_lower = stderr.lower() if stderr else ""
    if any(sig in stderr_lower for sig in RATE_LIMIT_SIGNALS):
        retry_match = re.search(r'try again at\s+(.+?)[\.\n]', stderr, re.IGNORECASE)
        retry_info = f" Retry after: {retry_match.group(1)}" if retry_match else ""
        return True, retry_info
    return False, ""


def _is_network_error(stderr: str, rc: int) -> bool:
    """Check if stderr indicates a network connectivity error."""
    if rc == 0:
        return False
    stderr_lower = stderr.lower() if stderr else ""
    return any(sig in stderr_lower for sig in NETWORK_ERROR_SIGNALS)


# ============================================================================
# TERMINAL UI
# ============================================================================

class TerminalUI:
    def __init__(self):
        self._width = min(shutil.get_terminal_size().columns, 120)
        if os.name == "nt":
            os.system("")

    def clear(self):
        os.system("cls" if os.name == "nt" else "clear")

    def clear_line(self):
        sys.stdout.write("\r" + " " * (self._width - 1) + "\r")
        sys.stdout.flush()

    def banner(self):
        print()
        print(f"{C_CYAN}{C_BOLD}")
        w = self._width
        print("=" * w)
        print("GOVERNMENT".center(w))
        print("Multi-AI Phased Execution System".center(w))
        print("Executor + Inspector Feedback Loop".center(w))
        print("=" * w)
        print(f"{C_RESET}")

    def box(self, title: str, content: str, color: str = C_CYAN):
        w = self._width - 4
        title_len = len(_strip_ansi(title))
        title_pad = max(0, w - title_len)
        print(f"  {color}{BOX_TL}{BOX_H * (w + 2)}{BOX_TR}{C_RESET}")
        print(f"  {color}{BOX_V}{C_RESET} {C_BOLD}{title}{C_RESET}"
              f"{' ' * title_pad}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_V}{BOX_H * (w + 2)}{BOX_V}{C_RESET}")
        for line in content.split("\n"):
            clean = _strip_ansi(line)
            if len(clean) > w:
                display = clean[:w - 3] + "..."
            else:
                display = line
            vis_len = len(_strip_ansi(display))
            pad = max(0, w - vis_len)
            print(f"  {color}{BOX_V}{C_RESET} {display}{' ' * pad}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_BL}{BOX_H * (w + 2)}{BOX_BR}{C_RESET}")
        print()

    def status(self, msg: str, color: str = C_YELLOW):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  {C_DIM}[{ts}]{C_RESET} {color}{C_BOLD}{msg}{C_RESET}")

    def phase_header(self, phase_num: int, step: str, round_num: int, max_rounds: int):
        print()
        print(f"  {C_CYAN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print(f"  {C_CYAN}{C_BOLD}  Phase {phase_num} | {step} | "
              f"Round {round_num}/{max_rounds}{C_RESET}")
        print(f"  {C_CYAN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print()

    def agent_header(self, agent: str, action: str):
        if agent == "EXECUTOR":
            color, icon = C_BLUE, "[E]"
        else:
            color, icon = C_MAGENTA, "[I]"
        print()
        print(f"  {color}{C_BOLD}{'─' * (self._width - 4)}{C_RESET}")
        print(f"  {color}{C_BOLD}  {icon} {agent} — {action}{C_RESET}")
        print(f"  {color}{C_BOLD}{'─' * (self._width - 4)}{C_RESET}")
        print()

    def stream_line(self, agent: str, line: str):
        if agent == "executor":
            prefix = f"  {C_BLUE}{C_DIM}E |{C_RESET} "
        else:
            prefix = f"  {C_MAGENTA}{C_DIM}I |{C_RESET} "
        max_len = self._width - 10
        display = line.rstrip()
        if len(display) > max_len:
            display = display[:max_len - 3] + "..."
        _print_safe(f"{prefix}{display}")

    def verdict_display(self, verdict: str, round_num: int, max_rounds: int):
        if verdict == "APPROVED":
            color = C_GREEN
            msg = f"APPROVED at round {round_num}!"
        elif round_num >= max_rounds:
            color = C_RED
            msg = f"NEEDS REVISION — round limit ({max_rounds}) reached. Soft stop."
        else:
            color = C_YELLOW
            msg = f"NEEDS REVISION — continuing to round {round_num + 1}"
        print()
        print(f"  {color}{C_BOLD}{'*' * (self._width - 4)}{C_RESET}")
        print(f"  {color}{C_BOLD}  VERDICT: {msg}{C_RESET}")
        print(f"  {color}{C_BOLD}{'*' * (self._width - 4)}{C_RESET}")
        print()

    def soft_stop_prompt(self, context: str, extend_by: int = MAX_REVIEW_ROUNDS) -> str:
        print()
        print(f"  {C_YELLOW}{C_BOLD}{'!' * (self._width - 4)}{C_RESET}")
        print(f"  {C_YELLOW}{C_BOLD}  SOFT STOP — {context}{C_RESET}")
        print(f"  {C_YELLOW}{C_BOLD}{'!' * (self._width - 4)}{C_RESET}")
        print()
        print(f"  Options:")
        print(f"    {C_BOLD}c{C_RESET} = continue (extend by {extend_by} more rounds)")
        print(f"    {C_BOLD}s{C_RESET} = skip to next step")
        print(f"    {C_BOLD}a{C_RESET} = abort")
        print()
        while True:
            try:
                choice = input(f"  {C_CYAN}> Choice [c/s/a]: {C_RESET}").strip().lower()
            except EOFError:
                return "a"  # safe default: abort in non-interactive
            if choice in ("c", "s", "a"):
                return choice
            print(f"  {C_RED}Invalid. Type c, s, or a.{C_RESET}")

    def user_checkpoint(self, phase_num: int, executor_sid: str,
                        inspector_sid: str, auto_timeout: int = 0) -> str:
        print()
        print(f"  {C_GREEN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}  PHASE {phase_num} COMPLETE — USER CHECKPOINT{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print()
        print(f"  To chat with executor manually:")
        print(f"    {C_BOLD}codex resume {executor_sid}{C_RESET}")
        print(f"  To chat with inspector manually:")
        print(f"    {C_BOLD}codex resume {inspector_sid}{C_RESET}")
        print()
        print(f"  Options:")
        print(f"    {C_BOLD}c{C_RESET} = continue to next phase")
        print(f"    {C_BOLD}d{C_RESET} = done (project complete)")
        print(f"    {C_BOLD}a{C_RESET} = abort")
        print()

        # Auto-continue countdown
        if auto_timeout > 0:
            print(f"  {C_DIM}Auto-continuing in {auto_timeout}s if no input...{C_RESET}")
            for remaining in range(auto_timeout, 0, -1):
                if _kbhit():
                    _consume_key()  # consume trigger key before falling to input()
                    sys.stdout.write(f"\r{' ' * 60}\r")
                    sys.stdout.flush()
                    break  # fall through to manual input
                if remaining % 30 == 0 or remaining <= 10:
                    sys.stdout.write(
                        f"\r  {C_DIM}Auto-continuing in {remaining}s... "
                        f"(press any key to choose){C_RESET}   ")
                    sys.stdout.flush()
                time.sleep(1)
            else:
                # Timeout expired — auto-continue
                sys.stdout.write(
                    f"\r  {C_GREEN}Auto-continuing to next phase..."
                    f"{' ' * 30}{C_RESET}\n")
                sys.stdout.flush()
                return "c"

        while True:
            try:
                choice = input(f"  {C_CYAN}> Choice [c/d/a]: {C_RESET}").strip().lower()
            except EOFError:
                return "a"  # safe default: abort in non-interactive
            if choice in ("c", "d", "a"):
                return choice
            print(f"  {C_RED}Invalid. Type c, d, or a.{C_RESET}")

    def error(self, msg: str):
        print(f"  {C_RED}{C_BOLD}[ERROR]{C_RESET} {C_RED}{msg}{C_RESET}")


# ============================================================================
# LOGGING SYSTEM
# ============================================================================

class GovernmentLogger:
    def __init__(self, gov_dir: str):
        self.log_dir = os.path.join(gov_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.master_path = os.path.join(self.log_dir, "master.log")
        self.executor_path = os.path.join(self.log_dir, "executor.log")
        self.inspector_path = os.path.join(self.log_dir, "inspector.log")
        for p in [self.master_path, self.executor_path, self.inspector_path]:
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"{'=' * 80}\n  GOVERNMENT LOG — {datetime.now()}\n{'=' * 80}\n\n")

    def _append(self, path: str, text: str):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def master(self, tag: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._append(self.master_path, f"[{ts}] [{tag}] {msg}\n")

    def agent(self, agent: str, round_num: int, prompt: str, stdout: str,
              stderr: str, rc: int, duration: float):
        path = self.executor_path if agent == "executor" else self.inspector_path
        self._append(path, (
            f"\n{'=' * 70}\n"
            f"ROUND {round_num} | rc={rc} | {duration:.1f}s | {datetime.now()}\n"
            f"{'=' * 70}\n"
            f"PROMPT ({len(prompt)} chars):\n{'─' * 40}\n{prompt}\n{'─' * 40}\n"
            f"STDOUT ({len(stdout)} chars):\n{'─' * 40}\n{stdout}\n{'─' * 40}\n"
        ))
        if stderr.strip():
            self._append(path, f"STDERR:\n{'─' * 40}\n{stderr}\n{'─' * 40}\n")


# ============================================================================
# STATE MANAGEMENT
# ============================================================================

class GovernmentState:
    """Persistent state tracker with atomic writes."""

    def __init__(self, gov_dir: str):
        self.gov_dir = gov_dir
        self.state_path = os.path.join(gov_dir, "state.json")
        os.makedirs(gov_dir, exist_ok=True)
        self.data = None  # ensure attribute exists before any branch

        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                _print_safe(f"  {C_YELLOW}[WARNING] state.json corrupted ({e}), "
                            f"starting fresh.{C_RESET}")
                # Try to load the temp file as backup
                temp = self.state_path + ".tmp"
                if os.path.exists(temp):
                    try:
                        with open(temp, "r", encoding="utf-8") as f:
                            self.data = json.load(f)
                        _print_safe(f"  {C_GREEN}[RECOVERED] Loaded from backup .tmp{C_RESET}")
                        self._save()
                        return
                    except (json.JSONDecodeError, OSError):
                        pass
                self.data = None  # fall through to default init below
        if self.data is None or not isinstance(self.data, dict):
            self.data = {
                "source_file": "",
                "user_instructions": "",
                "working_dir": "",
                "executor_session_id": None,
                "inspector_session_id": None,
                "current_phase": 0,
                "current_step": "init",       # init, master_plan, plan, plan_review, exec, exec_review, checkpoint
                "current_round": 0,
                "current_substep": "",        # "", "executor_done", "inspector_done"
                "phases_completed": [],
                "phase_summaries": {},         # {phase_num: "summary text"}
                "total_executor_calls": 0,
                "total_inspector_calls": 0,
                "created_at": datetime.now().isoformat(),
            }
            self._save()

    def _save(self):
        temp = self.state_path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, default=str)
            os.replace(temp, self.state_path)
        except OSError as e:
            _print_safe(f"  {C_DIM}[STATE WARNING] Save failed: {e}{C_RESET}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self._save()

    def update(self, **kwargs):
        self.data.update(kwargs)
        self._save()


# ============================================================================
# PROJECT STATUS FILE GENERATOR
# ============================================================================

def generate_project_status(state: GovernmentState, workdir: str):
    """Generate project_status.md from state — the single source of truth for agents."""
    lines = [
        "# Project Status (auto-generated by Government)",
        f"Source: {state.get('source_file')}",
        f"Working Directory: {workdir}",
        f"Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    completed = state.get("phases_completed", [])
    summaries = state.get("phase_summaries", {})

    if completed:
        lines.append("## Completed Phases")
        for p in completed:
            summary = summaries.get(str(p), "No summary available.")
            lines.append(f"### Phase {p}: COMPLETED")
            lines.append(f"{summary}")
            lines.append("")
    else:
        lines.append("## No phases completed yet.")
        lines.append("")

    cur_phase = state.get("current_phase", 0)
    cur_step = state.get("current_step", "init")
    cur_round = state.get("current_round", 0)
    if cur_phase > 0:
        lines.append(f"## Current: Phase {cur_phase}")
        lines.append(f"Step: {cur_step} | Round: {cur_round}")
        lines.append("")

    status_path = os.path.join(workdir, "project_status.md")
    try:
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError as e:
        _print_safe(f"  {C_DIM}[WARNING] Could not write project_status.md: {e}{C_RESET}")
    return status_path


# ============================================================================
# FILE VERIFICATION
# ============================================================================

def verify_file_created(expected_path: str, min_chars: int = 50) -> tuple[bool, str]:
    """Check if the expected file was created and is non-empty.
    Returns (success, message)."""
    # Direct check
    if os.path.exists(expected_path):
        try:
            with open(expected_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if len(content.strip()) < min_chars:
                return False, f"File exists but is too short ({len(content.strip())} chars). Write the actual content."
            return True, "OK"
        except OSError as e:
            return False, f"File exists but cannot be read: {e}"

    # Case-insensitive fuzzy search in the expected directory
    expected_dir = os.path.dirname(expected_path)
    expected_name = os.path.basename(expected_path).lower()

    if os.path.isdir(expected_dir):
        for fname in os.listdir(expected_dir):
            if fname.lower() == expected_name:
                actual = os.path.join(expected_dir, fname)
                return False, (f"File created with wrong name: '{fname}'. "
                               f"Expected: '{os.path.basename(expected_path)}'. "
                               f"Rename it to the exact path: {expected_path}")

        # Check for similar files
        stem = Path(expected_path).stem.lower()
        for fname in os.listdir(expected_dir):
            if stem in fname.lower() and fname.lower().endswith(".md"):
                return False, (f"Found similar file '{fname}' but expected exact name "
                               f"'{os.path.basename(expected_path)}'. "
                               f"Create or rename to: {expected_path}")

    return False, f"File not found at: {expected_path}. You MUST create it at that exact path."


def verify_has_verdict(filepath: str) -> tuple[bool, str]:
    """Check that a review file contains a VERDICT line."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False, "Cannot read review file."

    matches = re.findall(r'VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)', content, re.IGNORECASE)
    if matches:
        return True, matches[-1].upper()  # last verdict wins
    return False, "No VERDICT found. Add VERDICT: APPROVED or VERDICT: NEEDS_REVISION at the end."


# ============================================================================
# FILE ARCHIVAL
# ============================================================================

def archive_file(filepath: str, phase_dir: str):
    """Copy current file to .archive/ with version number before overwrite."""
    if not os.path.exists(filepath):
        return
    archive_dir = os.path.join(phase_dir, ".archive")
    os.makedirs(archive_dir, exist_ok=True)

    stem = Path(filepath).stem
    ext = Path(filepath).suffix
    version = 1
    while os.path.exists(os.path.join(archive_dir, f"{stem}_v{version}{ext}")):
        version += 1
    dest = os.path.join(archive_dir, f"{stem}_v{version}{ext}")
    try:
        shutil.copy2(filepath, dest)
    except OSError as e:
        _print_safe(f"  {C_DIM}[ARCHIVE WARNING] Failed to archive {filepath}: {e}{C_RESET}")


# ============================================================================
# CODEX CLI INTERFACE
# ============================================================================

def _resolve_codex_binary(cli_arg: str | None = None) -> str | None:
    if cli_arg and os.path.exists(cli_arg):
        return cli_arg
    if os.name == "nt":
        for check in [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "codex.cmd",
            Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd" if os.environ.get("APPDATA") else None,
        ]:
            if check and check.exists():
                return str(check)
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            ext_root = Path(userprofile) / ".vscode" / "extensions"
            if ext_root.exists():
                matches = sorted(ext_root.glob("openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"))
                if matches:
                    return str(matches[-1])
    for name in ["codex.cmd", "codex.exe", "codex"]:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def _pipe_reader(pipe, q, tag):
    try:
        for line in pipe:
            q.put((tag, line))
    except Exception:
        pass
    finally:
        q.put((tag + "_done", None))


def _kill_proc(proc):
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _parse_session_id(stderr_text: str) -> str | None:
    """Extract session id from codex stderr output."""
    match = re.search(r'session id:\s*([0-9a-f-]+)', stderr_text, re.IGNORECASE)
    return match.group(1) if match else None


def run_codex(prompt: str, codex_bin: str, session_id: str | None,
              agent_name: str, ui: TerminalUI, logger: GovernmentLogger,
              round_label: str, working_dir: str,
              pause_event: threading.Event | None = None,
              ) -> tuple[str, str, int, float, str | None]:
    """
    Run Codex CLI. If session_id is provided, resume that session.
    Otherwise start new session.

    Returns (stdout, stderr, returncode, duration, session_id).
    rc special values: -1 = launch/timeout error, -2 = KeyboardInterrupt, -3 = network error.
    session_id is captured from stderr on new sessions.

    Timeout: tracks activity on BOTH pipes. stderr activity (codex thinking)
    resets the silence timer.
    """
    if os.name == "nt" and codex_bin.lower().endswith((".cmd", ".bat")):
        base_cmd = ["cmd.exe", "/c", codex_bin]
    else:
        base_cmd = [codex_bin]

    if session_id:
        cmd = base_cmd + ["exec", "resume", session_id,
                          APPROVAL_FLAG, "--skip-git-repo-check", "-"]
    else:
        cmd = base_cmd + ["exec", APPROVAL_FLAG, "--skip-git-repo-check", "-"]

    logger.master(agent_name.upper(), f"CMD: {' '.join(cmd)}")
    logger.master(agent_name.upper(), f"CWD: {working_dir}")

    start_time = time.time()
    proc = None

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=working_dir, env=env,
        )
    except Exception as e:
        d = time.time() - start_time
        logger.master(agent_name.upper(), f"Launch failed: {e}")
        return "", str(e), -1, d, None

    # Write prompt
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        logger.master(agent_name.upper(), f"STDIN write failed: {e}")
        ui.error(f"Failed to send prompt: {e}")
        _kill_proc(proc)
        d = time.time() - start_time
        return "", f"stdin failed: {e}", -1, d, session_id

    # Reader threads
    q = queue.Queue()
    threading.Thread(target=_pipe_reader, args=(proc.stdout, q, "out"), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(proc.stderr, q, "err"), daemon=True).start()

    stdout_lines = []
    stderr_lines = []
    done_flags = set()
    last_activity = time.time()
    codex_started = False
    out_count = 0
    err_count = 0
    captured_sid = session_id  # keep existing or capture new
    header_separator_count = 0  # track "--------" lines to detect end of startup header
    interactive = sys.stdin.isatty()
    pause_hint = " [P=pause]" if interactive and pause_event else ""
    inet_down = threading.Event()  # set by background internet check thread

    # Enter cbreak mode on Unix for single-keypress detection during codex run
    old_term = _enter_cbreak() if (interactive and pause_event) else None

    try:
        while len(done_flags) < 2:
            try:
                tag, line = q.get(timeout=1.0)
            except queue.Empty:
                silent = int(time.time() - last_activity)

                if not codex_started and silent >= STARTUP_TIMEOUT:
                    _kill_proc(proc)
                    d = time.time() - start_time
                    msg = f"Codex did not start within {STARTUP_TIMEOUT}s"
                    logger.master(agent_name.upper(), msg)
                    return "", msg, -1, d, captured_sid

                if codex_started and silent >= SILENCE_TIMEOUT:
                    _kill_proc(proc)
                    d = time.time() - start_time
                    msg = f"Timed out: zero activity for {silent}s"
                    logger.master(agent_name.upper(), msg)
                    return "".join(stdout_lines), msg, -1, d, captured_sid

                # Background internet check on prolonged silence
                if codex_started and silent > 30 and silent % 30 == 0 and not inet_down.is_set():
                    def _bg_inet(evt):
                        if not _check_internet(timeout=3):
                            evt.set()
                    threading.Thread(target=_bg_inet, args=(inet_down,), daemon=True).start()

                if inet_down.is_set():
                    _kill_proc(proc)
                    d = time.time() - start_time
                    msg = "NETWORK_ERROR: Internet connection lost during codex execution"
                    logger.master(agent_name.upper(), msg)
                    return "".join(stdout_lines), msg, -3, d, captured_sid

                # Check for pause keypress
                if interactive and pause_event and not pause_event.is_set() and _kbhit():
                    ch = _read_key()
                    if ch == 'p':
                        pause_event.set()
                        ui.status("Pause queued — will pause after this agent finishes.", C_YELLOW)

                if silent > 0 and silent % 5 == 0:
                    elapsed = int(time.time() - start_time)
                    if not codex_started:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} Waiting for Codex... "
                            f"{C_DIM}({silent}s){C_RESET}    ")
                    else:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} {agent_name} working... "
                            f"{C_DIM}({elapsed}s, silent {silent}s){pause_hint}{C_RESET}    ")
                    sys.stdout.flush()
                continue

            if tag == "out_done":
                done_flags.add("out")
                continue
            if tag == "err_done":
                done_flags.add("err")
                continue

            # ANY output = alive
            last_activity = time.time()
            codex_started = True
            inet_down.clear()  # got output, internet is fine
            ui.clear_line()

            if tag == "out":
                stdout_lines.append(line)
                out_count += 1
                if out_count <= 5 or out_count % 20 == 0:
                    ui.stream_line(agent_name, line)
                elif out_count == 6:
                    _print_safe(f"  {C_DIM}  ... streaming (showing every 20th line) ...{C_RESET}")
            else:
                stderr_lines.append(line)
                err_count += 1
                # Track header boundaries (two "--------" separator lines)
                if header_separator_count < 2 and line.strip().startswith("--------"):
                    header_separator_count += 1
                # Capture session id ONLY from within the startup header
                if header_separator_count == 1 and "session id:" in line.lower():
                    sid = _parse_session_id(line)
                    if sid:
                        captured_sid = sid
                # Show some stderr so user knows codex is thinking
                stripped = line.strip()
                if stripped and err_count <= 5:
                    _print_safe(f"  {C_DIM}  [codex] {stripped[:80]}{C_RESET}")
                elif err_count == 6:
                    _print_safe(f"  {C_DIM}  [codex] ... (suppressing further stderr){C_RESET}")

    except KeyboardInterrupt:
        ui.error("Interrupted! Killing Codex...")
        _kill_proc(proc)
        d = time.time() - start_time
        logger.master(agent_name.upper(), f"Interrupted after {d:.1f}s")
        return "".join(stdout_lines), "Interrupted", -2, d, captured_sid
    finally:
        _exit_cbreak(old_term)

    proc.wait()
    d = time.time() - start_time

    if out_count > 5:
        _print_safe(f"  {C_DIM}  ... {out_count} total lines{C_RESET}")

    return "".join(stdout_lines), "".join(stderr_lines), proc.returncode, d, captured_sid


# ============================================================================
# PROMPT TEMPLATES
# ============================================================================

# ---------- EXECUTOR FIRST MESSAGE (establishes identity) ----------

EXECUTOR_SYSTEM = """You are the EXECUTOR agent in a system called "Government" — a multi-agent AI pipeline for precise, phased software implementation.

YOUR IDENTITY:
- You are a senior software engineer responsible for planning and executing a project phase by phase.
- You think before you act. You plan before you code. You verify before you report.
- You are methodical, thorough, and you NEVER skip steps or cut corners.
- You take inspector feedback seriously — every comment is a potential bug you missed.

YOUR CAPABILITIES:
- Read any file in the project directory.
- Write, modify, and create code files.
- Run commands to build, test, and verify.
- Search the internet for documentation if needed.

STRICT RULES YOU MUST ALWAYS FOLLOW:
1. ALWAYS read files you are told to read FIRST, before doing anything else.
2. When told to create a file, create it at the EXACT path specified. Not a different name, not a different location.
3. Your reports must be detailed enough that someone who cannot see the code can understand exactly what was done, changed, and verified.
4. When revising based on inspector feedback, address EVERY SINGLE point. Do not skip any. If you disagree, explain why with technical reasoning.
5. After making code changes, VERIFY your work. Run the code. Check the output. Don't assume it works.
6. State what you're about to do, then do it, then confirm what you did.
7. Be honest. If something failed or partially worked, say so. Never hide failures.
8. NEVER wrap your entire response in a code block. Use code blocks only for actual code snippets.

You will now receive instructions for the project. Follow them precisely.
"""

# ---------- EXECUTOR PERSISTENT REMINDERS (in every message after first) ----------

EXECUTOR_REMINDERS = """
=== IMPORTANT REMINDERS (read every time) ===
- First, re-read our project source file at: {source_file}
  Remember exactly what our project is, what we need to build, how it should work, and what the expected outcome is.
- Read project_status.md to remember what phases we have completed and where we are now.
- Check the current phase folder to see all existing files and understand current state.
- Before making any changes: understand what was the plan, what have we already implemented, what is left, and what is the current task.
- After you make changes or write code: VERIFY your work. Actually run the code. Check output. Test edge cases.
- Read files you changed after changing them to confirm changes are correct.
- If you create or update a report, make sure it accurately reflects what you ACTUALLY did, not what you planned to do.
- Save your output to the EXACT file path specified below. No different name.
=== END REMINDERS ===
"""

# ---------- INSPECTOR PERSISTENT REMINDERS (in every message after first) ----------

INSPECTOR_REMINDERS = """
=== IMPORTANT REMINDERS (read every time) ===
- First, re-read our project source file at: {source_file}
  Remember exactly what our project is, how everything needs to work, exactly what we have to do, how we should do it, what we expect to see, and how we can make sure everything is going correctly.
- Read project_status.md to remember what phases we already completed and what was done.
- Remember: you are a very, very strict inspector. Suspicious, detailed, and thorough. You trust NOTHING the executor claims without verifying it yourself.
- You MUST read the actual code files and verify every claim the executor makes. Do not just read the report — go check the actual source files, run the code if possible.
- For every issue you find: explain what has been done wrong, WHY it is wrong, how it can be fixed, and how to verify that the fix actually works. The executor needs to understand the issue deeply, not just patch it.
- Check: Is it based on the implementation plan? Is it the expected output? Is it correct? Does it handle edge cases? Is it secure? Would it work in production?
- Do NOT approve just because many rounds have passed. Only approve when the work is genuinely correct. If the same issues keep reappearing, flag that the executor is not addressing feedback properly.
- Save your review to the EXACT file path specified below.
- At the very end, on its own line, write exactly: VERDICT: APPROVED or VERDICT: NEEDS_REVISION
=== END REMINDERS ===
"""

# ---------- INSPECTOR FIRST MESSAGE (establishes identity) ----------

INSPECTOR_SYSTEM = """You are the INSPECTOR agent in a system called "Government" — a multi-agent AI pipeline for precise, phased software implementation.

YOUR IDENTITY:
- You are a ruthless, suspicious, extremely thorough senior code reviewer and quality assurance engineer.
- You trust NOTHING the executor claims. You VERIFY everything yourself by reading actual files and running actual code.
- You read every line. You check every file. You run every test.
- You are the last line of defense before code goes to production.

YOUR PHILOSOPHY:
- "Works on my machine" is NOT acceptable. Prove it.
- "I checked and it's fine" is NOT acceptable. Show the evidence.
- Every uncaught bug is YOUR failure. Be paranoid.
- The executor is skilled but makes mistakes. Your job is to catch them ALL.

YOUR CAPABILITIES:
- Read any file in the project directory.
- Run commands to verify claims, run tests, check output (read-only — never modify project files).
- Search the internet for best practices.
- Check if code actually compiles, runs, and produces correct output.

YOUR LIMITATIONS:
- You MUST NOT create, modify, write to, or delete any project files.
- You may ONLY write to the specific review file path you are given for each task.
- If you find issues, describe them in your review. The executor will make the fixes.

STRICT RULES:
1. NEVER approve work you haven't verified yourself by reading the actual code files.
2. Rate every issue: CRITICAL / HIGH / MEDIUM / LOW.
3. Be specific: file name, line number, what's wrong, how to fix it.
4. At the END, write on its own line: VERDICT: NEEDS_REVISION or VERDICT: APPROVED
5. Use NEEDS_REVISION if ANY critical or high severity issues exist.
6. Use APPROVED only if the work is genuinely solid (only medium/low issues remain).
7. When re-reviewing after fixes, CHECK that previous issues were actually fixed. Don't take the executor's word for it.
8. NEVER wrap your entire response in a code block.
9. NEVER create, modify, write, or delete any project files. You are a READ-ONLY reviewer. Your only file output is your review/verdict file at the path you are given. If you see issues, DESCRIBE them — the executor will fix them.

You will now receive instructions for the project. Follow them precisely.
"""


def build_context_anchor(state: GovernmentState, workdir: str) -> str:
    """Build the dynamic context anchor block from current state."""
    source = state.get("source_file", "")
    phase = state.get("current_phase", 0)
    step = state.get("current_step", "")
    completed = state.get("phases_completed", [])

    lines = [
        "=== CONTEXT (current position) ===",
        f"Project source: {source}",
        f"Status file: {os.path.join(workdir, 'project_status.md')}",
        f"Current phase: {phase}",
        f"Current step: {step}",
        f"Phases completed: {completed if completed else 'none'}",
    ]

    # List current phase files
    if phase > 0:
        phase_dir = os.path.join(workdir, f"phase_{phase}")
        if os.path.isdir(phase_dir):
            files = [f for f in os.listdir(phase_dir) if not f.startswith(".")]
            if files:
                lines.append(f"Files in phase_{phase}/: {', '.join(sorted(files))}")

    # List previous phase's key files (for reference)
    if phase > 1:
        prev_dir = os.path.join(workdir, f"phase_{phase - 1}")
        if os.path.isdir(prev_dir):
            prev_files = [f for f in os.listdir(prev_dir) if not f.startswith(".")]
            if prev_files:
                lines.append(f"Files in phase_{phase - 1}/: {', '.join(sorted(prev_files))}")

    lines.append("=== END CONTEXT ===")
    return "\n".join(lines)


def build_executor_prompt(state: GovernmentState, workdir: str, task: str) -> str:
    """Build a full executor prompt: context + reminders + task."""
    anchor = build_context_anchor(state, workdir)
    reminders = EXECUTOR_REMINDERS.format(source_file=state.get("source_file", ""))
    return f"{anchor}\n\n{reminders}\n\n{task}"


def build_inspector_prompt(state: GovernmentState, workdir: str, task: str) -> str:
    """Build a full inspector prompt: context + reminders + task."""
    anchor = build_context_anchor(state, workdir)
    reminders = INSPECTOR_REMINDERS.format(source_file=state.get("source_file", ""))
    return f"{anchor}\n\n{reminders}\n\n{task}"


# ============================================================================
# SPECIFIC TASK PROMPTS (filled in by the engine)
# ============================================================================

TASK_MASTER_PLAN = """TASK: CREATE MASTER PLAN

Read the project source file carefully. Understand the full scope of the project.

Then create a master implementation plan that breaks the project into logical phases.
Each phase should be a self-contained unit of work that can be planned, executed, and verified independently.

Save the master plan to: {filepath}

The master plan must include:
## Master Implementation Plan
### Project Overview
Brief summary of what this project is and its goals.
### Phase Breakdown
For each phase:
## Phase N: [Phase Title]
- Objective: what this phase achieves
- Key deliverables: what files/features will exist after this phase
- Dependencies: what must be done before this phase
- Estimated complexity: Low/Medium/High
### Total Phases: [number]
"""

TASK_REVIEW_MASTER_PLAN = """TASK: REVIEW THE MASTER PLAN

The executor created a master implementation plan for the project.

Read these files:
1. Project source: {source_file}
2. Master plan: {plan_path}

Review the master plan thoroughly:
- Does it cover the ENTIRE project scope?
- Are the phases logically ordered?
- Are dependencies correct?
- Is each phase the right size (not too big, not too small)?
- Are any important features or requirements missing?
- Would this plan actually work if followed step by step?

Save your review to: {review_path}

Format:
## Master Plan Review
### Overall Assessment
### Issues Found
- [SEVERITY] Issue description
### Suggestions
### Verdict
VERDICT: NEEDS_REVISION or VERDICT: APPROVED
"""

TASK_REVISE_MASTER_PLAN = """TASK: REVISE MASTER PLAN BASED ON INSPECTOR FEEDBACK

The inspector reviewed your master plan and found issues.

Read these files:
1. Your master plan: {plan_path}
2. Inspector's review: {review_path}

Address EVERY point the inspector raised:
- For each issue: understand it, fix it, explain what you changed.
- If you disagree with a point, explain why with reasoning.

Update the master plan at: {plan_path}

Add a section at the top:
### Changes After Review Round {round_num}
List each inspector comment and what you changed.
"""

TASK_PHASE_PLAN = """TASK: CREATE DETAILED PLAN FOR PHASE {phase_num}

Read these files:
1. Project source: {source_file}
2. Master plan: {master_plan_path}
3. Project status: {status_path}
{prev_phase_ref}

Create a detailed implementation plan for Phase {phase_num}.
Save it to: {filepath}

The plan MUST include:
## Phase {phase_num} — Implementation Plan
### Objective
What this phase achieves (1-2 sentences).
### Prerequisites
What must be true before starting.
### Steps
Numbered list of every step. Each step must have:
- What to do (specific action)
- Which files are affected
- Expected outcome / how to verify
- Potential risks
### Testing Strategy
How to verify the entire phase works after all steps.
### Success Criteria
Bullet list of concrete conditions that prove this phase is complete.

Be SPECIFIC. Each step should be small enough to verify independently.
"""

TASK_REVIEW_PLAN = """TASK: REVIEW PHASE {phase_num} PLAN

The executor created an implementation plan for Phase {phase_num}.

Read these files:
1. Project source: {source_file}
2. Master plan: {master_plan_path}
3. Phase {phase_num} plan: {plan_path}
{prev_phase_ref}

Review the plan:
- Does it cover everything Phase {phase_num} needs per the master plan?
- Is the technical approach sound?
- Is each step concrete and actionable?
- Are success criteria measurable?
- What could go wrong that the plan doesn't address?

Save your review to: {review_path}

Format:
## Phase {phase_num} Plan Review — Round {round_num}
### Overall Assessment
### Issues Found
- [SEVERITY] description — where — fix suggestion
### Positive Aspects
### Verdict
VERDICT: NEEDS_REVISION or VERDICT: APPROVED
"""

TASK_REVISE_PLAN = """TASK: REVISE PHASE {phase_num} PLAN

The inspector reviewed your plan and found issues.

Read these files:
1. Your plan: {plan_path}
2. Inspector's review: {review_path}

Address EVERY point. Update the plan at: {plan_path}

Add at the top:
### Changes After Review Round {round_num}
For each inspector comment: what was the issue, what you changed, why.
"""

TASK_EXECUTE = """TASK: EXECUTE PHASE {phase_num}

The plan for Phase {phase_num} has been APPROVED. Now execute it step by step.

Read these files:
1. Approved plan: {plan_path}
2. Project source: {source_file}
{prev_phase_ref}

Execute EACH step from the plan in order:
- Before each step: state what you are about to do
- Do it (write code, modify files, run commands)
- After each step: verify it worked
- Record what happened

After ALL steps, run the testing strategy from the plan.

Create the execution report at: {report_path}

The report MUST include:
## Phase {phase_num} — Execution Report
### Summary
### Step-by-Step Execution Log
For each step:
- Step N: [title]
- Action taken
- Files modified (list with brief descriptions)
- Verification: what you checked and result
- Status: DONE / DONE_WITH_DEVIATION / FAILED
### Testing Results
Actual output from running tests.
### Success Criteria Checklist
[x] or [ ] for each criterion from the plan.
### Known Issues
Any issues discovered.
"""

TASK_REVIEW_EXEC = """TASK: REVIEW PHASE {phase_num} EXECUTION

The executor has executed Phase {phase_num} and written an execution report.
This is the most critical review. You must VERIFY, not just read.

Read these files:
1. Project source: {source_file}
2. Approved plan: {plan_path}
3. Execution report: {report_path}
4. Then CHECK the actual code files mentioned in the report.

For EACH step in the report:
1. Read what the executor claims to have done
2. Go to the actual file and verify the code exists and is correct
3. Check that the code matches what the plan specified
4. Run the code if possible
5. Look for bugs, security issues, edge cases

Also verify that previous phases still work. Check that Phase {phase_num}'s changes haven't broken earlier functionality.

Save your review to: {review_path}

Format:
## Phase {phase_num} Execution Review — Round {round_num}
### Overall Assessment
### Verified Claims
Step N: VERIFIED / PARTIALLY_VERIFIED / FAILED_VERIFICATION — evidence
### Issues Found
- [SEVERITY] Issue — location — problem — fix
### Verdict
VERDICT: NEEDS_REVISION or VERDICT: APPROVED
"""

TASK_REVISE_EXEC = """TASK: FIX ISSUES AND UPDATE EXECUTION REPORT

The inspector reviewed your Phase {phase_num} execution and found issues.

Read these files:
1. Approved plan: {plan_path}
2. Your execution report: {report_path}
3. Inspector's review: {review_path}

For EACH issue:
1. Understand the issue
2. Go to the actual code and verify the inspector is correct
3. Fix the issue in the actual code
4. Verify your fix works (run it, test it)

Then update your execution report at: {report_path}

Add at the top:
### Fixes Applied — Review Round {round_num}
For each issue: what it was, did you confirm it, what you fixed, how you verified.

IMPORTANT: Actually fix the CODE, not just the report. Then re-run tests.
"""

TASK_PHASE_SUMMARY = """TASK: SUMMARIZE PHASE {phase_num}

Phase {phase_num} is now complete (approved by inspector).

Read your execution report at: {report_path}

Provide a brief summary (3-5 sentences) of what was accomplished in this phase.
Include: what was built, key files created/modified, and any important decisions made.

Reply with ONLY the summary text, nothing else. Do not create any files.
"""


# ============================================================================
# MAIN ENGINE
# ============================================================================

class Government:
    """Orchestrates the Executor-Inspector loop across project phases."""

    def __init__(self, source_file: str, working_dir: str,
                 user_instructions: str = "",
                 codex_bin: str | None = None,
                 max_review_rounds: int = MAX_REVIEW_ROUNDS,
                 auto_continue_timeout: int = AUTO_CONTINUE_TIMEOUT,
                 resume_mode: bool = False):

        self.ui = TerminalUI()
        self.codex_bin = _resolve_codex_binary(codex_bin)
        if not self.codex_bin:
            self.ui.error("Codex CLI not found!")
            sys.exit(1)

        self.source_file = os.path.abspath(source_file)
        if not os.path.isfile(self.source_file):
            self.ui.error(f"Source file not found: {self.source_file}")
            sys.exit(1)

        self.working_dir = os.path.abspath(working_dir)
        os.makedirs(self.working_dir, exist_ok=True)

        self.user_instructions = user_instructions
        self.max_rounds = max_review_rounds
        self.auto_continue_timeout = auto_continue_timeout
        self._resume_mode = resume_mode
        self._interrupt_requested = False

        # Government internal directory
        self.gov_dir = os.path.join(self.working_dir, ".government")
        os.makedirs(self.gov_dir, exist_ok=True)

        self.state = GovernmentState(self.gov_dir)
        self.logger = GovernmentLogger(self.gov_dir)

        # Always sync source_file into state (detect changes on reuse)
        old_source = self.state.get("source_file")
        self._source_changed = bool(old_source and old_source != self.source_file)
        if self._source_changed:
            self.ui.status(
                f"Source file changed: {old_source} -> {self.source_file}", C_YELLOW)
            self.logger.master("SYSTEM",
                               f"Source file changed from {old_source} to {self.source_file}")
        self.state.update(
            source_file=self.source_file,
            user_instructions=user_instructions,
            working_dir=self.working_dir,
        )

        self.logger.master("SYSTEM", f"Initialized. Source: {self.source_file}")
        self.logger.master("SYSTEM", f"Workdir: {self.working_dir}")
        self.logger.master("SYSTEM", f"Codex: {self.codex_bin}")

    # ------------------------------------------------------------------
    # Pause trigger
    # ------------------------------------------------------------------

    def _check_pause(self):
        """Block if .government/pause exists. Resumes when file is deleted."""
        pause_file = os.path.join(self.gov_dir, "pause")
        try:
            if not os.path.exists(pause_file):
                return
        except OSError:
            return

        self.ui.status("PAUSED — delete .government/pause to continue", C_YELLOW)
        self.ui.status(f"  Path: {pause_file}", C_DIM)
        self.logger.master("SYSTEM", "Paused by user")

        wait_count = 0
        try:
            while True:
                try:
                    if not os.path.exists(pause_file):
                        break
                except OSError:
                    break
                time.sleep(2)
                wait_count += 1
                if wait_count % 15 == 0:
                    self.ui.status(
                        f"Still paused ({wait_count * 2}s)... "
                        f"delete .government/pause to continue", C_DIM)
        except KeyboardInterrupt:
            self._interrupt_requested = True
            self.ui.status("Interrupted while paused.", C_RED)
            return

        self.ui.status("Resumed!", C_GREEN)
        self.logger.master("SYSTEM", "Resumed by user")

    # ------------------------------------------------------------------
    # Interactive pause / wait helpers
    # ------------------------------------------------------------------

    def _do_interactive_pause(self):
        """Block until user presses R to resume or Q to quit."""
        _flush_input()
        self.ui.status("PAUSED — press [R] to resume, [Q] to quit", C_YELLOW)
        self.logger.master("SYSTEM", "Paused by user (keyboard)")
        old_term = _enter_cbreak()
        try:
            wait = 0
            while True:
                if _kbhit():
                    ch = _read_key()
                    if ch == 'r':
                        break
                    elif ch == 'q':
                        self._interrupt_requested = True
                        self.ui.status("Quit requested.", C_RED)
                        return
                time.sleep(0.2)
                wait += 1
                if wait % 75 == 0:  # every 15s
                    self.ui.status(
                        f"Still paused ({wait // 5}s)... [R=resume, Q=quit]", C_DIM)
        finally:
            _exit_cbreak(old_term)
        self.ui.status("Resumed!", C_GREEN)
        self.logger.master("SYSTEM", "Resumed by user")

    def _wait_for_internet(self) -> bool:
        """Wait for internet to come back. Returns False if user quits."""
        _flush_input()
        self.ui.error("Internet connection lost. Waiting for reconnection...")
        self.logger.master("SYSTEM", "Internet lost — auto-paused")
        old_term = _enter_cbreak()
        try:
            wait = 0
            while not _check_internet():
                time.sleep(5)
                wait += 5
                if _kbhit():
                    ch = _read_key()
                    if ch == 'q':
                        self._interrupt_requested = True
                        self.ui.status("Quit requested.", C_RED)
                        return False
                if wait % 30 == 0:
                    self.ui.status(
                        f"Waiting for internet ({wait}s)... [Q=quit]", C_DIM)
        finally:
            _exit_cbreak(old_term)
        self.ui.status(f"Internet restored after {wait}s!", C_GREEN)
        self.logger.master("SYSTEM", f"Internet restored after {wait}s")
        return True

    def _wait_for_rate_limit(self, retry_info: str) -> str:
        """Wait for manual retry or hourly auto-retry. Returns an action string."""
        _flush_input()
        auto_minutes = max(1, RATE_LIMIT_AUTO_RETRY_SECONDS // 60)
        self.ui.status(
            f"RATE LIMITED — press [R] to retry now, [Q] to quit. "
            f"Auto-retry in {auto_minutes}m.{retry_info}", C_YELLOW)
        self.logger.master("SYSTEM", f"Rate limit pause.{retry_info}")
        old_term = _enter_cbreak()
        start = time.monotonic()
        deadline = start + RATE_LIMIT_AUTO_RETRY_SECONDS
        last_status_bucket = -1
        try:
            while True:
                if _kbhit():
                    ch = _read_key()
                    waited = int(time.monotonic() - start)
                    if ch == 'r':
                        self.logger.master(
                            "SYSTEM",
                            f"Rate limit manual retry after {waited}s.{retry_info}")
                        return "manual_retry"
                    elif ch == 'q':
                        self._interrupt_requested = True
                        self.ui.status("Quit requested.", C_RED)
                        self.logger.master(
                            "SYSTEM",
                            f"Rate limit quit after {waited}s.{retry_info}")
                        return "quit"
                now = time.monotonic()
                if now >= deadline:
                    waited = int(now - start)
                    self.logger.master(
                        "SYSTEM",
                        f"Rate limit auto-retry after {waited}s.{retry_info}")
                    return "auto_retry"
                time.sleep(1)
                waited = int(time.monotonic() - start)
                remaining = max(0, int(deadline - time.monotonic()))
                status_bucket = waited // 30
                if waited >= 30 and status_bucket != last_status_bucket:
                    last_status_bucket = status_bucket
                    self.ui.status(
                        f"Rate limited ({waited}s)... auto-retry in "
                        f"{remaining}s [R=retry now, Q=quit]", C_DIM)
        finally:
            _exit_cbreak(old_term)

    # ------------------------------------------------------------------
    # Codex call with network/rate-limit recovery
    # ------------------------------------------------------------------

    def _run_with_recovery(self, agent: str, prompt: str, round_label: str,
                           pause_event: threading.Event,
                           ) -> tuple[str, str, int, float, str | None]:
        """Run codex with recovery.
        Network retries are capped; rate-limit retries wait until manual or auto retry."""

        sid_key = f"{agent}_session_id"
        call_key = f"total_{agent}_calls"
        rnd = self.state.get("current_round", 0)
        total_duration = 0.0
        attempt = 0
        network_retries = 0

        while True:
            current_sid = self.state.get(sid_key)

            if attempt > 0:
                # Retry — prepend continuation context
                retry_prompt = (
                    "Your previous task was interrupted. "
                    "Analyze what you have done so far and continue where you left off. "
                    "Do NOT redo work that is already complete.\n\n"
                    f"Original task:\n{prompt}"
                )
            else:
                retry_prompt = prompt

            stdout, stderr, rc, duration, new_sid = run_codex(
                retry_prompt, self.codex_bin, current_sid,
                agent, self.ui, self.logger, round_label, self.working_dir,
                pause_event=pause_event,
            )
            total_duration += duration

            self.logger.agent(agent, rnd, retry_prompt, stdout, stderr, rc, duration)
            self.state.set(call_key, self.state.get(call_key, 0) + 1)

            # Save session ID
            if new_sid and new_sid != current_sid:
                self.state.set(sid_key, new_sid)
                self.logger.master(agent.upper(), f"Session ID: {new_sid}")

            # --- Priority 1: Interrupt ---
            if rc == -2:
                self._interrupt_requested = True
                return stdout, stderr, rc, total_duration, new_sid

            # --- Priority 2: Network error ---
            if rc == -3 or _is_network_error(stderr, rc):
                self.logger.master(agent.upper(),
                                   f"NETWORK ERROR (attempt {attempt+1}): {stderr[:300]}")
                if network_retries >= MAX_RECOVERY_RETRIES:
                    self.ui.error(f"Network error persists after {MAX_RECOVERY_RETRIES} retries. Halting.")
                    self._interrupt_requested = True
                    return stdout, stderr, rc, total_duration, new_sid
                if not self._wait_for_internet():
                    return stdout, stderr, rc, total_duration, new_sid  # user quit
                network_retries += 1
                attempt += 1
                self.ui.status(
                    f"Retrying {agent} after network recovery "
                    f"(attempt {attempt+1})...", C_CYAN)
                continue

            # --- Priority 3: Rate limit ---
            rate_limited, retry_info = _is_rate_limited(stderr, rc)
            if rate_limited:
                self.ui.error(f"API rate/usage limit hit for {agent}.{retry_info}")
                self.logger.master(agent.upper(),
                                   f"RATE LIMIT (attempt {attempt+1}): {stderr[:300]}")
                has_output = bool(stdout.strip())
                if has_output:
                    # Codex finished its task — output is valid, don't retry
                    return stdout, stderr, 0, total_duration, new_sid

                wait_started = time.monotonic()
                wait_action = self._wait_for_rate_limit(retry_info)
                total_duration += time.monotonic() - wait_started

                if wait_action == "quit":
                    return stdout, stderr, rc, total_duration, new_sid  # user quit

                attempt += 1
                if wait_action == "auto_retry":
                    self.ui.status(
                        f"Auto-retrying {agent} after rate-limit wait "
                        f"(attempt {attempt+1})...", C_CYAN)
                else:
                    self.ui.status(
                        f"Retrying {agent} on user request "
                        f"(attempt {attempt+1})...", C_CYAN)
                continue

            # --- No error — success ---
            return stdout, stderr, rc, total_duration, new_sid

    # ------------------------------------------------------------------
    # Codex call wrapper with session management
    # ------------------------------------------------------------------

    def _call_agent(self, agent: str, prompt: str, action_desc: str) -> str:
        """Call an agent (executor/inspector). Manages session resume + fallback.
        Returns stdout text."""

        sid_key = f"{agent}_session_id"
        session_id = self.state.get(sid_key)

        self.ui.agent_header(agent.upper(), action_desc)

        # Determine round label for logging
        phase = self.state.get("current_phase", 0)
        rnd = self.state.get("current_round", 0)
        round_label = f"P{phase}R{rnd}"

        pause_event = threading.Event()

        stdout, stderr, rc, duration, new_sid = self._run_with_recovery(
            agent, prompt, round_label, pause_event)

        if self._interrupt_requested:
            return ""

        # Tightened session error detection — specific phrases only
        stderr_lower = stderr.lower() if stderr else ""
        is_session_error = (session_id and rc != 0 and not stdout.strip()
                            and ("session not found" in stderr_lower
                                 or "invalid session" in stderr_lower
                                 or "unknown session" in stderr_lower
                                 or "no such session" in stderr_lower))
        if is_session_error:
            self.ui.status(f"Resume failed for {agent}. Starting new session...", C_YELLOW)
            self.logger.master(agent.upper(),
                               f"Resume failed (rc={rc}, stderr={stderr[:200]}), "
                               f"falling back to new session")

            # Re-send system prompt + current prompt in a new session
            system = EXECUTOR_SYSTEM if agent == "executor" else INSPECTOR_SYSTEM
            instructions_block = ""
            if agent == "executor" and self.user_instructions:
                instructions_block = f"\nUSER INSTRUCTIONS:\n{self.user_instructions}\n"
            init_msg = (
                f"{system}\n\n"
                f"The project source file is at: {self.source_file}\n"
                f"Read project_status.md in the working directory for context.\n"
                f"{instructions_block}\n"
                f"--- Now continuing with your task ---\n\n"
                f"{prompt}"
            )

            # Clear session so _run_with_recovery uses None (new session)
            self.state.set(sid_key, None)

            stdout, stderr, rc, fb_dur, new_sid = self._run_with_recovery(
                agent, init_msg, round_label, pause_event)
            duration += fb_dur

            if self._interrupt_requested:
                return ""

            if not stdout.strip() and rc != 0:
                msg = (f"Fallback session for {agent} also returned no output (rc={rc}). "
                       f"Possible API outage. Halting.")
                self.ui.error(msg)
                self.logger.master(agent.upper(),
                                   f"FALLBACK FAILED: rc={rc}, stderr={stderr[:300]}")
                self._interrupt_requested = True
                return ""

        # Pause check — after all processing, before returning result
        if pause_event.is_set() and not self._interrupt_requested:
            self._do_interactive_pause()
            if self._interrupt_requested:
                return ""

        output = stdout.strip()
        self.ui.status(
            f"{agent.capitalize()} finished ({duration:.0f}s, {len(output)} chars)",
            C_BLUE if agent == "executor" else C_MAGENTA
        )
        return output

    # ------------------------------------------------------------------
    # File verification with retry
    # ------------------------------------------------------------------

    def _verify_and_retry(self, agent: str, expected_path: str,
                          retry_prompt_template: str, min_chars: int = 50) -> bool:
        """Verify a file exists. If not, ask agent to create it. Returns True if OK."""
        for attempt in range(FILE_VERIFY_RETRIES):
            ok, msg = verify_file_created(expected_path, min_chars)
            if ok:
                return True
            self.ui.error(f"File verification failed: {msg}")
            self.logger.master("VERIFY", f"FAIL ({attempt+1}/{FILE_VERIFY_RETRIES}): {msg}")

            if attempt < FILE_VERIFY_RETRIES - 1:
                fix_prompt = retry_prompt_template.format(
                    error_message=msg, filepath=expected_path)
                if agent == "executor":
                    prompt = build_executor_prompt(self.state, self.working_dir, fix_prompt)
                else:
                    prompt = build_inspector_prompt(self.state, self.working_dir, fix_prompt)
                self._call_agent(agent, prompt, f"Fix: create missing file")
                if self._interrupt_requested:
                    return False

        self.ui.error(f"File still missing after {FILE_VERIFY_RETRIES} retries: {expected_path}")
        return False

    FILE_RETRY_PROMPT = """URGENT: The file you were supposed to create was not found or is invalid.

Error: {error_message}

You MUST create the file at exactly this path: {filepath}
Do it now. This is critical — the process cannot continue without this file.
"""

    # ------------------------------------------------------------------
    # Initialize agent sessions
    # ------------------------------------------------------------------

    def _init_agents(self):
        """Initialize both agent sessions with system prompts."""

        # Executor init
        if not self.state.get("executor_session_id"):
            self.ui.status("Initializing Executor session...", C_BLUE)
            init_prompt = (
                f"{EXECUTOR_SYSTEM}\n\n"
                f"PROJECT SOURCE FILE: {self.source_file}\n"
                f"WORKING DIRECTORY: {self.working_dir}\n"
                f"\nThis is your initialization step. Your ONLY task right now is:\n"
                f"1. Read the project source file at the path above.\n"
                f"2. Confirm you understand what the project is about.\n"
                f"3. Do NOT start building, coding, or creating any files yet.\n"
                f"4. Do NOT make any changes to the project directory.\n"
                f"\nSimply read the source file and briefly confirm what you understand "
                f"the project to be. Implementation instructions will follow in subsequent messages.\n"
            )

            output = self._call_agent("executor", init_prompt, "Initialization")
            if self._interrupt_requested:
                return False
            self.logger.master("EXECUTOR", f"Init response: {output[:200]}...")

        # Inspector init
        if not self.state.get("inspector_session_id"):
            self.ui.status("Initializing Inspector session...", C_MAGENTA)
            init_prompt = (
                f"{INSPECTOR_SYSTEM}\n\n"
                f"PROJECT SOURCE FILE: {self.source_file}\n"
                f"WORKING DIRECTORY: {self.working_dir}\n"
                f"\nThis is your initialization step. Your ONLY task right now is:\n"
                f"1. Read the project source file at the path above.\n"
                f"2. Confirm you understand what the project is about and your role as inspector.\n"
                f"3. Do NOT create, modify, or delete ANY files.\n"
                f"4. Do NOT write any code or make any changes to the project.\n"
                f"\nYou are a READ-ONLY reviewer. You will never modify project files — only review them "
                f"and write your findings to the review file you are given in each task.\n"
                f"\nSimply read the source file and briefly confirm your understanding. "
                f"Review tasks will follow in subsequent messages.\n"
            )

            output = self._call_agent("inspector", init_prompt, "Initialization")
            if self._interrupt_requested:
                return False
            self.logger.master("INSPECTOR", f"Init response: {output[:200]}...")

        return True

    # ------------------------------------------------------------------
    # Master plan phase
    # ------------------------------------------------------------------

    def _do_master_plan(self) -> bool:
        """Create and review master plan. Returns True if approved."""
        master_plan_path = os.path.join(self.working_dir, "master_plan.md")
        master_review_path = os.path.join(self.working_dir, "master_plan_review.md")

        # Resume detection
        saved_step = self.state.get("current_step", "")
        saved_round = self.state.get("current_round", 0)
        saved_substep = self.state.get("current_substep", "")
        resuming = (saved_step == "master_plan" and saved_round > 0)

        if resuming:
            round_num = saved_round - 1  # will be incremented to saved_round
            self.ui.status(
                f"Resuming master plan at round {saved_round}"
                f" (substep: {saved_substep or 'start'})", C_GREEN)
            self.logger.master("SYSTEM",
                               f"Resuming master plan at round {saved_round}, "
                               f"substep={saved_substep}")
        else:
            round_num = 0
            self.state.update(current_step="master_plan", current_round=0,
                              current_substep="")
            generate_project_status(self.state, self.working_dir)

        rounds_limit = self.max_rounds
        first_iteration = True

        while True:
            round_num += 1
            self.state.set("current_round", round_num)

            self.ui.phase_header(0, "Master Plan", round_num, rounds_limit)

            # Determine if we should skip executor (already done on resume)
            skip_executor = (first_iteration and resuming
                             and round_num == saved_round
                             and saved_substep == "executor_done")
            first_iteration = False

            if skip_executor:
                self.ui.status(
                    f"Executor already completed round {round_num}, skipping to inspector.",
                    C_GREEN)
            else:
                # ── Executor: create or revise master plan ──
                self.state.set("current_substep", "")
                if round_num == 1 and not resuming:
                    task = TASK_MASTER_PLAN.format(filepath=master_plan_path)
                    if self.user_instructions:
                        task += f"\nADDITIONAL INSTRUCTIONS FROM USER:\n{self.user_instructions}\n"
                    # Extend mode: tell executor about completed phases
                    completed_phases = self.state.get("phases_completed", [])
                    if completed_phases:
                        next_phase = max(completed_phases) + 1
                        task += (
                            f"\n\nIMPORTANT — EXTENSION MODE:\n"
                            f"Phases {sorted(completed_phases)} are ALREADY COMPLETED. "
                            f"Do NOT re-plan or re-number them.\n"
                            f"Number all new phases starting from Phase {next_phase}.\n"
                            f"Read project_status.md to understand what was already built.\n"
                        )
                else:
                    archive_file(master_plan_path, self.working_dir)
                    task = TASK_REVISE_MASTER_PLAN.format(
                        plan_path=master_plan_path,
                        review_path=master_review_path,
                        round_num=round_num - 1,
                    )

                prompt = build_executor_prompt(self.state, self.working_dir, task)
                self._call_agent("executor", prompt, f"Master Plan (round {round_num})")
                if self._interrupt_requested:
                    return False

                if not self._verify_and_retry("executor", master_plan_path, self.FILE_RETRY_PROMPT):
                    return False

                self.state.set("current_substep", "executor_done")

            self._check_pause()
            if self._interrupt_requested:
                return False

            # ── Inspector: review master plan ──
            task = TASK_REVIEW_MASTER_PLAN.format(
                source_file=self.source_file,
                plan_path=master_plan_path,
                review_path=master_review_path,
            )
            prompt = build_inspector_prompt(self.state, self.working_dir, task)
            archive_file(master_review_path, self.working_dir)
            self._call_agent("inspector", prompt, f"Review Master Plan (round {round_num})")
            if self._interrupt_requested:
                return False

            if not self._verify_and_retry("inspector", master_review_path,
                                          self.FILE_RETRY_PROMPT, min_chars=30):
                return False

            self.state.set("current_substep", "")

            # Check verdict
            has_v, verdict = verify_has_verdict(master_review_path)
            if not has_v:
                # Ask inspector to add verdict
                fix = (f"You forgot to include a verdict in your review at {master_review_path}. "
                       f"Read your review and add VERDICT: APPROVED or VERDICT: NEEDS_REVISION "
                       f"at the end of the file.")
                p = build_inspector_prompt(self.state, self.working_dir, fix)
                self._call_agent("inspector", p, "Add missing verdict")
                if self._interrupt_requested:
                    return False
                has_v, verdict = verify_has_verdict(master_review_path)
                if not has_v:
                    verdict = "NEEDS_REVISION"

            self.ui.verdict_display(verdict, round_num, rounds_limit)
            self.logger.master("SYSTEM", f"Master plan verdict round {round_num}: {verdict}")

            if verdict == "APPROVED":
                return True

            # Soft stop check
            if round_num >= rounds_limit:
                choice = self.ui.soft_stop_prompt(
                    f"Master plan review: {round_num} rounds without approval",
                    self.max_rounds)
                if choice == "c":
                    rounds_limit += self.max_rounds
                    self.ui.status(f"Extended to {rounds_limit} rounds.", C_GREEN)
                elif choice == "s":
                    self.ui.status("Skipping to execution with current plan (NOT inspector-approved).", C_YELLOW)
                    self.logger.master("SYSTEM", f"Master plan SKIPPED by user at round {round_num} (not approved)")
                    return "skipped"
                else:
                    return False

            self._check_pause()
            if self._interrupt_requested:
                return False

            time.sleep(COOLDOWN_BETWEEN)

    # ------------------------------------------------------------------
    # Phase plan loop
    # ------------------------------------------------------------------

    def _do_phase_plan(self, phase_num: int) -> bool:
        """Plan loop for a specific phase. Returns True if approved."""
        phase_dir = os.path.join(self.working_dir, f"phase_{phase_num}")
        os.makedirs(phase_dir, exist_ok=True)

        plan_path = os.path.join(phase_dir, "plan.md")
        review_path = os.path.join(phase_dir, "plan_review.md")
        master_plan = os.path.join(self.working_dir, "master_plan.md")
        status_path = os.path.join(self.working_dir, "project_status.md")

        # Previous phase reference
        prev_ref = ""
        if phase_num > 0:
            prev_dir = os.path.join(self.working_dir, f"phase_{phase_num - 1}")
            prev_report = os.path.join(prev_dir, "exec_report.md")
            if os.path.exists(prev_report):
                prev_ref = (f"- Previous phase execution report: {prev_report}\n"
                            f"  Read this to understand what was done in phase {phase_num - 1}.")

        # Resume detection
        saved_step = self.state.get("current_step", "")
        saved_round = self.state.get("current_round", 0)
        saved_substep = self.state.get("current_substep", "")
        resuming = (saved_step == "plan" and saved_round > 0
                    and self.state.get("current_phase") == phase_num)

        if resuming:
            round_num = saved_round - 1
            self.ui.status(
                f"Resuming Phase {phase_num} planning at round {saved_round}"
                f" (substep: {saved_substep or 'start'})", C_GREEN)
            self.logger.master("SYSTEM",
                               f"Resuming Phase {phase_num} plan at round {saved_round}, "
                               f"substep={saved_substep}")
        else:
            round_num = 0
            self.state.update(current_phase=phase_num, current_step="plan",
                              current_round=0, current_substep="")
            generate_project_status(self.state, self.working_dir)

        rounds_limit = self.max_rounds
        first_iteration = True

        while True:
            round_num += 1
            self.state.set("current_round", round_num)

            self.ui.phase_header(phase_num, "Planning", round_num, rounds_limit)

            skip_executor = (first_iteration and resuming
                             and round_num == saved_round
                             and saved_substep == "executor_done")
            first_iteration = False

            if skip_executor:
                self.ui.status(
                    f"Executor already completed round {round_num}, skipping to inspector.",
                    C_GREEN)
            else:
                # ── Executor ──
                self.state.set("current_substep", "")
                if round_num == 1 and not resuming:
                    task = TASK_PHASE_PLAN.format(
                        phase_num=phase_num, source_file=self.source_file,
                        master_plan_path=master_plan, status_path=status_path,
                        filepath=plan_path, prev_phase_ref=prev_ref,
                    )
                else:
                    archive_file(plan_path, phase_dir)
                    task = TASK_REVISE_PLAN.format(
                        phase_num=phase_num, plan_path=plan_path,
                        review_path=review_path, round_num=round_num - 1,
                    )

                prompt = build_executor_prompt(self.state, self.working_dir, task)
                self._call_agent("executor", prompt,
                                 f"Phase {phase_num} Plan (round {round_num})")
                if self._interrupt_requested:
                    return False

                if not self._verify_and_retry("executor", plan_path, self.FILE_RETRY_PROMPT):
                    return False

                self.state.set("current_substep", "executor_done")

            self._check_pause()
            if self._interrupt_requested:
                return False

            # ── Inspector ──
            task = TASK_REVIEW_PLAN.format(
                phase_num=phase_num, source_file=self.source_file,
                master_plan_path=master_plan, plan_path=plan_path,
                review_path=review_path, round_num=round_num,
                prev_phase_ref=prev_ref,
            )
            prompt = build_inspector_prompt(self.state, self.working_dir, task)
            archive_file(review_path, phase_dir)
            self._call_agent("inspector", prompt,
                             f"Review Phase {phase_num} Plan (round {round_num})")
            if self._interrupt_requested:
                return False

            if not self._verify_and_retry("inspector", review_path,
                                          self.FILE_RETRY_PROMPT, min_chars=30):
                return False

            self.state.set("current_substep", "")

            has_v, verdict = verify_has_verdict(review_path)
            if not has_v:
                fix = (f"Add VERDICT: APPROVED or VERDICT: NEEDS_REVISION "
                       f"to the end of {review_path}")
                p = build_inspector_prompt(self.state, self.working_dir, fix)
                self._call_agent("inspector", p, "Add verdict")
                if self._interrupt_requested:
                    return False
                has_v, verdict = verify_has_verdict(review_path)
                if not has_v:
                    verdict = "NEEDS_REVISION"

            self.ui.verdict_display(verdict, round_num, rounds_limit)
            self.logger.master("PLAN", f"Phase {phase_num} plan round {round_num}: {verdict}")

            if verdict == "APPROVED":
                try:
                    shutil.copy2(plan_path, os.path.join(phase_dir, "plan_approved.md"))
                except OSError as e:
                    self.logger.master("PLAN", f"Could not copy plan_approved.md: {e}")
                return True

            if round_num >= rounds_limit:
                choice = self.ui.soft_stop_prompt(
                    f"Phase {phase_num} plan: {round_num} rounds",
                    self.max_rounds)
                if choice == "c":
                    rounds_limit += self.max_rounds
                elif choice == "s":
                    self.ui.status(f"Skipping Phase {phase_num} plan review (NOT inspector-approved).", C_YELLOW)
                    self.logger.master("PLAN", f"Phase {phase_num} plan SKIPPED by user at round {round_num}")
                    return "skipped"
                else:
                    return False

            self._check_pause()
            if self._interrupt_requested:
                return False

            resuming = False
            time.sleep(COOLDOWN_BETWEEN)

    # ------------------------------------------------------------------
    # Phase execution loop
    # ------------------------------------------------------------------

    def _do_phase_execution(self, phase_num: int) -> bool:
        """Execution loop for a specific phase. Returns True if approved."""
        phase_dir = os.path.join(self.working_dir, f"phase_{phase_num}")
        os.makedirs(phase_dir, exist_ok=True)

        plan_path = os.path.join(phase_dir, "plan_approved.md")
        if not os.path.exists(plan_path):
            plan_path = os.path.join(phase_dir, "plan.md")
        report_path = os.path.join(phase_dir, "exec_report.md")
        review_path = os.path.join(phase_dir, "exec_review.md")

        prev_ref = ""
        if phase_num > 0:
            prev_report = os.path.join(self.working_dir, f"phase_{phase_num-1}", "exec_report.md")
            if os.path.exists(prev_report):
                prev_ref = f"- Previous phase report: {prev_report}"

        # Resume detection: if state already says "exec" for this phase, restore progress
        saved_step = self.state.get("current_step", "")
        saved_round = self.state.get("current_round", 0)
        saved_substep = self.state.get("current_substep", "")
        resuming = (saved_step == "exec" and saved_round > 0
                    and self.state.get("current_phase") == phase_num)

        if resuming:
            round_num = saved_round - 1  # will be incremented to saved_round
            self.ui.status(
                f"Resuming Phase {phase_num} execution at round {saved_round}"
                f" (substep: {saved_substep or 'start'})", C_GREEN)
            self.logger.master("SYSTEM",
                               f"Resuming Phase {phase_num} exec at round {saved_round}, "
                               f"substep={saved_substep}")
        else:
            round_num = 0
            self.state.update(current_step="exec", current_round=0, current_substep="")
            generate_project_status(self.state, self.working_dir)

        rounds_limit = self.max_rounds
        first_iteration = True

        while True:
            round_num += 1
            self.state.set("current_round", round_num)

            self.ui.phase_header(phase_num, "Execution", round_num, rounds_limit)

            # Determine if we should skip executor (already done on resume)
            skip_executor = (first_iteration and resuming
                             and round_num == saved_round
                             and saved_substep == "executor_done")
            first_iteration = False

            if skip_executor:
                self.ui.status(
                    f"Executor already completed round {round_num}, skipping to inspector.",
                    C_GREEN)
            else:
                # ── Executor ──
                self.state.set("current_substep", "")
                if round_num == 1 and not resuming:
                    task = TASK_EXECUTE.format(
                        phase_num=phase_num, plan_path=plan_path,
                        source_file=self.source_file, report_path=report_path,
                        prev_phase_ref=prev_ref,
                    )
                else:
                    archive_file(report_path, phase_dir)
                    task = TASK_REVISE_EXEC.format(
                        phase_num=phase_num, plan_path=plan_path,
                        report_path=report_path, review_path=review_path,
                        round_num=round_num - 1,
                    )

                prompt = build_executor_prompt(self.state, self.working_dir, task)
                self._call_agent("executor", prompt,
                                 f"Phase {phase_num} Execute (round {round_num})")
                if self._interrupt_requested:
                    return False

                if not self._verify_and_retry("executor", report_path, self.FILE_RETRY_PROMPT):
                    return False

                self.state.set("current_substep", "executor_done")

            self._check_pause()
            if self._interrupt_requested:
                return False

            # ── Inspector ──
            task = TASK_REVIEW_EXEC.format(
                phase_num=phase_num, source_file=self.source_file,
                plan_path=plan_path, report_path=report_path,
                review_path=review_path, round_num=round_num,
            )
            prompt = build_inspector_prompt(self.state, self.working_dir, task)
            archive_file(review_path, phase_dir)
            self._call_agent("inspector", prompt,
                             f"Review Phase {phase_num} Execution (round {round_num})")
            if self._interrupt_requested:
                return False

            if not self._verify_and_retry("inspector", review_path,
                                          self.FILE_RETRY_PROMPT, min_chars=30):
                return False

            self.state.set("current_substep", "")

            has_v, verdict = verify_has_verdict(review_path)
            if not has_v:
                fix = (f"Add VERDICT: APPROVED or VERDICT: NEEDS_REVISION "
                       f"to the end of {review_path}")
                p = build_inspector_prompt(self.state, self.working_dir, fix)
                self._call_agent("inspector", p, "Add verdict")
                if self._interrupt_requested:
                    return False
                has_v, verdict = verify_has_verdict(review_path)
                if not has_v:
                    verdict = "NEEDS_REVISION"

            self.ui.verdict_display(verdict, round_num, rounds_limit)
            self.logger.master("EXEC", f"Phase {phase_num} exec round {round_num}: {verdict}")

            if verdict == "APPROVED":
                try:
                    shutil.copy2(report_path, os.path.join(phase_dir, "exec_report_approved.md"))
                except OSError as e:
                    self.logger.master("EXEC", f"Could not copy exec_report_approved.md: {e}")
                return True

            if round_num >= rounds_limit:
                choice = self.ui.soft_stop_prompt(
                    f"Phase {phase_num} execution: {round_num} rounds",
                    self.max_rounds)
                if choice == "c":
                    rounds_limit += self.max_rounds
                elif choice == "s":
                    self.ui.status(f"Skipping Phase {phase_num} execution review (NOT inspector-approved).", C_YELLOW)
                    self.logger.master("EXEC", f"Phase {phase_num} execution SKIPPED by user at round {round_num}")
                    return "skipped"
                else:
                    return False

            self._check_pause()
            if self._interrupt_requested:
                return False

            resuming = False  # only first round can be a resume
            time.sleep(COOLDOWN_BETWEEN)

    # ------------------------------------------------------------------
    # Phase summary
    # ------------------------------------------------------------------

    def _get_phase_summary(self, phase_num: int):
        """Ask executor for a brief phase summary."""
        phase_dir = os.path.join(self.working_dir, f"phase_{phase_num}")
        report = os.path.join(phase_dir, "exec_report.md")

        task = TASK_PHASE_SUMMARY.format(phase_num=phase_num, report_path=report)
        prompt = build_executor_prompt(self.state, self.working_dir, task)
        summary = self._call_agent("executor", prompt, f"Phase {phase_num} Summary")

        if summary:
            summaries = self.state.get("phase_summaries", {})
            summaries[str(phase_num)] = summary[:500]
            self.state.set("phase_summaries", summaries)

        return summary

    # ------------------------------------------------------------------
    # Parse phase count from master plan
    # ------------------------------------------------------------------

    def _count_phases(self) -> tuple[int, int] | None:
        """Parse phase range from master_plan.md. Returns (start_phase, end_phase) or None."""
        master_path = os.path.join(self.working_dir, "master_plan.md")
        if not os.path.exists(master_path):
            return None
        try:
            with open(master_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return None

        # Find all "## Phase N" headings (anchored to line start)
        phases = re.findall(r'^##\s+Phase\s+(\d+)', content, re.IGNORECASE | re.MULTILINE)
        if phases:
            nums = [int(p) for p in phases]
            return (min(nums), max(nums))

        # Fallback: "Total Phases: N" without headings — assume 1-indexed
        match = re.search(r'Total\s+Phases\s*:\s*(\d+)', content, re.IGNORECASE)
        if match:
            return (1, int(match.group(1)))

        return None

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def run(self):
        """Main execution loop. Supports resuming from saved state."""
        self.ui.clear()
        self.ui.banner()
        self._start_time = time.time()

        # Clear stale pause file from previous run
        pause_file = os.path.join(self.gov_dir, "pause")
        if os.path.exists(pause_file):
            try:
                os.remove(pause_file)
                self.ui.status("Cleared stale pause file from previous run.", C_YELLOW)
            except OSError:
                self.ui.status(
                    "Warning: stale .government/pause exists but could not be removed. "
                    "System may pause immediately.", C_YELLOW)

        self.ui.box("Configuration", (
            f"Source: {self.source_file}\n"
            f"Workdir: {self.working_dir}\n"
            f"Codex: {self.codex_bin}\n"
            f"Max review rounds: {self.max_rounds} (soft stop, extendable)\n"
            f"Logs: {os.path.join(self.gov_dir, 'logs')}\n"
            f"Pause: press [P] while agent is running, or create .government/pause"
        ))

        if self.user_instructions:
            self.ui.box("User Instructions", self.user_instructions)

        # Check for resumable state
        saved_step = self.state.get("current_step", "init")
        saved_phase = self.state.get("current_phase", 0)
        completed_phases = self.state.get("phases_completed", [])

        if self._resume_mode:
            # main() already confirmed resume — skip prompt, force can_resume
            can_resume = True
        else:
            can_resume = (saved_step not in ("init", "completed")
                          and (self.state.get("executor_session_id") or completed_phases))

            if can_resume:
                self.ui.box("Resumable State Found", (
                    f"Last step: {saved_step}\n"
                    f"Last phase: {saved_phase}\n"
                    f"Completed phases: {completed_phases}\n"
                    f"Executor session: {self.state.get('executor_session_id', 'N/A')}\n"
                    f"Inspector session: {self.state.get('inspector_session_id', 'N/A')}"
                ), C_YELLOW)
                print(f"  {C_BOLD}r{C_RESET} = resume from where we left off")
                print(f"  {C_BOLD}n{C_RESET} = start fresh (new sessions)")
                print()
                while True:
                    try:
                        choice = input(f"  {C_CYAN}> Resume or new? [r/n]: {C_RESET}").strip().lower()
                    except EOFError:
                        choice = "n"
                        break
                    if choice in ("r", "n"):
                        break
                if choice == "n":
                    self.state.update(
                        executor_session_id=None, inspector_session_id=None,
                        current_phase=0, current_step="init", current_round=0,
                        phases_completed=[], phase_summaries={},
                    )
                    can_resume = False
                    completed_phases = []

        # 1. Initialize agents (skipped if sessions already exist from resume)
        self.ui.status("Initializing agent sessions...", C_CYAN)
        if not self._init_agents():
            self.ui.error("Agent initialization failed.")
            return
        if self._interrupt_requested:
            return

        self.ui.status(f"Executor session: {self.state.get('executor_session_id')}", C_GREEN)
        self.ui.status(f"Inspector session: {self.state.get('inspector_session_id')}", C_GREEN)

        # 2. Master plan (skip if resuming past this point, unless source changed)
        master_plan_path = os.path.join(self.working_dir, "master_plan.md")
        master_plan_exists = os.path.exists(master_plan_path)
        past_master_plan = saved_step not in ("init", "master_plan")
        if (can_resume and master_plan_exists
                and (completed_phases or past_master_plan)
                and not self._source_changed):
            self.ui.status("Master plan already exists, skipping.", C_GREEN)
        else:
            # Archive old master plan before creating new one (extend mode)
            if self._source_changed and master_plan_exists:
                archive_file(master_plan_path, self.working_dir)
                self.ui.status("Archived previous master plan.", C_GREEN)
            self.ui.status("Starting master plan creation...", C_CYAN)
            mp_result = self._do_master_plan()
            if not mp_result:
                if self._interrupt_requested:
                    self._show_exit_info()
                    return
                self.ui.error("Master plan phase failed.")
                self._show_exit_info()
                return
            if mp_result == "skipped":
                self.ui.status("Master plan proceeding (user-skipped, not inspector-approved).", C_YELLOW)
            else:
                self.ui.status("Master plan APPROVED!", C_GREEN)

        # 3. Count phases
        phase_range = self._count_phases()
        if phase_range is None:
            self.ui.status("Could not determine phase range from master plan.", C_YELLOW)
            try:
                start_phase = int(input(
                    f"  {C_CYAN}> First phase number (0 or 1) [1]: {C_RESET}"
                ).strip() or "1")
                total_input = int(input(
                    f"  {C_CYAN}> How many phases? {C_RESET}").strip())
            except (ValueError, EOFError):
                start_phase = 1
                total_input = 1
            end_phase = start_phase + total_input - 1
        else:
            start_phase, end_phase = phase_range

        total_count = end_phase - start_phase + 1
        self.ui.status(
            f"Phases: {start_phase} to {end_phase} ({total_count} total)", C_CYAN)

        # Migration: if phases start at 0 and Phase 0 is not in completed_phases
        # but a later phase IS completed, Phase 0 must have been done already
        # (the old code skipped Phase 0 but executors built it inside Phase 1)
        if (start_phase == 0 and 0 not in completed_phases
                and any(p > 0 for p in completed_phases)):
            completed_phases.append(0)
            self.state.set("phases_completed", completed_phases)
            self.ui.status(
                "Phase 0 auto-marked as completed (prerequisite for later phases).",
                C_GREEN)

        # 4. Phase loop (skip already-completed phases on resume)
        for phase_num in range(start_phase, end_phase + 1):
            if phase_num in completed_phases:
                self.ui.status(f"Phase {phase_num} already completed, skipping.", C_GREEN)
                continue

            self._check_pause()
            if self._interrupt_requested:
                self._show_exit_info()
                return

            self.state.set("current_phase", phase_num)
            generate_project_status(self.state, self.working_dir)

            # Skip planning if resuming mid-phase and already past the plan step
            skip_plan = (can_resume and phase_num == saved_phase
                         and saved_step in ("exec", "exec_review", "checkpoint"))

            # ── Plan ──
            if skip_plan:
                self.ui.status(f"Phase {phase_num} plan already done (resuming at {saved_step}), skipping.", C_GREEN)
            else:
                phase_pos = phase_num - start_phase + 1
                self.ui.status(f"Phase {phase_num} ({phase_pos}/{total_count}): Planning...", C_CYAN)
                plan_result = self._do_phase_plan(phase_num)
                if not plan_result:
                    if self._interrupt_requested:
                        self._show_exit_info()
                        return
                    self.ui.error(f"Phase {phase_num} planning failed.")
                    self._show_exit_info()
                    return

                if plan_result == "skipped":
                    self.ui.status(f"Phase {phase_num} plan proceeding (user-skipped).", C_YELLOW)
                else:
                    self.ui.status(f"Phase {phase_num} plan APPROVED!", C_GREEN)
            time.sleep(COOLDOWN_BETWEEN)

            self._check_pause()
            if self._interrupt_requested:
                self._show_exit_info()
                return

            # ── Execute ──
            phase_pos = phase_num - start_phase + 1
            self.ui.status(f"Phase {phase_num} ({phase_pos}/{total_count}): Executing...", C_CYAN)
            exec_result = self._do_phase_execution(phase_num)
            if not exec_result:
                if self._interrupt_requested:
                    self._show_exit_info()
                    return
                self.ui.error(f"Phase {phase_num} execution failed.")
                self._show_exit_info()
                return

            if exec_result == "skipped":
                self.ui.status(f"Phase {phase_num} execution proceeding (user-skipped, NOT verified).", C_YELLOW)
            else:
                self.ui.status(f"Phase {phase_num} execution APPROVED!", C_GREEN)

            # ── Summary ──
            self._get_phase_summary(phase_num)
            if self._interrupt_requested:
                self._show_exit_info()
                return
            cur_completed = self.state.get("phases_completed", [])
            cur_completed.append(phase_num)
            self.state.set("phases_completed", cur_completed)
            generate_project_status(self.state, self.working_dir)

            # ── User checkpoint ──
            choice = self.ui.user_checkpoint(
                phase_num,
                self.state.get("executor_session_id", "unknown"),
                self.state.get("inspector_session_id", "unknown"),
                auto_timeout=self.auto_continue_timeout,
            )
            if choice == "d":
                self.ui.status("Project marked as done by user.", C_GREEN)
                break
            elif choice == "a":
                self.ui.status("Aborted by user.", C_RED)
                break
            # choice == "c" → continue to next phase (or finish if last)
            if phase_num < end_phase:
                time.sleep(COOLDOWN_BETWEEN)

        # Final
        self.state.update(current_step="completed")
        generate_project_status(self.state, self.working_dir)
        self._show_exit_info()

    # ------------------------------------------------------------------
    # Exit info
    # ------------------------------------------------------------------

    def _show_exit_info(self):
        executor_sid = self.state.get("executor_session_id", "N/A")
        inspector_sid = self.state.get("inspector_session_id", "N/A")
        completed = self.state.get("phases_completed", [])
        total_exec = self.state.get("total_executor_calls", 0)
        total_insp = self.state.get("total_inspector_calls", 0)
        elapsed = time.time() - getattr(self, "_start_time", time.time())
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)

        print()
        self.ui.box("SESSION COMPLETE", (
            f"Phases completed: {completed}\n"
            f"Total codex calls: {total_exec + total_insp} "
            f"(executor: {total_exec}, inspector: {total_insp})\n"
            f"Total time: {elapsed_min}m {elapsed_sec}s\n"
            f"Working directory: {self.working_dir}\n"
            f"Logs: {os.path.join(self.gov_dir, 'logs')}\n"
            f"\n"
            f"To resume chat with Executor:\n"
            f"  codex resume {executor_sid}\n"
            f"\n"
            f"To resume chat with Inspector:\n"
            f"  codex resume {inspector_sid}\n"
            f"\n"
            f"State saved to: {self.state.state_path}\n"
            f"Re-run government.py with same --workdir to resume."
        ), C_GREEN)


# ============================================================================
# MAIN
# ============================================================================

def main():
    _configure_stdio()

    source_file = None
    working_dir = None
    user_instructions = ""
    codex_bin = None
    max_rounds = MAX_REVIEW_ROUNDS
    auto_continue = AUTO_CONTINUE_TIMEOUT
    resume_mode = False

    for arg in sys.argv[1:]:
        if arg.startswith("--source="):
            source_file = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--workdir="):
            working_dir = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--instructions="):
            user_instructions = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--codex-bin="):
            codex_bin = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--max-review-rounds="):
            try:
                max_rounds = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --max-review-rounds: {arg}")
                sys.exit(1)
        elif arg.startswith("--timeout="):
            try:
                global SILENCE_TIMEOUT
                SILENCE_TIMEOUT = max(30, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --timeout: {arg}")
                sys.exit(1)
        elif arg.startswith("--auto-continue="):
            try:
                auto_continue = max(0, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --auto-continue: {arg}")
                sys.exit(1)
        elif arg in ("--help", "-h"):
            print("GOVERNMENT - Multi-AI Phased Execution System")
            print()
            print("Usage:")
            print("  python government.py                      Interactive (asks for directory)")
            print("  python government.py <directory>           Resume or start in directory")
            print("  python government.py --source=<file>       Start new project from spec")
            print()
            print("Options:")
            print("  --source=FILE              Project spec/plan file")
            print("  --workdir=DIR              Working directory")
            print("  --instructions=\"...\"       Additional instructions for agents")
            print("  --max-review-rounds=N      Soft stop after N rounds (default: 7)")
            print("  --timeout=N                Silence timeout in seconds (default: 600)")
            print("  --auto-continue=N          Auto-continue after N seconds (default: 300, 0=manual)")
            print("  --codex-bin=PATH           Path to codex binary")
            print("  --help                     Show this help")
            print()
            print("Examples:")
            print("  python government.py C:\\MyProject")
            print("  python government.py --source=spec.md")
            print("  python government.py --source=plan.md --workdir=C:\\MyProject")
            print("  python government.py --auto-continue=0     # manual mode, no auto-continue")
            sys.exit(0)
        elif not arg.startswith("-"):
            # Positional arg: directory or file
            if os.path.isdir(arg):
                working_dir = arg
            elif os.path.isfile(arg):
                source_file = arg

    # Smart resume: detect existing project in working directory
    if not source_file:
        ui = TerminalUI()
        ui.clear()
        ui.banner()

        print(f"  {C_CYAN}Welcome to Government!{C_RESET}")
        print()

        # Ask for working directory if not provided
        if not working_dir:
            default_wd = os.getcwd()
            try:
                wd_input = input(
                    f"  {C_CYAN}> Working directory [{default_wd}]: {C_RESET}"
                ).strip().strip('"')
            except EOFError:
                print(f"\n  {C_RED}No input (non-interactive). "
                      f"Use: python government.py <directory>{C_RESET}")
                sys.exit(1)
            working_dir = wd_input if wd_input else default_wd

        # Check for existing Government project
        state_path = os.path.join(working_dir, ".government", "state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)

                saved_source = saved.get("source_file", "")
                completed = saved.get("phases_completed", [])
                step = saved.get("current_step", "init")
                phase = saved.get("current_phase", 0)

                status_label = "COMPLETED" if step == "completed" else f"Phase {phase}, step: {step}"
                ui.box("Existing Project Found", (
                    f"Source: {saved_source}\n"
                    f"Completed phases: {completed}\n"
                    f"Last: {status_label}\n"
                    f"Executor: {saved.get('executor_session_id', 'N/A')}\n"
                    f"Inspector: {saved.get('inspector_session_id', 'N/A')}"
                ), C_GREEN)
                print(f"  {C_BOLD}r{C_RESET} = resume project")
                if completed:
                    print(f"  {C_BOLD}e{C_RESET} = extend with new plan (keep history & sessions)")
                print(f"  {C_BOLD}n{C_RESET} = start fresh (new project)")
                print(f"  {C_BOLD}q{C_RESET} = quit")
                print()
                valid_choices = ("r", "e", "n", "q") if completed else ("r", "n", "q")
                while True:
                    try:
                        choice = input(
                            f"  {C_CYAN}> Choice [{'/'.join(valid_choices)}]: {C_RESET}"
                        ).strip().lower()
                    except EOFError:
                        choice = "q"
                        break
                    if choice in valid_choices:
                        break

                if choice == "q":
                    sys.exit(0)
                elif choice == "r":
                    source_file = saved_source
                    user_instructions = saved.get("user_instructions", "")
                    resume_mode = True
                elif choice == "e":
                    # Extend: ask for new plan file, keep sessions + history
                    print()
                    print(f"  {C_DIM}Extending project with a new plan file.{C_RESET}")
                    print(f"  {C_DIM}Completed phases {completed} will be preserved.{C_RESET}")
                    print()
                    while True:
                        try:
                            new_source = input(
                                f"  {C_CYAN}> New plan file path: {C_RESET}"
                            ).strip().strip('"')
                        except EOFError:
                            print(f"\n  {C_RED}No input.{C_RESET}")
                            sys.exit(1)
                        if os.path.isfile(new_source):
                            break
                        print(f"  {C_RED}File not found: {new_source}{C_RESET}")
                    source_file = new_source
                    # Ask for new instructions
                    old_instr = saved.get("user_instructions", "")
                    if old_instr:
                        print(f"  {C_DIM}Previous instructions: {old_instr[:100]}{'...' if len(old_instr) > 100 else ''}{C_RESET}")
                    try:
                        extra = input(
                            f"  {C_CYAN}> New instructions (Enter to keep previous): {C_RESET}"
                        ).strip()
                    except EOFError:
                        extra = ""
                    user_instructions = extra if extra else old_instr
                    resume_mode = True
                # choice == "n" → fall through to ask for source file
            except (json.JSONDecodeError, OSError):
                pass  # corrupted state, treat as no project

        # If still no source file, ask for one
        if not source_file:
            print(f"  {C_DIM}I need a project source file (your plan/spec) to get started.{C_RESET}")
            print()
            while True:
                try:
                    source_file = input(
                        f"  {C_CYAN}> Source file path: {C_RESET}"
                    ).strip().strip('"')
                except EOFError:
                    print(f"\n  {C_RED}No input. Use --source=FILE.{C_RESET}")
                    sys.exit(1)
                if os.path.isfile(source_file):
                    break
                print(f"  {C_RED}File not found: {source_file}{C_RESET}")

            if not user_instructions:
                try:
                    extra = input(
                        f"  {C_CYAN}> Additional instructions (or Enter to skip): {C_RESET}"
                    ).strip()
                except EOFError:
                    extra = ""
                if extra:
                    user_instructions = extra

        print()

    if not working_dir:
        working_dir = os.path.join(os.getcwd(), "government_workspace")

    gov = Government(
        source_file=source_file,
        working_dir=working_dir,
        user_instructions=user_instructions,
        codex_bin=codex_bin,
        max_review_rounds=max_rounds,
        auto_continue_timeout=auto_continue,
        resume_mode=resume_mode,
    )
    gov.run()


if __name__ == "__main__":
    main()
