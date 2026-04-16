"""
CONGRESS v2 - Multi-AI Debate System
=====================================
Two Codex agents (Researcher + Inspector) debate in a loop to produce
high-quality output. The Inspector critiques the Researcher's work,
and the Researcher improves based on feedback.

New in v2:
  - State persistence + resume after rate limit / crash / internet drop
  - File-based context: only last 2 rounds passed; agents read files directly
  - Source file injection: agents re-read requirements files every round
  - User-declared output files as the real deliverables
  - Researcher stdout saved as researcher_updated.md process notes
  - Inspector reviews deliverable files plus researcher notes every round
  - In-execution pause key [P] during codex runs
  - Internet check: auto-pause when connection drops
  - Rate limit: pause + retry instead of abort
  - Recovery context on retry attempts

Usage:
    python congress.py
    python congress.py --query="How do I implement a binary search tree?"
    python congress.py --outputs="answer.md,notes.txt" --query="..."
    python congress.py --max-rounds=5
    python congress.py --timeout=900
    python congress.py --workdir="C:\\MyProject"
"""

import subprocess
import sys
import os
import re
import time
import shutil
import socket
import threading
import queue
import json
import uuid
from pathlib import Path
from datetime import datetime


# ============================================================================
# CONSTANTS
# ============================================================================

MAX_ROUNDS            = 3
SILENCE_TIMEOUT       = 3600     # kill codex if zero output for 60 min
STARTUP_TIMEOUT       = 3600     # kill if no output within 60 min of launch
COOLDOWN_BETWEEN      = 5        # seconds between codex calls
LOG_DIR               = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
APPROVAL_FLAG         = "--dangerously-bypass-approvals-and-sandbox"
MAX_PROMPT_CHARS      = 50000    # max inline text in prompts
AUTO_CONTINUE_SECS    = 10       # seconds before auto-continue in transition menu
CONGRESS_ROUNDS_DIR   = "congress_rounds"   # stable round output files (in working_dir)
CONGRESS_STATE_FILE   = "congress_state.json"  # resume state (in working_dir)
RESEARCHER_UPDATED_FILE = "researcher_updated.md"  # managed researcher notes / reasoning doc
INSPECTOR_COMMENTS_FILE = "inspector_comments.md"  # managed inspector review doc
SESSION_REQUEST_FILE  = "session_request.md"  # managed copy of the original request + output contract
STATE_VERSION         = 2
MAX_RECOVERY_RETRIES  = 3        # max retries for network recovery in _run_with_recovery
RATE_LIMIT_AUTO_RETRY_SECONDS = 3600  # auto-retry once per hour while waiting on rate limits
MAX_CONTINUATIONS     = 10       # max "continue" prompts when context limit cuts output
INTERNET_CHECK_INTERVAL = 30     # seconds between internet checks during silence

# Real API rate / usage-cap keywords (need pause + retry).
# Kept specific to avoid false-positives from context-window messages.
RATE_LIMIT_SIGNALS = (
    "rate limit reached",       # "Rate limit reached for organization..."
    "rate_limit",               # API error code
    "hit your usage limit",     # "You've hit your usage limit."
    "usage limit reached",      # "usage limit reached" (cline/codex variants)
    "try again at",             # retry timestamp in message
    "too many requests",        # HTTP 429 text
    "429",                      # HTTP status code
    "tokens per min",           # TPM rate limit detail
    "requests per min",         # RPM rate limit detail
    "quota exceeded",           # quota hard stop
    "billing",                  # billing/payment block
)

# Context window / output-token exhaustion keywords.
# These cause Codex to stop mid-output but are NOT rate limits —
# no pause/retry needed, just accept whatever was captured.
CONTEXT_LIMIT_SIGNALS = (
    "exceeds the context window",   # "Your input exceeds the context window of this model"
    "context length exceeded",      # "context length exceeded"
    "context_length_exceeded",      # API error code
    "maximum context length",       # "This model's maximum context length is X tokens"
    "please adjust your input",     # "Please adjust your input and try again"
    "input is too long",            # generic too-long message
    "input exceeds",                # "input exceeds the context window"
)

# Network error keywords
NETWORK_ERROR_SIGNALS = (
    "network error", "connection refused", "connection reset",
    "dns resolution", "etimedout", "econnrefused", "enotfound",
    "socket hang up", "fetch failed", "econnreset",
    "unable to connect", "network is unreachable",
    "no internet", "getaddrinfo", "eai_again",
)

# Terminal colors (ANSI)
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
# SYSTEM PROMPTS
# ============================================================================

RESEARCHER_SYSTEM_PROMPT = """You are the RESEARCHER/PLANNER agent in a multi-agent AI system called "Congress".

YOUR ROLE:
- You are a world-class engineer, architect, analyst, and problem solver.
- When given a question or task, you must deeply analyze it, research all angles, and produce the BEST possible solution. (and search in internet to get most update information)
- Think step by step. Consider edge cases, performance, maintainability, and security.
- The requested output files on disk are the REAL deliverables for the user.
- Your stdout is NOT the final deliverable. Congress saves your stdout into researcher_updated.md as your findings, reasoning, and change log for the Inspector.
- You can handle ANY type of task: coding, analysis, research, architecture, debugging, etc.

CRITICAL RULES:
1. Read the original request, session_request.md, every source file, every requested output file, and prior Congress-managed docs exactly as instructed every round.
2. Create or update ONLY the requested output files on disk. If an output file already exists, read it fully before editing it.
3. Do NOT treat stdout as the final deliverable. Stdout must explain what you changed, why you changed it, and which output files were affected.
4. Do NOT ask follow-up questions, if you have question, search in internet and find possible answers, and in your output mention each option based on each answer!, Work with what you have from resources, do not assumpt or guess, we need real resourced information.
5. If the task involves code, make it complete, runnable, well-commented, and secure.
6. If analyzing something, be exhaustive and precise.
7. Structure your response clearly with labeled sections.
8. Always explain your reasoning, trade-offs considered, and alternatives rejected.
9. Do NOT modify Congress-managed files yourself unless explicitly instructed: researcher_updated.md, inspector_comments.md, session_request.md, congress_state.json.
10. DO NOT wrap your entire response in a markdown code block — write it as plain structured text.

OUTPUT FORMAT:
- UNDERSTANDING: Brief summary of what you understood the task to be.
- OUTPUT FILE CHANGES: For each requested output file, state whether you created, updated, or intentionally left it unchanged, and why.
- RESEARCH / REASONING: Your detailed solution/analysis.
- Refrences or ASSUMPTIONS: List all refrence or any assumptions you made (and explain why you did not found any refrence for this aasumption).
"""

INSPECTOR_SYSTEM_PROMPT_TEMPLATE = """You are the INSPECTOR/SUPERVISOR agent in a multi-agent AI system called "Congress".

YOUR ROLE:
- You are a ruthless but fair reviewer, security auditor, and quality inspector.
- You receive the ORIGINAL user request, the requested output files on disk, and the RESEARCHER's explanation report.
- Your job is to find EVERY flaw, bug, security issue, logic error, missed edge case, and deliverable gap in the requested output files. 

{previous_review_context}

YOUR TASKS:
1. VERIFY: Do the requested output files actually satisfy the user's original request completely?
2. FILE REVIEW: Inspect every requested output file directly from disk. Missing, stale, or wrong output files are issues.
3. BUGS: Find all bugs, logic errors, off-by-one errors, race conditions, null/undefined risks.
4. PERFORMANCE: Flag performance issues, unnecessary complexity, O(n^2) where O(n) suffices.
5. EDGE CASES: What inputs/scenarios would break this? Empty inputs, huge inputs, unicode, concurrency.
6. COMPLETENESS: Is anything missing? Unhandled error cases? Missing validation?
7. BEST PRACTICES: Industry conventions, naming, structure, documentation.
8. IMPROVEMENTS: Suggest specific, actionable improvements WITH code examples when applicable.

CRITICAL RULES:
1. DO NOT create, modify, or delete files. Your text response IS your output.
2. Read every required file from disk exactly as instructed every round, including the requested output files, session_request.md, source files, researcher_updated.md, and prior inspector comments when available.
3. Be SPECIFIC. Don't say "improve error handling" — say exactly WHERE and HOW. (and how can check and what avoid)
4. Provide code snippets for your suggested fixes when possible. 
5. Rate the severity of each issue: CRITICAL / HIGH / MEDIUM / LOW.
6. At the VERY END of your review, on its own line, you MUST write exactly one of:
   VERDICT: NEEDS_REVISION
   VERDICT: APPROVED
   Use NEEDS_REVISION if ANY critical or high severity issues remain.
   Use APPROVED only if the work is solid (only medium/low issues remain).

OUTPUT FORMAT:
## Summary
Brief overall assessment (2-3 sentences).

## Issues Found
For each issue:
- [SEVERITY] Issue title
- Location: where exactly
- Problem: what's wrong
- Fix: specific fix with code

## Improvement Suggestions
Actionable improvements ranked by impact.

## Verdict
VERDICT: NEEDS_REVISION or VERDICT: APPROVED
"""


# ============================================================================
# LOGGING SYSTEM
# ============================================================================

class CongressLogger:
    """Comprehensive logging system that captures everything."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = os.path.join(LOG_DIR, session_id)
        os.makedirs(self.session_dir, exist_ok=True)

        self.master_log_path     = os.path.join(self.session_dir, "master.log")
        self.researcher_log_path = os.path.join(self.session_dir, "researcher.log")
        self.inspector_log_path  = os.path.join(self.session_dir, "inspector.log")
        self.rounds_dir          = os.path.join(self.session_dir, "rounds")
        os.makedirs(self.rounds_dir, exist_ok=True)
        self.meta_path = os.path.join(self.session_dir, "session.json")
        self.meta = {
            "session_id":  session_id,
            "started_at":  datetime.now().isoformat(),
            "status":      "running",
            "rounds":      [],
            "user_query":  "",
            "output_files": [],
        }
        self._save_meta()

        for path in [self.master_log_path, self.researcher_log_path, self.inspector_log_path]:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{'=' * 80}\n")
                f.write(f"  CONGRESS SESSION: {session_id}\n")
                f.write(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"{'=' * 80}\n\n")

    def _save_meta(self):
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(self.meta, f, indent=2, default=str)
        except OSError as e:
            _print_safe(f"  {C_DIM}[LOG WARNING] Could not save meta: {e}{C_RESET}")

    def _append(self, filepath: str, text: str):
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            _print_safe(f"  {C_DIM}[LOG WARNING] Could not append to {filepath}: {e}{C_RESET}")

    def log_master(self, tag: str, message: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._append(self.master_log_path, f"[{ts}] [{tag}] {message}\n")

    def log_user_query(self, query: str):
        self.meta["user_query"] = query
        self._save_meta()
        self.log_master("USER", f"Query: {query[:200]}...")
        self._append(self.master_log_path,
                     f"\n{'=' * 60}\nUSER QUERY:\n{'=' * 60}\n{query}\n{'=' * 60}\n\n")

    def log_output_files(self, output_files: list[str]):
        self.meta["output_files"] = list(output_files)
        self._save_meta()
        joined = ", ".join(output_files) if output_files else "(none)"
        self.log_master("USER", f"Output files: {joined}")
        if output_files:
            listing = "\n".join(f"  - {path}" for path in output_files)
            self._append(self.master_log_path,
                         f"\n{'=' * 60}\nREQUESTED OUTPUT FILES:\n{'=' * 60}\n"
                         f"{listing}\n{'=' * 60}\n\n")

    def log_agent_start(self, agent: str, round_num, prompt: str):
        self.log_master(agent.upper(), f"Round {round_num} - Started")
        log_path = (self.researcher_log_path if agent.startswith("researcher")
                    else self.inspector_log_path)
        self._append(log_path,
                     f"\n{'=' * 60}\n"
                     f"ROUND {round_num} - STARTED at {datetime.now().strftime('%H:%M:%S')}\n"
                     f"{'=' * 60}\n"
                     f"PROMPT SENT ({len(prompt)} chars):\n{'─' * 40}\n{prompt}\n{'─' * 40}\n\n")

    def log_agent_output(self, agent: str, round_num, stdout: str, stderr: str,
                         returncode: int, duration: float):
        self.log_master(agent.upper(),
                        f"Round {round_num} - Finished (rc={returncode}, {duration:.1f}s, "
                        f"{len(stdout)} chars stdout, {len(stderr)} chars stderr)")
        log_path = (self.researcher_log_path if agent.startswith("researcher")
                    else self.inspector_log_path)
        self._append(log_path,
                     f"OUTPUT (exit={returncode}, duration={duration:.1f}s):\n"
                     f"{'─' * 40}\n{stdout}\n{'─' * 40}\n")
        if stderr.strip():
            self._append(log_path, f"STDERR:\n{stderr}\n{'─' * 40}\n")

        round_file = os.path.join(self.rounds_dir, f"round_{round_num}_{agent}.txt")
        try:
            with open(round_file, "w", encoding="utf-8") as f:
                f.write(f"Agent: {agent}\nRound: {round_num}\n"
                        f"Return Code: {returncode}\nDuration: {duration:.1f}s\n")
                f.write(f"{'=' * 60}\nOUTPUT:\n{'=' * 60}\n{stdout}\n")
                if stderr.strip():
                    f.write(f"\n{'=' * 60}\nSTDERR:\n{'=' * 60}\n{stderr}\n")
        except OSError:
            pass

    def log_round_summary(self, round_num: int, verdict: str):
        self.meta["rounds"].append({
            "round":        round_num,
            "verdict":      verdict,
            "completed_at": datetime.now().isoformat(),
        })
        self._save_meta()
        self.log_master("SYSTEM", f"Round {round_num} verdict: {verdict}")

    def log_session_end(self, final_output: str, total_rounds: int,
                        status: str = "completed"):
        self.meta["status"]       = status
        self.meta["total_rounds"] = total_rounds
        self.meta["ended_at"]     = datetime.now().isoformat()
        self._save_meta()

        final_path = os.path.join(self.session_dir, "final_output.txt")
        try:
            with open(final_path, "w", encoding="utf-8") as f:
                f.write(final_output)
        except OSError:
            pass

        self.log_master("SYSTEM",
                        f"Session {status}. {total_rounds} rounds. "
                        f"Output saved to final_output.txt")


# ============================================================================
# TERMINAL UI
# ============================================================================

def _strip_ansi(text: str) -> str:
    return re.sub(r'\033\[[0-9;]*m', '', text)


class TerminalUI:

    def __init__(self):
        self._width = min(shutil.get_terminal_size().columns, 120)
        if os.name == "nt":
            os.system("")   # enable ANSI on Windows

    def _center(self, text: str, width: int = 0) -> str:
        return text.center(width or self._width)

    def clear(self):
        os.system("cls" if os.name == "nt" else "clear")

    def clear_line(self):
        sys.stdout.write("\r" + " " * (self._width - 1) + "\r")
        sys.stdout.flush()

    def banner(self):
        print()
        print(f"{C_CYAN}{C_BOLD}")
        print(self._center("=" * 60))
        print(self._center(""))
        print(self._center("  CONGRESS v2  "))
        print(self._center("  Multi-AI Debate System  "))
        print(self._center(""))
        print(self._center("  Researcher + Inspector Feedback Loop  "))
        print(self._center(""))
        print(self._center("=" * 60))
        print(f"{C_RESET}")
        print()

    def box(self, title: str, content: str, color: str = C_CYAN):
        w = self._width - 4
        title_pad = max(0, w - len(_strip_ansi(title)))
        print(f"  {color}{BOX_TL}{BOX_H * (w + 2)}{BOX_TR}{C_RESET}")
        print(f"  {color}{BOX_V}{C_RESET} {C_BOLD}{title}{C_RESET}"
              f"{' ' * title_pad}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_V}{BOX_H * (w + 2)}{BOX_V}{C_RESET}")
        for line in content.split("\n"):
            clean = _strip_ansi(line)
            display = clean[:w] if len(clean) > w else line
            padding = max(0, w - len(_strip_ansi(display)))
            print(f"  {color}{BOX_V}{C_RESET} {display}{' ' * padding}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_BL}{BOX_H * (w + 2)}{BOX_BR}{C_RESET}")
        print()

    def status(self, message: str, color: str = C_YELLOW):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  {C_DIM}[{ts}]{C_RESET} {color}{C_BOLD}{message}{C_RESET}")

    def agent_header(self, agent_name: str, round_num: int, total_rounds: int, phase: str):
        color = C_BLUE if agent_name == "RESEARCHER" else C_MAGENTA
        icon  = "[R]"  if agent_name == "RESEARCHER" else "[I]"
        print()
        print(f"  {color}{C_BOLD}{'─' * (self._width - 4)}{C_RESET}")
        print(f"  {color}{C_BOLD}  {icon} {agent_name} - Round {round_num}/{total_rounds} - {phase}{C_RESET}")
        print(f"  {color}{C_BOLD}{'─' * (self._width - 4)}{C_RESET}")
        print()

    def stream_line(self, agent: str, line: str):
        if agent == "researcher":
            prefix = f"  {C_BLUE}{C_DIM}R |{C_RESET} "
        elif agent == "inspector":
            prefix = f"  {C_MAGENTA}{C_DIM}I |{C_RESET} "
        else:
            prefix = f"  {C_DIM}  |{C_RESET} "
        max_len = self._width - 10
        display = line.rstrip()
        if len(display) > max_len:
            display = display[:max_len - 3] + "..."
        _print_safe(f"{prefix}{display}")

    def verdict_display(self, verdict: str, round_num: int, max_rounds: int):
        if verdict == "APPROVED":
            color = C_GREEN
            msg   = f"APPROVED - Inspector is satisfied after round {round_num}!"
        elif round_num >= max_rounds:
            color = C_YELLOW
            msg   = f"NEEDS REVISION - But max rounds ({max_rounds}) reached. Using latest output."
        else:
            color = C_YELLOW
            msg   = f"NEEDS REVISION - Moving to round {round_num + 1}"
        print()
        print(f"  {color}{C_BOLD}{'*' * (self._width - 4)}{C_RESET}")
        print(f"  {color}{C_BOLD}  VERDICT: {msg}{C_RESET}")
        print(f"  {color}{C_BOLD}{'*' * (self._width - 4)}{C_RESET}")
        print()

    def final_result(self, total_rounds: int, log_dir: str):
        print()
        print(f"  {C_GREEN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}  CONGRESS SESSION COMPLETE{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}  Total Rounds: {total_rounds}{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}  Logs: {log_dir}{C_RESET}")
        print(f"  {C_GREEN}{C_BOLD}{'=' * (self._width - 4)}{C_RESET}")
        print()

    def error(self, message: str):
        print(f"  {C_RED}{C_BOLD}[ERROR]{C_RESET} {C_RED}{message}{C_RESET}")


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
        sep  = kwargs.get("sep", " ")
        end  = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args)
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(end.encode("utf-8", errors="replace"))
        sys.stdout.flush()


# ============================================================================
# KEYBOARD INPUT HELPERS (cross-platform)
# ============================================================================

def _kbhit() -> bool:
    """Non-blocking check if a key has been pressed."""
    try:
        if os.name == "nt":
            import msvcrt
            return bool(msvcrt.kbhit())
        else:
            if not sys.stdin.isatty():
                return False
            import select
            return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def _consume_key() -> str:
    """Consume one keypress, return lowercase char. Empty string for special keys."""
    try:
        if os.name == "nt":
            import msvcrt
            ch = msvcrt.getch()
            if ch in (b'\xe0', b'\x00'):
                msvcrt.getch()   # consume second byte of special key
                return ""
            return ch.decode("utf-8", errors="replace").lower()
        else:
            ch = sys.stdin.read(1)
            return ch.lower() if ch else ""
    except Exception:
        return ""


def _flush_input():
    """Discard any buffered keypresses to prevent stale input in menus."""
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
    """Enter cbreak (single-keypress) mode on Unix. Returns old settings or None."""
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
    """Restore terminal to previous settings."""
    if old_settings is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


# ============================================================================
# NETWORK HELPERS
# ============================================================================

def _check_internet(timeout: int = 3) -> bool:
    """Check internet connectivity by connecting to api.openai.com:443."""
    try:
        socket.create_connection(("api.openai.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def _is_context_limit(stderr: str) -> bool:
    """Return True if stderr indicates context-window / output-token exhaustion.
    This is NOT a rate limit — no pause or retry needed, just accept partial output."""
    stderr_lower = stderr.lower() if stderr else ""
    return any(sig in stderr_lower for sig in CONTEXT_LIMIT_SIGNALS)


def _is_network_error(stderr: str, rc: int) -> bool:
    """Return True if stderr indicates a network-level error (not rate/context limit)."""
    if rc == 0:
        return False
    stderr_lower = stderr.lower() if stderr else ""
    # Don't re-classify rate-limit or context-limit errors as network errors
    if any(sig in stderr_lower for sig in RATE_LIMIT_SIGNALS):
        return False
    if _is_context_limit(stderr):
        return False
    return any(sig in stderr_lower for sig in NETWORK_ERROR_SIGNALS)


def _is_rate_limited(stderr: str, rc: int) -> tuple[bool, str]:
    """Return (is_limited, retry_info_string).
    Context-window exhaustion is explicitly excluded — that is not a rate limit."""
    stderr_lower = stderr.lower() if stderr else ""
    # Context limit takes priority — must not be misclassified as rate limit
    if _is_context_limit(stderr):
        return False, ""
    if any(sig in stderr_lower for sig in RATE_LIMIT_SIGNALS):
        retry_match = re.search(r'try again at\s+(.+?)[\.\n]', stderr, re.IGNORECASE)
        retry_info  = f" Retry after: {retry_match.group(1)}" if retry_match else ""
        return True, retry_info
    return False, ""


def _workspace_root(working_dir: str) -> str:
    return os.path.abspath(working_dir)


def _workspace_abs_path(working_dir: str, relpath: str) -> str:
    return os.path.abspath(os.path.join(working_dir, relpath.replace("/", os.sep)))


def _normalize_workspace_relpath(working_dir: str, raw_path: str) -> str:
    raw = raw_path.strip().strip('"').strip("'")
    if not raw:
        raise ValueError("Output file names cannot be empty.")

    candidate = raw if os.path.isabs(raw) else os.path.join(working_dir, raw)
    abs_path = os.path.abspath(os.path.normpath(candidate))
    root = _workspace_root(working_dir)
    try:
        within_root = os.path.commonpath([root, abs_path]) == root
    except ValueError:
        within_root = False
    if not within_root:
        raise ValueError(f"Output path must stay inside the working directory: {raw_path}")

    relpath = os.path.relpath(abs_path, root).replace("\\", "/")
    if relpath in (".", ""):
        raise ValueError(f"Output path must point to a file, not the working directory: {raw_path}")
    if not os.path.basename(relpath):
        raise ValueError(f"Output path must include a file name: {raw_path}")
    if os.path.isdir(abs_path):
        raise ValueError(f"Output path points to a directory, not a file: {raw_path}")
    return relpath


def _reserved_output_relpaths() -> set[str]:
    return {
        os.path.normcase(CONGRESS_STATE_FILE.replace("\\", "/")),
        os.path.normcase(RESEARCHER_UPDATED_FILE.replace("\\", "/")),
        os.path.normcase(INSPECTOR_COMMENTS_FILE.replace("\\", "/")),
        os.path.normcase(SESSION_REQUEST_FILE.replace("\\", "/")),
    }


def _is_reserved_output_relpath(relpath: str) -> bool:
    normalized = os.path.normcase(relpath.replace("\\", "/"))
    if normalized in _reserved_output_relpaths():
        return True
    for prefix in (CONGRESS_ROUNDS_DIR, "logs", ".git", "__pycache__"):
        prefix_norm = os.path.normcase(prefix.replace("\\", "/"))
        if normalized == prefix_norm or normalized.startswith(prefix_norm + "/"):
            return True
    return False


def _parse_output_files(raw: str, working_dir: str) -> list[str]:
    if not raw or not raw.strip():
        raise ValueError("At least one output file is required.")

    return _normalize_output_file_list(raw.split(","), working_dir)


def _normalize_output_file_list(items: list[str], working_dir: str) -> list[str]:
    if not items:
        raise ValueError("At least one output file is required.")

    output_files: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"Output path must be a string, got {type(item).__name__}.")
        relpath = _normalize_workspace_relpath(working_dir, item)
        if _is_reserved_output_relpath(relpath):
            raise ValueError(f"Output path is reserved by Congress: {relpath}")
        key = os.path.normcase(relpath)
        if key in seen:
            continue
        seen.add(key)
        output_files.append(relpath)

    if not output_files:
        raise ValueError("At least one output file is required.")
    return output_files


def _collect_output_status(working_dir: str, output_files: list[str]) -> list[dict]:
    statuses = []
    for relpath in output_files:
        abs_path = _workspace_abs_path(working_dir, relpath)
        path_exists = os.path.exists(abs_path)
        invalid_type = path_exists and not os.path.isfile(abs_path)
        exists = os.path.isfile(abs_path)
        size = None
        mtime_ns = None
        if exists:
            try:
                stat = os.stat(abs_path)
                size = stat.st_size
                mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
            except OSError:
                exists = False
                size = None
                mtime_ns = None
        statuses.append({
            "path": relpath,
            "exists": exists,
            "invalid_type": invalid_type,
            "size": size,
            "mtime_ns": mtime_ns,
        })
    return statuses


def _summarize_output_status(before: list[dict], after: list[dict]) -> dict[str, list[str]]:
    before_map = {item["path"]: item for item in before}
    summary = {
        "created": [],
        "updated": [],
        "unchanged": [],
        "missing": [],
        "invalid": [],
    }
    for item in after:
        path = item["path"]
        prev = before_map.get(path, {})
        if item.get("invalid_type"):
            summary["invalid"].append(path)
        elif not item["exists"]:
            summary["missing"].append(path)
        elif not prev.get("exists"):
            summary["created"].append(path)
        elif item["size"] != prev.get("size") or item["mtime_ns"] != prev.get("mtime_ns"):
            summary["updated"].append(path)
        else:
            summary["unchanged"].append(path)
    return summary


def _write_round_output_status(working_dir: str, round_num: int,
                               before: list[dict], after: list[dict]):
    rdir = _rounds_dir(working_dir)
    os.makedirs(rdir, exist_ok=True)
    path = os.path.join(rdir, f"round_{round_num}_outputs.json")
    payload = {
        "before": before,
        "after": after,
        "summary": _summarize_output_status(before, after),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        _print_safe(f"  {C_DIM}[WARN] Could not write {path}: {e}{C_RESET}")


def _write_session_request(working_dir: str, user_query: str, output_files: list[str]) -> bool:
    path = os.path.join(working_dir, SESSION_REQUEST_FILE)
    listing = "\n".join(f"- {item}" for item in output_files) if output_files else "- (none)"
    content = (
        "# Congress Session Request\n\n"
        "This file is generated by Congress and should be re-read every round.\n\n"
        "## Requested Output Files\n"
        f"{listing}\n\n"
        "The files above are the real deliverables for the user.\n"
        f"{RESEARCHER_UPDATED_FILE} is the Researcher's explanation/process notes.\n"
        f"{INSPECTOR_COMMENTS_FILE} is the Inspector's review.\n\n"
        "## Original User Request\n"
        f"{user_query}\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except OSError as e:
        _print_safe(f"  {C_DIM}[WARN] Could not write session request {path}: {e}{C_RESET}")
        return False


# ============================================================================
# STATE & ROUND-FILE HELPERS
# ============================================================================

def _state_path(working_dir: str) -> str:
    return os.path.join(working_dir, CONGRESS_STATE_FILE)


def _load_state(working_dir: str) -> dict | None:
    """Load congress_state.json from working_dir. Returns None if missing/corrupt."""
    try:
        with open(_state_path(working_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_state_file(working_dir: str, data: dict):
    """Atomically write congress_state.json."""
    path = _state_path(working_dir)
    tmp  = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        # Atomic replace
        if os.path.exists(path):
            os.replace(tmp, path)
        else:
            os.rename(tmp, path)
    except OSError as e:
        _print_safe(f"  {C_DIM}[STATE WARNING] Could not save state: {e}{C_RESET}")


def _delete_state(working_dir: str):
    """Remove congress_state.json (session complete)."""
    try:
        os.remove(_state_path(working_dir))
    except OSError:
        pass


def _rounds_dir(working_dir: str) -> str:
    return os.path.join(working_dir, CONGRESS_ROUNDS_DIR)


def _write_round_output(working_dir: str, round_num: int, agent: str, content: str):
    """Write clean round output to stable path congress_rounds/round_N_agent.md."""
    rdir = _rounds_dir(working_dir)
    os.makedirs(rdir, exist_ok=True)
    path = os.path.join(rdir, f"round_{round_num}_{agent}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        _print_safe(f"  {C_DIM}[WARN] Could not write {path}: {e}{C_RESET}")


def _read_round_output(working_dir: str, round_num: int, agent: str) -> str:
    """Read round output from congress_rounds/round_N_agent.md. Returns '' if missing."""
    path = os.path.join(_rounds_dir(working_dir), f"round_{round_num}_{agent}.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _write_living_doc(working_dir: str, filename: str, content: str,
                      version: int, role: str) -> bool:
    """
    Write/overwrite a living document with a version header.
    Returns True on success, False on failure.
    These are the single always-up-to-date files agents read each round.
    """
    path = os.path.join(working_dir, filename)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f"# {role} — Version v{version}\n"
        f"# Last updated: {ts}\n"
        f"{'=' * 60}\n\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + content)
        return True
    except OSError as e:
        _print_safe(f"  {C_DIM}[WARN] Could not write living doc {path}: {e}{C_RESET}")
        return False


def _detect_source_files(working_dir: str, excluded_relpaths: list[str] | None = None) -> list[str]:
    """
    Auto-detect reference/source files in working_dir (non-recursive).
    Excludes congress-managed files and hidden files.
    """
    source_extensions = {
        ".txt", ".md", ".pdf", ".doc", ".docx",
        ".json", ".yaml", ".yml", ".toml", ".csv",
        ".py", ".js", ".ts", ".java", ".go", ".rs", ".rb",
    }
    excluded_names = {
        "congress.py",           # this script
        CONGRESS_STATE_FILE,     # congress_state.json
        RESEARCHER_UPDATED_FILE, # living researcher doc (auto-generated)
        INSPECTOR_COMMENTS_FILE, # living inspector doc (auto-generated)
        SESSION_REQUEST_FILE,    # managed session request
    }
    excluded_dirs = {
        CONGRESS_ROUNDS_DIR, "logs", "__pycache__", ".git",
        "node_modules", ".venv", "venv",
    }
    excluded_norm = {
        os.path.normcase(item.replace("\\", "/"))
        for item in (excluded_relpaths or [])
    }

    found = []
    try:
        with os.scandir(working_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() and entry.name in excluded_dirs:
                    continue
                if entry.is_file():
                    if entry.name in excluded_names:
                        continue
                    relpath = os.path.relpath(entry.path, working_dir).replace("\\", "/")
                    if os.path.normcase(relpath) in excluded_norm:
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in source_extensions:
                        found.append(entry.path)
    except OSError:
        pass

    return sorted(found)


# ============================================================================
# CODEX CLI INTERFACE
# ============================================================================

def _resolve_codex_binary(cli_arg: str | None = None) -> str | None:
    if cli_arg and os.path.exists(cli_arg):
        return cli_arg

    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf_cmd = Path(program_files) / "nodejs" / "codex.cmd"
        if pf_cmd.exists():
            return str(pf_cmd)

        appdata = os.environ.get("APPDATA", "")
        if appdata:
            p = Path(appdata) / "npm" / "codex.cmd"
            if p.exists():
                return str(p)

        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            ext_root = Path(userprofile) / ".vscode" / "extensions"
            if ext_root.exists():
                matches = sorted(
                    ext_root.glob("openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"))
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


def _parse_session_id(stderr_text: str) -> str | None:
    """Extract session id from codex stderr output."""
    match = re.search(r'session id:\s*([0-9a-f-]+)', stderr_text, re.IGNORECASE)
    return match.group(1) if match else None


def run_codex(prompt: str, codex_bin: str, agent_name: str,
              ui: TerminalUI, logger: CongressLogger,
              round_num, working_dir: str,
              session_id: str | None = None,
              startup_timeout: int | None = None,
              silence_timeout: int | None = None,
              pause_event: threading.Event | None = None,
              ) -> tuple[str, str, int, float, str | None]:
    """
    Run Codex CLI with a prompt. If session_id is provided, resume that session.
    Returns (stdout, stderr, returncode, duration, session_id).

    rc special values:
      -1  launch/timeout error
      -2  KeyboardInterrupt or pause-then-quit
      -3  network error detected during silence
    """
    _startup_timeout = startup_timeout if startup_timeout is not None else STARTUP_TIMEOUT
    _silence_timeout = silence_timeout if silence_timeout is not None else SILENCE_TIMEOUT

    if os.name == "nt" and codex_bin.lower().endswith((".cmd", ".bat")):
        base_cmd = ["cmd.exe", "/c", codex_bin]
    else:
        base_cmd = [codex_bin]

    if session_id:
        cmd = base_cmd + ["exec", "resume", session_id,
                          APPROVAL_FLAG, "--skip-git-repo-check", "-"]
    else:
        cmd = base_cmd + ["exec", APPROVAL_FLAG, "--skip-git-repo-check", "-"]

    logger.log_master(agent_name.upper(), f"Executing: {' '.join(cmd)}")
    logger.log_master(agent_name.upper(), f"Working dir: {working_dir}")

    start_time = time.time()
    proc       = None

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=working_dir,
            env=env,
        )
    except Exception as e:
        duration = time.time() - start_time
        logger.log_master(agent_name.upper(), f"Failed to launch: {e}")
        return "", str(e), -1, duration, None

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        logger.log_master(agent_name.upper(), f"STDIN write failed: {e}")
        ui.error(f"Failed to send prompt to Codex: {e}")
        _kill_proc(proc)
        return "", f"stdin write failed: {e}", -1, time.time() - start_time, session_id

    # Reader threads
    q          = queue.Queue()
    t_out      = threading.Thread(target=_pipe_reader, args=(proc.stdout, q, "out"), daemon=True)
    t_err      = threading.Thread(target=_pipe_reader, args=(proc.stderr, q, "err"), daemon=True)
    t_out.start()
    t_err.start()

    stdout_lines       = []
    stderr_lines       = []
    done_flags         = set()
    last_activity_time = time.time()
    codex_started      = False
    line_count         = 0
    stderr_count       = 0
    captured_sid       = session_id   # keep existing or capture new
    header_separator_count = 0        # track "--------" lines to detect end of startup header

    # ── Internet monitor (background thread during silence) ──────────────
    inet_down      = threading.Event()
    _monitor_alive = [True]

    def _inet_monitor():
        time.sleep(INTERNET_CHECK_INTERVAL)   # wait before first check
        while _monitor_alive[0]:
            if not _check_internet():
                inet_down.set()
            else:
                inet_down.clear()
            time.sleep(INTERNET_CHECK_INTERVAL)

    inet_thread = threading.Thread(target=_inet_monitor, daemon=True)
    inet_thread.start()

    # ── Cbreak mode for keypress detection (Unix) ────────────────────────
    old_term = _enter_cbreak()
    interactive = sys.stdin.isatty()

    try:
        while len(done_flags) < 2:
            try:
                tag, line = q.get(timeout=1.0)
            except queue.Empty:
                silent_secs  = int(time.time() - last_activity_time)
                elapsed_secs = int(time.time() - start_time)

                # ── Keypress detection (pause) ──
                if interactive and pause_event is not None and _kbhit():
                    ch = _consume_key()
                    if ch == "p":
                        ui.status("Pause requested — finishing current codex call first...",
                                  C_YELLOW)
                        pause_event.set()

                # ── Internet check (only during silence, not before startup) ──
                if codex_started and inet_down.is_set() and silent_secs >= 60:
                    _kill_proc(proc)
                    duration = time.time() - start_time
                    msg = f"Network lost: zero activity {silent_secs}s + internet down"
                    logger.log_master(agent_name.upper(), msg)
                    return "".join(stdout_lines), msg, -3, duration, captured_sid

                # ── Startup timeout ──
                if not codex_started and silent_secs >= _startup_timeout:
                    _kill_proc(proc)
                    duration = time.time() - start_time
                    msg = f"Codex did not start within {_startup_timeout}s"
                    logger.log_master(agent_name.upper(), msg)
                    return "", msg, -1, duration, captured_sid

                # ── Silence timeout ──
                if codex_started and silent_secs >= _silence_timeout:
                    _kill_proc(proc)
                    duration = time.time() - start_time
                    msg = f"Timed out: zero activity for {silent_secs}s"
                    logger.log_master(agent_name.upper(), msg)
                    return "".join(stdout_lines), msg, -1, duration, captured_sid

                # ── Status line ──
                if silent_secs > 0 and silent_secs % 5 == 0:
                    pause_hint = "  [P=pause]" if interactive and pause_event is not None else ""
                    if not codex_started:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} Waiting for Codex to start... "
                            f"{C_DIM}({silent_secs}s){C_RESET}{pause_hint}    ")
                    else:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} {agent_name} working... "
                            f"{C_DIM}({elapsed_secs}s elapsed, silent {silent_secs}s)"
                            f"{C_RESET}{pause_hint}    ")
                    sys.stdout.flush()
                continue

            if tag == "out_done":
                done_flags.add("out")
                continue
            if tag == "err_done":
                done_flags.add("err")
                continue

            # Any output on either pipe = activity
            last_activity_time = time.time()
            codex_started      = True
            inet_down.clear()   # if codex is talking, internet is fine
            ui.clear_line()

            if tag == "out":
                stdout_lines.append(line)
                line_count += 1
                if line_count <= 30 or line_count % 10 == 0:
                    ui.stream_line(agent_name, line)
                elif line_count == 31:
                    _print_safe(f"  {C_DIM}  ... streaming (showing every 10th line) ...{C_RESET}")
            else:
                stderr_lines.append(line)
                stderr_count += 1
                # Track header boundaries (two "--------" separator lines)
                if header_separator_count < 2 and line.strip().startswith("--------"):
                    header_separator_count += 1
                # Capture session id ONLY from within the startup header
                if header_separator_count == 1 and "session id:" in line.lower():
                    sid = _parse_session_id(line)
                    if sid:
                        captured_sid = sid
                stripped = line.strip()
                if stripped and stderr_count <= 10:
                    _print_safe(f"  {C_DIM}  [codex] {stripped[:80]}{C_RESET}")
                elif stderr_count == 11:
                    _print_safe(f"  {C_DIM}  [codex] ... (suppressing further stderr){C_RESET}")

    except KeyboardInterrupt:
        ui.error("Interrupted! Killing Codex process...")
        _kill_proc(proc)
        duration = time.time() - start_time
        logger.log_master(agent_name.upper(), f"Killed by user interrupt after {duration:.1f}s")
        return "".join(stdout_lines), "Interrupted by user", -2, duration, captured_sid

    finally:
        _monitor_alive[0] = False
        _exit_cbreak(old_term)

    proc.wait()
    duration = time.time() - start_time

    if line_count > 30:
        _print_safe(f"  {C_DIM}  ... {line_count} total lines received{C_RESET}")

    return "".join(stdout_lines), "".join(stderr_lines), proc.returncode, duration, captured_sid


def _kill_proc(proc: subprocess.Popen):
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ============================================================================
# VERDICT PARSING
# ============================================================================

def parse_verdict(inspector_output: str) -> str:
    tail  = inspector_output[-500:] if len(inspector_output) > 500 else inspector_output
    match = re.search(r'VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)', tail, re.IGNORECASE)
    return match.group(1).upper() if match else "NEEDS_REVISION"


# ============================================================================
# PROMPT SIZE GUARD
# ============================================================================

def _truncate_for_prompt(text: str, max_chars: int = MAX_PROMPT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n[... TRUNCATED — {len(text) - max_chars} chars removed to fit context limit ...]\n\n"
        + text[-half:]
    )


# ============================================================================
# MAIN ENGINE
# ============================================================================

class Congress:
    """Main engine that orchestrates the Researcher-Inspector loop."""

    def __init__(self, max_rounds: int = MAX_ROUNDS, codex_bin: str | None = None,
                 codex_bin_resolved: str | None = None, working_dir: str | None = None):
        self.max_rounds  = max_rounds
        self.codex_bin   = codex_bin_resolved or _resolve_codex_binary(codex_bin)
        self.working_dir = working_dir or os.getcwd()
        self.ui          = TerminalUI()
        self.session_id  = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.logger      = CongressLogger(self.session_id)

        self.round_history: list[dict] = []
        self.source_files: list[str]   = []
        self.output_files: list[str]   = []
        self._interactive              = sys.stdin.isatty()
        self._interrupt_requested      = False
        self.researcher_session_id: str | None = None
        self.inspector_session_id:  str | None = None

        if not self.codex_bin:
            self.ui.error("Codex CLI not found! Install it or pass --codex-bin=<path>")
            sys.exit(1)

    # ──────────────────────────────────────────────────────────────────────
    # State management
    # ──────────────────────────────────────────────────────────────────────

    def _save_state_now(self, user_query: str, current_round: int, substep: str):
        """Persist current progress to congress_state.json."""
        _save_state_file(self.working_dir, {
            "state_version":   STATE_VERSION,
            "session_id":      self.session_id,
            "user_query":      user_query,
            "output_files":    self.output_files,
            "source_files":    self.source_files,
            "max_rounds":      self.max_rounds,
            "current_round":   current_round,
            "current_substep": substep,
            "status":          "running",
            "researcher_session_id": self.researcher_session_id,
            "inspector_session_id":  self.inspector_session_id,
        })

    # ──────────────────────────────────────────────────────────────────────
    # Prompt building
    # ──────────────────────────────────────────────────────────────────────

    def _source_file_section(self) -> str:
        if not self.source_files:
            return ""
        file_list = "\n".join(f"  - {f}" for f in self.source_files)
        return (
            f"\n{'=' * 60}\n"
            f"REQUIRED: READ THESE SOURCE FILES FIRST\n"
            f"{'=' * 60}\n"
            f"Before doing anything else, read these files from disk.\n"
            f"They contain the original requirements and reference material:\n"
            f"{file_list}\n"
        )

    def _output_file_section(self) -> str:
        if not self.output_files:
            return ""
        file_list = "\n".join(f"  - {path}" for path in self.output_files)
        return (
            f"\n{'=' * 60}\n"
            f"REQUIRED OUTPUT FILES - THESE ARE THE REAL DELIVERABLES\n"
            f"{'=' * 60}\n"
            f"Your working directory is: {self.working_dir}\n"
            f"Read every requested output file completely before making changes.\n"
            f"If a requested output file does not exist yet, create it.\n"
            f"Paths below are relative to the working directory:\n"
            f"{file_list}\n"
        )

    def _session_request_section(self) -> str:
        return (
            f"\n{'=' * 60}\n"
            f"SESSION REQUEST FILE\n"
            f"{'=' * 60}\n"
            f"Read this file every round before doing work:\n"
            f"  {os.path.join(self.working_dir, SESSION_REQUEST_FILE)}\n"
        )

    def _build_researcher_prompt(self, user_query: str, round_num: int) -> str:
        source_section = self._source_file_section()
        output_section = self._output_file_section()
        session_request_section = self._session_request_section()
        res_doc_path = os.path.join(self.working_dir, RESEARCHER_UPDATED_FILE)
        ins_doc_path = os.path.join(self.working_dir, INSPECTOR_COMMENTS_FILE)
        has_res_doc  = round_num > 1 and os.path.exists(res_doc_path)
        has_ins_doc  = round_num > 1 and os.path.exists(ins_doc_path)

        # Round 2+: point agent at living documents on disk
        living_doc_lines = []
        if has_res_doc:
            living_doc_lines.append(
                f"  Your previous notes/process report (latest): {res_doc_path}")
        if has_ins_doc:
            living_doc_lines.append(
                f"  Inspector feedback/comments (latest):      {ins_doc_path}")

        if living_doc_lines:
            living_docs_section = (
                f"\n{'=' * 60}\n"
                f"LIVING DOCUMENTS — READ THESE COMPLETELY BEFORE STARTING:\n"
                f"{'=' * 60}\n"
                + "\n".join(living_doc_lines) + "\n"
            )
        else:
            living_docs_section = ""

        steps = []
        steps.append(f"Read the managed session request file: {os.path.join(self.working_dir, SESSION_REQUEST_FILE)}")
        steps.append("Re-read the ORIGINAL USER REQUEST included inline below. It remains the source of truth every round.")
        steps.append(
            "Read every source/reference file listed above from disk before deciding what to change."
        )
        steps.append(
            "Read every requested output file listed above from disk. If any requested output file is missing, create it."
        )
        if round_num > 1 and has_res_doc:
            steps.append(f"Read your previous researcher notes from: {res_doc_path}")
        if round_num > 1 and has_ins_doc:
            steps.append(f"Read the latest inspector review from: {ins_doc_path}")
        steps.append(
            "Create or update ALL requested output files on disk so they satisfy the user's request as completely as possible.\n"
            "   - If a requested output file already exists, improve it instead of ignoring it.\n"
            "   - If you disagree with a previous inspector point, explain that clearly in your notes.\n"
            "   - The requested output files on disk are the actual deliverables."
        )
        steps.append(
            f"Output a COMPLETE researcher notes report to stdout.\n"
            f"   - Explain what changed in each requested output file.\n"
            f"   - Explain your reasoning, trade-offs, assumptions, and unresolved concerns.\n"
            f"   - Congress will automatically save your stdout as {RESEARCHER_UPDATED_FILE} v{round_num}."
        )
        steps.append("DO NOT wrap your entire response in a markdown code block.")

        task_section = (
            f"\n{'=' * 60}\n"
            f"YOUR TASK FOR ROUND {round_num}:\n"
            f"{'=' * 60}\n"
        )
        for i, step in enumerate(steps, 1):
            task_section += f"{i}. {step}\n"

        return (
            f"{RESEARCHER_SYSTEM_PROMPT}\n"
            f"{source_section}\n"
            f"{output_section}"
            f"{session_request_section}"
            f"{'=' * 60}\n"
            f"ORIGINAL USER REQUEST - RE-READ THIS EVERY ROUND:\n"
            f"{'=' * 60}\n"
            f"{user_query}\n"
            f"{living_docs_section}"
            f"{task_section}"
        )

    def _build_inspector_prompt(self, user_query: str, researcher_output: str,
                                round_num: int) -> str:
        source_section = self._source_file_section()
        output_section = self._output_file_section()
        session_request_section = self._session_request_section()

        res_doc_path = os.path.join(self.working_dir, RESEARCHER_UPDATED_FILE)
        ins_doc_path = os.path.join(self.working_dir, INSPECTOR_COMMENTS_FILE)
        has_res_doc  = os.path.exists(res_doc_path)
        has_ins_doc  = round_num > 1 and os.path.exists(ins_doc_path)

        # Build previous_review_context for the template placeholder
        if has_ins_doc:
            previous_review_context = (
                f"YOUR PREVIOUS REVIEW — read before starting to avoid "
                f"re-flagging already-fixed issues:\n"
                f"  File: {ins_doc_path}\n"
            )
        else:
            previous_review_context = ""

        prompt_template = INSPECTOR_SYSTEM_PROMPT_TEMPLATE.format(
            previous_review_context=previous_review_context
        )

        living_docs_section = (
            f"\n{'=' * 60}\n"
            f"LIVING DOCUMENTS — READ THESE COMPLETELY BEFORE STARTING:\n"
            f"{'=' * 60}\n"
        )
        if has_res_doc:
            living_docs_section += (
                f"  Researcher's latest explanation/process notes: {res_doc_path}\n"
            )
        else:
            living_docs_section += (
                f"  [WARNING] {RESEARCHER_UPDATED_FILE} not found on disk. "
                f"Using inline copy below.\n"
            )
        if has_ins_doc:
            living_docs_section += (
                f"  Your previous review comments: {ins_doc_path}\n"
            )

        _verdict_reminder = (
            "IMPORTANT: At the VERY END of your output, on its own line, write your verdict:\n"
            "   VERDICT: NEEDS_REVISION   (if any CRITICAL or HIGH issues remain)\n"
            "   VERDICT: APPROVED         (only if the work is solid)"
        )

        steps = []
        steps.append(f"Read the managed session request file: {os.path.join(self.working_dir, SESSION_REQUEST_FILE)}")
        steps.append("Re-read the ORIGINAL USER REQUEST included inline below. It remains the source of truth every round.")
        steps.append("Read every source/reference file listed above from disk.")
        steps.append(
            "Read every requested output file listed above from disk and review those deliverables first. "
            "If any requested output file is missing, treat that as an issue."
        )
        if has_res_doc:
            steps.append(f"Read the Researcher's latest notes from: {res_doc_path}")
        else:
            steps.append("Use the inline fallback copy of the Researcher's notes below.")
        if has_ins_doc:
            steps.append(f"Read your previous review comments from: {ins_doc_path}")
        steps.append(
            f"Produce a COMPLETE detailed review of the requested output files.\n"
            f"   - Judge the output files against the original request first.\n"
            f"   - Use researcher_updated.md only as explanation context.\n"
            f"   - Mark previous issues as RESOLVED or STILL PRESENT when applicable.\n"
            f"   - Congress will automatically save your stdout as {INSPECTOR_COMMENTS_FILE} v{round_num}."
        )

        steps.append(
            "Output your COMPLETE review to stdout. "
            "DO NOT write any files yourself — stdout is your review document."
        )
        steps.append(_verdict_reminder)

        task_section = (
            f"\n{'=' * 60}\n"
            f"YOUR TASK FOR ROUND {round_num}:\n"
            f"{'=' * 60}\n"
        )
        for i, step in enumerate(steps, 1):
            task_section += f"{i}. {step}\n"

        # Inline fallback if living doc is missing (robustness)
        inline_fallback = ""
        if not has_res_doc:
            res_inline = _truncate_for_prompt(researcher_output, 10000)
            inline_fallback = (
                f"\n{'=' * 60}\n"
                f"RESEARCHER'S OUTPUT — INLINE FALLBACK (file missing):\n"
                f"{'=' * 60}\n"
                f"{res_inline}\n"
            )

        return (
            f"{prompt_template}\n"
            f"{source_section}\n"
            f"{output_section}"
            f"{session_request_section}"
            f"{'=' * 60}\n"
            f"ORIGINAL USER REQUEST - RE-READ THIS EVERY ROUND:\n"
            f"{'=' * 60}\n"
            f"{user_query}\n"
            f"{living_docs_section}"
            f"{task_section}"
            f"{inline_fallback}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Interactive pause / wait helpers
    # ──────────────────────────────────────────────────────────────────────

    def _do_interactive_pause(self) -> bool:
        """Enter pause mode. Returns True to resume, False to quit."""
        _flush_input()
        self.ui.status("PAUSED — Press [R] to resume, [Q] to quit and save state.", C_YELLOW)
        self.logger.log_master("SYSTEM", "Paused by user (keyboard)")

        old_term   = _enter_cbreak()
        wait_count = 0
        try:
            while True:
                if _kbhit():
                    ch = _consume_key()
                    if ch == "r":
                        self.ui.status("Resumed!", C_GREEN)
                        self.logger.log_master("SYSTEM", "Resumed by user")
                        return True
                    elif ch == "q":
                        self._interrupt_requested = True
                        return False
                time.sleep(0.2)
                wait_count += 1
                if wait_count % 75 == 0:
                    elapsed = wait_count * 0.2
                    self.ui.status(
                        f"Still paused ({elapsed:.0f}s)... [R] resume  [Q] quit", C_DIM)
        finally:
            _exit_cbreak(old_term)

    def _wait_for_internet(self) -> bool:
        """
        Block until internet is restored. Checks every 5 seconds.
        Returns True when restored, False if user presses Q.
        [Q] is checked every 0.2s so it is always responsive.
        """
        _flush_input()
        self.ui.status("Internet connection lost. Waiting for reconnect...", C_RED)
        self.ui.status("Press [Q] to quit and save state.", C_DIM)
        self.logger.log_master("SYSTEM", "Waiting for internet reconnect")

        old_term    = _enter_cbreak()
        total_secs  = 0
        try:
            while True:
                # ── Poll Q key every 0.2s for 5 seconds before each inet check ──
                for _ in range(25):   # 25 × 0.2s = 5s
                    if _kbhit():
                        ch = _consume_key()
                        if ch == "q":
                            self._interrupt_requested = True
                            return False
                    time.sleep(0.2)

                total_secs += 5

                if _check_internet():
                    self.ui.status("Internet restored! Retrying...", C_GREEN)
                    self.logger.log_master("SYSTEM", "Internet reconnected")
                    return True

                if total_secs % 30 == 0:
                    self.ui.status(
                        f"Still waiting for internet ({total_secs}s)... [Q] to quit", C_DIM)
        finally:
            _exit_cbreak(old_term)

    def _wait_for_rate_limit(self, retry_info: str) -> str:
        """
        Pause after a rate limit hit. Waits for user to press R (retry) or Q (quit),
        and auto-retries once per hour while waiting.
        Returns: "manual_retry", "auto_retry", or "quit".
        """
        _flush_input()
        msg = f"API rate/usage limit hit.{retry_info}"
        auto_minutes = max(1, RATE_LIMIT_AUTO_RETRY_SECONDS // 60)
        self.ui.error(msg)
        self.ui.status(
            f"Press [R] to retry now. [Q] to quit and save state. "
            f"Auto-retry in {auto_minutes}m.",
            C_YELLOW)
        self.logger.log_master("SYSTEM", f"Rate limit pause: {msg}")

        old_term           = _enter_cbreak()
        start              = time.monotonic()
        deadline           = start + RATE_LIMIT_AUTO_RETRY_SECONDS
        last_status_bucket = -1
        try:
            while True:
                if _kbhit():
                    ch = _consume_key()
                    waited = int(time.monotonic() - start)
                    if ch == "r":
                        self.ui.status("Retrying...", C_GREEN)
                        self.logger.log_master(
                            "SYSTEM",
                            f"Rate limit retry requested by user after {waited}s")
                        return "manual_retry"
                    elif ch == "q":
                        self._interrupt_requested = True
                        self.logger.log_master(
                            "SYSTEM",
                            f"Rate limit quit requested after {waited}s")
                        return "quit"

                now = time.monotonic()
                if now >= deadline:
                    waited = int(now - start)
                    self.ui.status("Auto-retrying after rate-limit wait...", C_GREEN)
                    self.logger.log_master(
                        "SYSTEM",
                        f"Rate limit auto-retry after {waited}s")
                    return "auto_retry"

                time.sleep(0.2)
                waited = int(time.monotonic() - start)
                remaining = max(0, int(deadline - time.monotonic()))
                status_bucket = waited // 30
                if waited >= 30 and status_bucket != last_status_bucket:
                    last_status_bucket = status_bucket
                    self.ui.status(
                        f"Still waiting ({waited}s)... auto-retry in "
                        f"{remaining}s [R] retry  [Q] quit", C_DIM)
        finally:
            _exit_cbreak(old_term)

    # ──────────────────────────────────────────────────────────────────────
    # Recovery wrapper
    # ──────────────────────────────────────────────────────────────────────

    def _run_with_recovery(self, agent: str, prompt: str, round_label,
                           pause_event: threading.Event,
                           use_session: bool = True,
                           ) -> tuple[str, str, int, float, str | None]:
        """
        Run codex with recovery.
        Returns (stdout, stderr, rc, total_duration, session_id).

        If use_session=True, looks up and updates {agent}_session_id in self state.
        If use_session=False, always starts a new session (used by _finalize_output).

        Priority order per attempt:
          rc==-2  → user interrupt (no retry)
          rc==-3 or network error → wait for internet, retry
          context limit with partial output → resume session with "continue"
          rate limit (check regardless of output)
            → has partial output: accept as success (rc→0)
            → no output: wait for user [R/Q], retry
          other failure / success → return as-is
        """
        stdout, stderr, rc, duration = "", "", -1, 0.0
        total_duration = 0.0
        attempt = 0
        network_retries = 0

        # Session tracking
        sid_key = f"{agent}_session_id"
        current_sid = getattr(self, sid_key, None) if use_session else None

        while True:
            if self._interrupt_requested:
                return "", "interrupted", -2, total_duration, current_sid

            current_prompt = prompt
            if attempt > 0:
                if current_sid:
                    # Resuming existing session — agent has context, just say continue
                    current_prompt = (
                        "Your previous task was interrupted by a network or API error. "
                        "Continue where you left off. Do NOT redo completed work.\n\n"
                        f"Original task:\n{prompt}"
                    )
                else:
                    # No session to resume — full prompt with retry note
                    note = (
                        f"[SYSTEM NOTE: Retry attempt {attempt}. "
                        f"A previous attempt was interrupted by a network or API error. "
                        f"Please complete the task from the beginning.]\n\n"
                    )
                    current_prompt = note + prompt

            stdout, stderr, rc, duration, new_sid = run_codex(
                current_prompt, self.codex_bin, agent, self.ui, self.logger,
                round_label, self.working_dir,
                session_id=current_sid if use_session else None,
                pause_event=pause_event,
            )
            total_duration += duration

            # Update session ID
            if new_sid:
                current_sid = new_sid
                if use_session:
                    setattr(self, sid_key, new_sid)

            # ── Check pause event BEFORE evaluating rc ──
            if pause_event.is_set():
                pause_event.clear()
                if not self._do_interactive_pause():
                    # User chose to quit during pause
                    return stdout, "paused-quit", -2, total_duration, current_sid

            # ── Priority 1: User interrupt ──
            if rc == -2:
                return stdout, stderr, rc, total_duration, current_sid

            # ── Priority 2: Network error ──
            if rc == -3 or _is_network_error(stderr, rc):
                self.logger.log_master("SYSTEM",
                    f"Network error on attempt {attempt + 1}: {stderr[:100]}")
                if network_retries < MAX_RECOVERY_RETRIES:
                    if not self._wait_for_internet():
                        return stdout, stderr, -2, total_duration, current_sid
                    network_retries += 1
                    attempt += 1
                    continue   # retry
                # Max retries exhausted
                return stdout, stderr, rc, total_duration, current_sid

            # ── Priority 3: Context window exhaustion — resume session ──
            if _is_context_limit(stderr):
                if stdout.strip() and current_sid:
                    # Resume the same session with "continue" to get the rest
                    accumulated = stdout
                    self.ui.status(
                        f"Context limit hit — {len(accumulated)} chars captured, "
                        f"resuming session to get remaining output...",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Context limit hit with {len(accumulated)} chars — "
                        f"starting continuation loop (session {current_sid})")

                    for cont_num in range(1, MAX_CONTINUATIONS + 1):
                        self.ui.status(
                            f"Continuation {cont_num}/{MAX_CONTINUATIONS} — "
                            f"sending 'continue' to session {current_sid[:8]}...",
                            C_CYAN)

                        cont_out, cont_err, cont_rc, cont_dur, cont_sid = run_codex(
                            "continue",
                            self.codex_bin, agent, self.ui, self.logger,
                            round_label, self.working_dir,
                            session_id=current_sid,
                            pause_event=pause_event,
                        )
                        total_duration += cont_dur
                        if cont_sid:
                            current_sid = cont_sid
                            if use_session:
                                setattr(self, sid_key, cont_sid)

                        accumulated += cont_out

                        # Check for user interrupt mid-continuation
                        if cont_rc == -2:
                            return accumulated, cont_err, -2, total_duration, current_sid

                        # Check for network error mid-continuation
                        if cont_rc == -3 or _is_network_error(cont_err, cont_rc):
                            self.ui.status(
                                f"Network error during continuation — "
                                f"{len(accumulated)} chars so far, accepting partial.",
                                C_YELLOW)
                            self.logger.log_master("SYSTEM",
                                f"Network error mid-continuation, accepting {len(accumulated)} chars")
                            return accumulated, cont_err, 0, total_duration, current_sid

                        # Check for rate limit mid-continuation
                        is_limited, _ = _is_rate_limited(cont_err, cont_rc)
                        if is_limited:
                            self.ui.status(
                                f"Rate limit during continuation — "
                                f"{len(accumulated)} chars so far, accepting partial.",
                                C_YELLOW)
                            self.logger.log_master("SYSTEM",
                                f"Rate limit mid-continuation, accepting {len(accumulated)} chars")
                            return accumulated, cont_err, 0, total_duration, current_sid

                        # If NOT context limit again → agent finished cleanly
                        if not _is_context_limit(cont_err):
                            self.ui.status(
                                f"Continuation complete — total {len(accumulated)} chars "
                                f"({cont_num} continuation(s)).",
                                C_GREEN)
                            self.logger.log_master("SYSTEM",
                                f"Continuation finished cleanly after {cont_num} resume(s), "
                                f"total {len(accumulated)} chars")
                            return accumulated, cont_err, cont_rc, total_duration, current_sid

                        # Still hitting context limit — check if any new output
                        if not cont_out.strip():
                            self.ui.status(
                                f"Continuation produced no new output — "
                                f"accepting {len(accumulated)} chars.",
                                C_YELLOW)
                            break

                        self.ui.status(
                            f"Got {len(cont_out)} more chars "
                            f"(total: {len(accumulated)}), continuing...",
                            C_YELLOW)

                    # Exhausted MAX_CONTINUATIONS — accept what we have
                    self.ui.status(
                        f"Max continuations reached — accepting {len(accumulated)} chars.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Max continuations ({MAX_CONTINUATIONS}) exhausted, "
                        f"accepting {len(accumulated)} chars")
                    return accumulated, stderr, 0, total_duration, current_sid

                elif stdout.strip():
                    # No session ID available — fall back to accepting partial
                    self.ui.status(
                        f"Context window exhausted — {len(stdout)} chars captured "
                        f"(no session to resume). Accepting partial output.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Context limit, no session ID, accepting {len(stdout)} chars")
                    return stdout, stderr, 0, total_duration, current_sid

                else:
                    # Hit context limit before producing any output — prompt too large
                    self.ui.error(
                        "Context window exceeded with no output. "
                        "The prompt may be too large for the model.")
                    self.logger.log_master("SYSTEM",
                        f"Context limit hit, no output (rc={rc}): {stderr[:200]}")
                    return stdout, stderr, rc, total_duration, current_sid

            # ── Priority 4: Rate limit — check ALWAYS, even with partial output ──
            is_limited, retry_info = _is_rate_limited(stderr, rc)
            if is_limited:
                if stdout.strip():
                    # Partial output captured before limit hit — accept it
                    self.ui.status(
                        f"Rate limit hit but {len(stdout)} chars captured — using partial output.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Rate limit with partial output ({len(stdout)} chars) — accepted")
                    return stdout, stderr, 0, total_duration, current_sid
                # No output — must pause and retry
                wait_started = time.monotonic()
                wait_action = self._wait_for_rate_limit(retry_info)
                total_duration += time.monotonic() - wait_started
                if wait_action == "quit":
                    return "", stderr, -2, total_duration, current_sid
                attempt += 1
                continue   # retry

            # ── No error condition matched: return (success or other failure) ──
            return stdout, stderr, rc, total_duration, current_sid

        # Should never reach here (loop always returns inside), but safety fallback
        return stdout, stderr, rc, total_duration, current_sid

    # ──────────────────────────────────────────────────────────────────────
    # Transition menu (pause / resume / output / continue)
    # ──────────────────────────────────────────────────────────────────────

    def _transition_menu(self, next_agent: str, round_num: int) -> str:
        """Show transition menu between agents.
        Returns: 'continue', 'output', or 'quit'.
        Skipped entirely in non-interactive mode.
        """
        if not self._interactive:
            return "continue"

        _flush_input()

        print()
        print(f"  {C_CYAN}{'─' * 50}{C_RESET}")
        print(f"  {C_CYAN}{C_BOLD}  What next?{C_RESET}")
        print(f"  {C_GREEN}  [C]{C_RESET} Continue to {next_agent}  "
              f"{C_DIM}(auto in {AUTO_CONTINUE_SECS}s){C_RESET}")
        print(f"  {C_YELLOW}  [P]{C_RESET} Pause (wait for your command)")
        print(f"  {C_MAGENTA}  [O]{C_RESET} Output now (finalize current result)")
        print(f"  {C_RED}  [Q]{C_RESET} Quit session")
        print(f"  {C_CYAN}{'─' * 50}{C_RESET}")

        old_settings = _enter_cbreak()
        try:
            result = self._menu_countdown()
            if result == "pause":
                result = self._menu_pause_loop()
            return result
        except KeyboardInterrupt:
            self.ui.status("Interrupted.", C_RED)
            self.logger.log_master("SYSTEM", "Interrupted during transition menu")
            return "quit"
        finally:
            _exit_cbreak(old_settings)

    def _menu_countdown(self) -> str:
        for remaining in range(AUTO_CONTINUE_SECS, 0, -1):
            sys.stdout.write(
                f"\r  {C_DIM}  Auto-continue in {remaining}s... "
                f"(press C/P/O/Q){C_RESET}    ")
            sys.stdout.flush()

            for _ in range(10):
                if _kbhit():
                    key = _consume_key()
                    sys.stdout.write("\r" + " " * 60 + "\r")
                    sys.stdout.flush()
                    if key == "c":
                        self.ui.status("Continuing...", C_GREEN)
                        return "continue"
                    elif key == "p":
                        return "pause"
                    elif key == "o":
                        return "output"
                    elif key == "q":
                        return "quit"
                time.sleep(0.1)

        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
        self.ui.status("Auto-continuing...", C_DIM)
        return "continue"

    def _menu_pause_loop(self) -> str:
        self.ui.status("PAUSED. Press [R] Resume  [O] Output now  [Q] Quit", C_YELLOW)
        self.logger.log_master("SYSTEM", "Paused by user")

        wait_count = 0
        while True:
            if _kbhit():
                key = _consume_key()
                if key == "r":
                    self.ui.status("Resumed!", C_GREEN)
                    self.logger.log_master("SYSTEM", "Resumed by user")
                    return "continue"
                elif key == "o":
                    self.ui.status("Outputting...", C_MAGENTA)
                    self.logger.log_master("SYSTEM", "User chose output during pause")
                    return "output"
                elif key == "q":
                    self.ui.status("Quitting...", C_RED)
                    self.logger.log_master("SYSTEM", "User chose quit during pause")
                    return "quit"
            time.sleep(0.2)
            wait_count += 1
            if wait_count % 75 == 0:
                elapsed = wait_count * 0.2
                self.ui.status(
                    f"Still paused ({elapsed:.0f}s)... "
                    f"[R] Resume  [O] Output  [Q] Quit", C_DIM)

    # ──────────────────────────────────────────────────────────────────────
    # Finalize output
    # ──────────────────────────────────────────────────────────────────────

    def _build_researcher_fallback_notes(self, round_num: int,
                                         before_status: list[dict],
                                         after_status: list[dict]) -> str:
        summary = _summarize_output_status(before_status, after_status)
        lines = [
            "UNDERSTANDING:",
            f"Congress synthesized this fallback note because the Researcher returned empty stdout in round {round_num}.",
            "",
            "OUTPUT FILE CHANGES:",
        ]

        for label, paths in (
            ("Created", summary["created"]),
            ("Updated", summary["updated"]),
            ("Unchanged", summary["unchanged"]),
            ("Missing", summary["missing"]),
            ("Invalid type", summary["invalid"]),
        ):
            if paths:
                lines.append(f"- {label}:")
                for path in paths:
                    lines.append(f"  - {path}")

        lines.extend([
            "",
            "RESEARCH / REASONING:",
            "The deliverable files on disk are treated as the authoritative outputs for this round.",
            "Inspector should review those files directly, and treat this note as a Congress-generated fallback only.",
            "",
            "Refrences or ASSUMPTIONS:",
            "- Congress generated this note because the Researcher did not provide stdout for this round.",
        ])
        return "\n".join(lines)

    def _build_session_summary(self, status: str) -> str:
        output_status = _collect_output_status(self.working_dir, self.output_files)
        lines = [
            f"SESSION STATUS: {status}",
            f"WORKING DIRECTORY: {self.working_dir}",
            "",
            "REQUESTED OUTPUT FILES:",
        ]
        if self.output_files:
            for item in output_status:
                if item.get("invalid_type"):
                    marker = "INVALID"
                    size_text = "exists but is not a file"
                else:
                    marker = "OK" if item["exists"] else "MISSING"
                    size_text = f"{item['size']} bytes" if item["size"] is not None else "no size"
                lines.append(f"- [{marker}] {item['path']} ({size_text})")
        else:
            lines.append("- (none)")

        lines.extend([
            "",
            "MANAGED CONGRESS FILES:",
            f"- {SESSION_REQUEST_FILE}",
            f"- {RESEARCHER_UPDATED_FILE}",
            f"- {INSPECTOR_COMMENTS_FILE}",
            "",
            f"LOGS: {self.logger.session_dir}",
        ])
        return "\n".join(lines)

    def _finalize_output(self, user_query: str, current_output: str) -> str:
        """Build a read-only session summary for output-file mode."""
        self.ui.status("Building read-only session summary...", C_CYAN)
        self.logger.log_master("SYSTEM", "Finalize output requested (summary only)")
        return self._build_session_summary("output_early")

    # ──────────────────────────────────────────────────────────────────────
    # Main debate loop
    # ──────────────────────────────────────────────────────────────────────

    def run(self, user_query: str, start_round: int = 1,
            initial_substep: str = "",
            source_files: list[str] | None = None,
            output_files: list[str] | None = None) -> str:
        """
        Run the full Researcher-Inspector loop. Returns a session summary string.

        start_round      : Resume from this round (1 = fresh start).
        initial_substep  : "researcher_done" -> skip researcher on start_round.
        source_files     : Provided on resume (from saved state); auto-detected if None.
        output_files     : Required deliverable files for the session.
        """
        self.logger.log_user_query(user_query)

        if output_files is not None:
            self.output_files = _normalize_output_file_list(list(output_files), self.working_dir)
        elif self.output_files:
            self.output_files = _normalize_output_file_list(self.output_files, self.working_dir)
        if not self.output_files:
            raise ValueError("Congress requires at least one output file for this workflow.")

        self.logger.log_output_files(self.output_files)
        if not _write_session_request(self.working_dir, user_query, self.output_files):
            msg = (
                f"Could not write {SESSION_REQUEST_FILE}. "
                "Congress will not continue with a stale session request file."
            )
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._build_session_summary("failed")
            self.logger.log_session_end(final_output, 0, "failed")
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        if source_files:
            self.source_files = source_files
        else:
            self.source_files = _detect_source_files(
                self.working_dir,
                excluded_relpaths=self.output_files,
            )

        if self.source_files:
            self.ui.status(f"Source files detected ({len(self.source_files)}):", C_GREEN)
            for sf in self.source_files:
                self.ui.status(f"  {os.path.basename(sf)}", C_DIM)
        else:
            self.ui.status("No source files detected in working directory.", C_DIM)

        self.ui.status(f"Requested output files ({len(self.output_files)}):", C_GREEN)
        for output_file in self.output_files:
            self.ui.status(f"  {output_file}", C_DIM)

        self.ui.status(f"Session:    {self.session_id}")
        self.ui.status(f"Codex:      {self.codex_bin}")
        self.ui.status(f"Max rounds: {self.max_rounds}")
        self.ui.status(f"Working dir:{self.working_dir}")
        self.ui.status(f"Logs:       {self.logger.session_dir}")
        if start_round > 1:
            self.ui.status(
                f"RESUMING from round {start_round} (substep: {initial_substep or 'start'})",
                C_GREEN)
        if self._interactive:
            self.ui.status("Press [P] during codex execution to pause.", C_DIM)
        print()

        if start_round > 1:
            for r in range(1, start_round):
                rh = {"round": r}
                res = _read_round_output(self.working_dir, r, "researcher")
                ins = _read_round_output(self.working_dir, r, "inspector")
                if res:
                    rh["researcher_output"] = res
                if ins:
                    rh["inspector_output"] = ins
                if res or ins:
                    self.round_history.append(rh)

        self._save_state_now(user_query, start_round, initial_substep)

        pause_event = threading.Event()
        researcher_output = ""
        final_output = ""
        session_status = "completed"
        skip_researcher = (initial_substep == "researcher_done")

        for round_num in range(start_round, self.max_rounds + 1):
            round_data = {"round": round_num}

            if skip_researcher:
                loaded = _read_round_output(self.working_dir, round_num, "researcher")
                if loaded:
                    researcher_output = loaded
                    round_data["researcher_output"] = researcher_output
                    _write_living_doc(self.working_dir, RESEARCHER_UPDATED_FILE,
                                      researcher_output, round_num, "RESEARCHER NOTES")
                    round_data["output_status"] = _collect_output_status(
                        self.working_dir, self.output_files)
                    self.ui.status(
                        f"[Resume] Round {round_num}: researcher notes loaded from disk "
                        f"({len(researcher_output)} chars).", C_GREEN)
                    self.logger.log_master("SYSTEM",
                                           f"Resume: loaded researcher notes for round {round_num}")
                else:
                    self.ui.error(
                        f"Resume: expected round_{round_num}_researcher.md not found. "
                        f"Re-running researcher.")
                    skip_researcher = False

            if not skip_researcher:
                phase = "Initial Deliverable Draft" if round_num == 1 else "Updating Deliverables"
                self.ui.agent_header("RESEARCHER", round_num, self.max_rounds, phase)

                researcher_prompt = self._build_researcher_prompt(user_query, round_num)
                self.logger.log_agent_start("researcher", round_num, researcher_prompt)
                self._save_state_now(user_query, round_num, "researcher_running")

                before_status = _collect_output_status(self.working_dir, self.output_files)
                stdout, stderr, rc, duration, new_sid = self._run_with_recovery(
                    "researcher", researcher_prompt, round_num, pause_event)
                after_status = _collect_output_status(self.working_dir, self.output_files)
                _write_round_output_status(self.working_dir, round_num, before_status, after_status)

                self.logger.log_agent_output(
                    "researcher", round_num, stdout, stderr, rc, duration)

                if new_sid:
                    self.researcher_session_id = new_sid

                output_summary = _summarize_output_status(before_status, after_status)
                round_data["output_summary"] = output_summary
                self.logger.log_master(
                    "SYSTEM",
                    f"Round {round_num} outputs: created={output_summary['created']}, "
                    f"updated={output_summary['updated']}, missing={output_summary['missing']}, "
                    f"invalid={output_summary['invalid']}",
                )

                if rc == -2:
                    round_data["researcher_output"] = stdout.strip()
                    round_data["error"] = "interrupted"
                    self.round_history.append(round_data)
                    session_status = "interrupted"
                    break

                researcher_output = stdout.strip()
                if not researcher_output:
                    researcher_output = self._build_researcher_fallback_notes(
                        round_num, before_status, after_status)
                    self.logger.log_master(
                        "SYSTEM",
                        f"Researcher stdout empty in round {round_num}; generated fallback notes.",
                    )

                _write_round_output(self.working_dir, round_num, "researcher", researcher_output)
                _write_living_doc(self.working_dir, RESEARCHER_UPDATED_FILE,
                                  researcher_output, round_num, "RESEARCHER NOTES")

                invalid_outputs = [item["path"] for item in after_status if item.get("invalid_type")]
                missing_outputs = [
                    item["path"] for item in after_status
                    if not item["exists"] and not item.get("invalid_type")
                ]
                round_data["researcher_output"] = researcher_output
                round_data["output_status"] = after_status
                if missing_outputs or invalid_outputs:
                    problem_parts = []
                    if missing_outputs:
                        problem_parts.append("missing: " + ", ".join(missing_outputs))
                    if invalid_outputs:
                        problem_parts.append("not regular files: " + ", ".join(invalid_outputs))
                    self.ui.error("Researcher output file issues: " + "; ".join(problem_parts))
                    self.logger.log_master(
                        "SYSTEM",
                        f"Invalid requested output state after round {round_num}: "
                        f"missing={missing_outputs}, invalid={invalid_outputs}",
                    )
                    round_data["error"] = "; ".join(problem_parts)
                    self.round_history.append(round_data)
                    session_status = "failed"
                    break

                self._save_state_now(user_query, round_num, "researcher_done")
                self.ui.status(
                    f"Researcher finished ({duration:.0f}s, {len(researcher_output)} chars of notes)",
                    C_BLUE)

            skip_researcher = False

            action = self._transition_menu("Inspector", round_num)
            if action == "output":
                self.round_history.append(round_data)
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = "output_early"
                _delete_state(self.working_dir)
                break
            if action == "quit":
                self.round_history.append(round_data)
                final_output = self._build_session_summary("quit_by_user")
                session_status = "quit_by_user"
                break

            time.sleep(COOLDOWN_BETWEEN)

            self.ui.agent_header("INSPECTOR", round_num, self.max_rounds,
                                 "Reviewing Deliverables")

            inspector_prompt = self._build_inspector_prompt(
                user_query, researcher_output, round_num)
            self.logger.log_agent_start("inspector", round_num, inspector_prompt)
            self._save_state_now(user_query, round_num, "inspector_running")

            stdout, stderr, rc, duration, new_sid = self._run_with_recovery(
                "inspector", inspector_prompt, round_num, pause_event)

            self.logger.log_agent_output(
                "inspector", round_num, stdout, stderr, rc, duration)

            if new_sid:
                self.inspector_session_id = new_sid

            if rc == -2:
                self._save_state_now(user_query, round_num, "researcher_done")
                round_data["inspector_output"] = ""
                round_data["error"] = "interrupted"
                self.round_history.append(round_data)
                session_status = "interrupted"
                break

            inspector_output = stdout.strip()
            if not inspector_output:
                self.ui.error(
                    f"Inspector produced no output after {MAX_RECOVERY_RETRIES} retries "
                    f"(rc={rc}). Keeping current deliverables and saved state for resume.")
                self.logger.log_master("SYSTEM",
                                       f"Inspector failed, no output, rc={rc}: {stderr[:200]}")
                self._save_state_now(user_query, round_num, "researcher_done")
                final_output = self._build_session_summary("inspector_failed")
                self.logger.log_round_summary(round_num, "INSPECTOR_FAILED")
                round_data["inspector_output"] = ""
                self.round_history.append(round_data)
                session_status = "inspector_failed"
                break

            _write_round_output(self.working_dir, round_num, "inspector", inspector_output)
            _write_living_doc(self.working_dir, INSPECTOR_COMMENTS_FILE,
                              inspector_output, round_num, "INSPECTOR REVIEW")
            round_data["inspector_output"] = inspector_output
            self.round_history.append(round_data)

            self._save_state_now(user_query, round_num + 1, "")

            self.ui.status(
                f"Inspector finished ({duration:.0f}s, {len(inspector_output)} chars)",
                C_MAGENTA)

            verdict = parse_verdict(inspector_output)
            self.ui.verdict_display(verdict, round_num, self.max_rounds)
            self.logger.log_round_summary(round_num, verdict)

            if verdict == "APPROVED":
                final_output_status = _collect_output_status(self.working_dir, self.output_files)
                invalid_or_missing = [
                    item["path"] for item in final_output_status
                    if item.get("invalid_type") or not item["exists"]
                ]
                if invalid_or_missing:
                    self.ui.error(
                        "Approval blocked because some requested output files are missing or invalid: "
                        + ", ".join(invalid_or_missing)
                    )
                    self.logger.log_master(
                        "SYSTEM",
                        f"Approval blocked due to invalid final outputs: {invalid_or_missing}",
                    )
                    session_status = "failed"
                    final_output = self._build_session_summary("failed")
                    break
                final_output = self._build_session_summary("completed")
                _delete_state(self.working_dir)
                self.ui.status(f"Inspector APPROVED after {round_num} round(s)!", C_GREEN)
                self.logger.log_session_end(final_output, round_num)
                self.ui.final_result(round_num, self.logger.session_dir)
                return final_output

            if round_num == self.max_rounds:
                self.ui.status(
                    f"Max rounds ({self.max_rounds}) reached. Keeping the latest deliverables on disk.",
                    C_YELLOW)
                final_output = self._build_session_summary("completed")
                break

            action = self._transition_menu("Researcher", round_num + 1)
            if action == "output":
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = "output_early"
                _delete_state(self.working_dir)
                break
            if action == "quit":
                final_output = self._build_session_summary("quit_by_user")
                session_status = "quit_by_user"
                break

            self.ui.status(f"Preparing round {round_num + 1}...", C_DIM)
            time.sleep(COOLDOWN_BETWEEN)

        if not final_output:
            final_output = self._build_session_summary(session_status)

        total = len(self.round_history) or 1
        self.logger.log_session_end(final_output, total, session_status)
        self.ui.final_result(total, self.logger.session_dir)

        if session_status in ("completed", "output_early"):
            _delete_state(self.working_dir)

        return final_output


# ============================================================================
# INTERACTIVE LOOP
# ============================================================================

def interactive_mode(max_rounds: int, codex_bin: str | None, working_dir: str):
    ui = TerminalUI()
    ui.clear()
    ui.banner()

    ui.box("How it works", (
        "1. You declare one or more required output files\n"
        "2. You type the request that defines what each output file should contain\n"
        "3. RESEARCHER updates the requested output files on disk\n"
        "4. Congress saves the Researcher's explanation to researcher_updated.md\n"
        "5. INSPECTOR reviews the requested output files plus the Researcher notes\n"
        "6. Loop repeats until APPROVED or max rounds reached\n"
        "\n"
        "During codex execution: press [P] to pause.\n"
        "Between agents: C=continue  P=pause  O=output  Q=quit.\n"
        "If interrupted, restart and choose [R] to resume.\n"
        "\n"
        "Commands at the task prompt:\n"
        "  'quit'       exit Congress\n"
        "  'logs'       open logs folder\n"
        "  'rounds N'   set max rounds (current: 3)"
    ))

    codex_path = _resolve_codex_binary(codex_bin)
    if not codex_path:
        ui.error("Codex CLI not found!")
        ui.error("Install: npm install -g @openai/codex")
        sys.exit(1)

    ui.status(f"Codex:      {codex_path}", C_GREEN)
    ui.status(f"Max rounds: {max_rounds}", C_GREEN)
    ui.status(f"Working dir:{working_dir}", C_GREEN)
    ui.status(f"Logs:       {LOG_DIR}", C_GREEN)
    print()

    while True:
        try:
            saved = _load_state(working_dir)

            if saved and saved.get("status") == "running":
                saved_version = saved.get("state_version", 1)
                saved_round   = saved.get("current_round", 1)
                saved_substep = saved.get("current_substep", "")
                saved_query   = saved.get("user_query", "")
                saved_max     = saved.get("max_rounds", max_rounds)
                saved_srcs    = saved.get("source_files", [])
                saved_outputs_raw = saved.get("output_files", [])
                saved_outputs = []
                saved_output_error = ""
                if saved_version >= STATE_VERSION and saved_outputs_raw:
                    try:
                        saved_outputs = _normalize_output_file_list(saved_outputs_raw, working_dir)
                    except ValueError as e:
                        saved_output_error = str(e)
                resume_allowed = saved_version >= STATE_VERSION and bool(saved_outputs) and not saved_output_error

                print(f"  {C_YELLOW}{C_BOLD}{'─' * 60}{C_RESET}")
                ui.status("Found a saved (interrupted) session:", C_YELLOW)
                ui.status(f"  Round:   {saved_round}", C_DIM)
                ui.status(f"  Substep: {saved_substep or 'start of round'}", C_DIM)
                if saved_outputs_raw:
                    ui.status(f"  Outputs: {', '.join(saved_outputs_raw)}", C_DIM)
                else:
                    ui.status("  Outputs: (missing from saved state)", C_DIM)
                ui.status(
                    f"  Query:   {saved_query[:80]}{'...' if len(saved_query) > 80 else ''}",
                    C_DIM,
                )
                if resume_allowed:
                    print(f"  {C_GREEN}[R]{C_RESET} Resume previous session")
                else:
                    ui.error(
                        "This saved session cannot be resumed safely because its output-file contract is missing or invalid."
                    )
                    if saved_output_error:
                        ui.status(f"  Reason: {saved_output_error}", C_DIM)
                print(f"  {C_CYAN}[N]{C_RESET} Start a new session (keeps saved files)")
                print(f"  {C_RED}[D]{C_RESET} Discard saved state and start fresh")
                print(f"  {C_YELLOW}{C_BOLD}{'─' * 60}{C_RESET}")

                _flush_input()
                old_term = _enter_cbreak()
                valid_choices = ("r", "n", "d") if resume_allowed else ("n", "d")
                choice = ""
                try:
                    while choice not in valid_choices:
                        if _kbhit():
                            choice = _consume_key()
                        time.sleep(0.05)
                finally:
                    _exit_cbreak(old_term)

                print()

                if choice == "r":
                    ui.status("Resuming previous session...", C_GREEN)
                    congress = Congress(
                        max_rounds=saved_max,
                        codex_bin_resolved=codex_path,
                        working_dir=working_dir,
                    )
                    congress.researcher_session_id = saved.get("researcher_session_id")
                    congress.inspector_session_id = saved.get("inspector_session_id")
                    final = congress.run(
                        saved_query,
                        start_round=saved_round,
                        initial_substep=saved_substep,
                        source_files=saved_srcs,
                        output_files=saved_outputs,
                    )
                    _display_final(ui, final)
                    continue

                if choice == "d":
                    _delete_state(working_dir)
                    ui.status("Previous session discarded. Starting fresh.", C_YELLOW)
                elif choice == "n":
                    _delete_state(working_dir)
                    ui.status("Starting new session (old round files preserved).", C_DIM)

            print(f"  {C_CYAN}{C_BOLD}{'─' * 60}{C_RESET}")
            raw_outputs = input(
                f"  {C_CYAN}{C_BOLD}> Output files (comma-separated):{C_RESET} "
            ).strip()
            print()

            if not raw_outputs:
                ui.error("At least one output file is required.")
                continue
            if raw_outputs.lower() in ("quit", "exit", "q"):
                ui.status("Goodbye!", C_GREEN)
                break

            try:
                output_files = _parse_output_files(raw_outputs, working_dir)
            except ValueError as e:
                ui.error(str(e))
                continue

            print(f"  {C_CYAN}{C_BOLD}{'─' * 60}{C_RESET}")
            user_input = input(f"  {C_CYAN}{C_BOLD}> Your question/task:{C_RESET} ").strip()
            print()

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                ui.status("Goodbye!", C_GREEN)
                break
            if user_input.lower() == "logs":
                _open_folder(LOG_DIR)
                continue
            if user_input.lower().startswith("rounds "):
                try:
                    new_rounds = max(1, int(user_input.split()[1]))
                    max_rounds = new_rounds
                    ui.status(f"Max rounds set to {new_rounds}", C_GREEN)
                except (ValueError, IndexError):
                    ui.error("Usage: rounds <number>  (e.g. 'rounds 5')")
                continue
            if user_input.lower() == "rounds":
                ui.status(f"Current max rounds: {max_rounds}", C_CYAN)
                ui.status("Usage: rounds <number>  (e.g. 'rounds 5')", C_DIM)
                continue

            while user_input.endswith("\\"):
                user_input = user_input[:-1] + "\n"
                more = input(f"  {C_DIM}  ...{C_RESET} ")
                user_input += more

            congress = Congress(
                max_rounds=max_rounds,
                codex_bin_resolved=codex_path,
                working_dir=working_dir,
            )
            final = congress.run(user_input, output_files=output_files)
            _display_final(ui, final)

        except KeyboardInterrupt:
            print()
            ui.status("Interrupted. Type 'quit' to exit or ask another question.", C_YELLOW)
            print()
        except EOFError:
            break


def _display_final(ui: TerminalUI, final: str):
    print()
    if len(final) > 2000:
        ui.box("SESSION RESULT (truncated)",
               final[:2000] + "\n... (see logs for full output)", C_GREEN)
        ui.status(f"Full session summary ({len(final)} chars) saved in logs.", C_DIM)
    else:
        ui.box("SESSION RESULT", final if final else "(empty)", C_GREEN)
    print()


def _open_folder(path: str):
    os.makedirs(path, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:
        _print_safe(f"  {C_DIM}Could not open folder: {e}{C_RESET}")
        _print_safe(f"  {C_DIM}Path: {path}{C_RESET}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    _configure_stdio()

    max_rounds  = MAX_ROUNDS
    codex_bin   = None
    working_dir = os.getcwd()
    query       = None
    raw_outputs = None

    for arg in sys.argv[1:]:
        if arg.startswith("--max-rounds="):
            try:
                max_rounds = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --max-rounds: {arg}")
                sys.exit(1)
        elif arg.startswith("--codex-bin="):
            codex_bin = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--timeout="):
            try:
                global SILENCE_TIMEOUT
                SILENCE_TIMEOUT = max(30, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --timeout: {arg}")
                sys.exit(1)
        elif arg.startswith("--approval="):
            global APPROVAL_FLAG
            APPROVAL_FLAG = arg.split("=", 1)[1]
        elif arg.startswith("--workdir="):
            working_dir = arg.split("=", 1)[1].strip().strip('"')
            if not os.path.isdir(working_dir):
                print(f"Error: --workdir path does not exist: {working_dir}")
                sys.exit(1)
        elif arg.startswith("--outputs="):
            raw_outputs = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--query="):
            query = arg.split("=", 1)[1].strip().strip('"')
        elif arg in ("--help", "-h"):
            print("CONGRESS v2 - Multi-AI Debate System")
            print()
            print("Usage: python congress.py [options]")
            print()
            print("Options:")
            print("  --max-rounds=N     Max debate rounds (default: 3)")
            print("  --timeout=N        Silence timeout in seconds (default: 3600)")
            print("  --codex-bin=PATH   Path to codex binary")
            print("  --workdir=PATH     Working directory for codex (default: cwd)")
            print("  --outputs=FILES    Comma-separated required output files")
            print("  --query=\"...\"      Run a single query non-interactively")
            print("  --approval=FLAG    Codex approval flag")
            print("  --help             Show this help")
            sys.exit(0)

    # Non-interactive mode: run single query and exit
    if query:
        if not raw_outputs:
            print("ERROR: --outputs is required when using --query.", file=sys.stderr)
            sys.exit(1)
        try:
            output_files = _parse_output_files(raw_outputs, working_dir)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        codex_path = _resolve_codex_binary(codex_bin)
        if not codex_path:
            print("ERROR: Codex CLI not found!")
            sys.exit(1)
        congress = Congress(
            max_rounds=max_rounds,
            codex_bin_resolved=codex_path,
            working_dir=working_dir,
        )
        final = congress.run(query, output_files=output_files)
        print()
        if final:
            print(final)
            sys.exit(0)
        else:
            print("ERROR: No output produced. Check logs for details.", file=sys.stderr)
            sys.exit(1)

    interactive_mode(max_rounds, codex_bin, working_dir)


if __name__ == "__main__":
    main()
