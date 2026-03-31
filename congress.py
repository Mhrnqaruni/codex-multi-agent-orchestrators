"""
CONGRESS - Multi-AI Debate System
==================================
Two Codex agents (Researcher + Inspector) debate in a loop to produce
high-quality output. The Inspector critiques the Researcher's work,
and the Researcher improves based on feedback. Up to 3 rounds or until
the Inspector approves.

Usage:
    python congress.py
    python congress.py --query="How do I implement a binary search tree?"
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
import threading
import queue
import json
import uuid
from pathlib import Path
from datetime import datetime


# ============================================================================
# CONSTANTS
# ============================================================================

MAX_ROUNDS = 3
SILENCE_TIMEOUT = 600       # kill codex if ZERO output (both pipes) for 10 min
STARTUP_TIMEOUT = 90        # kill if no output within 90s of launch
COOLDOWN_BETWEEN = 5        # seconds between codex calls
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
APPROVAL_FLAG = "--dangerously-bypass-approvals-and-sandbox"
MAX_PROMPT_CHARS = 50000    # truncate researcher output if larger than this
AUTO_CONTINUE_SECS = 10     # seconds before auto-continue in transition menu

# Rate limit / billing keywords (case-insensitive check against stderr)
RATE_LIMIT_SIGNALS = ("usage limit", "rate limit", "rate_limit", "quota",
                      "billing", "try again at", "too many requests", "429")

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
BOX_H = "\u2500"
BOX_V = "\u2502"
BOX_TL = "\u250c"
BOX_TR = "\u2510"
BOX_BL = "\u2514"
BOX_BR = "\u2518"


# ============================================================================
# SYSTEM PROMPTS FOR EACH AGENT
# ============================================================================

RESEARCHER_SYSTEM_PROMPT = """You are the RESEARCHER/PLANNER agent in a multi-agent AI system called "Congress".

YOUR ROLE:
- You are a world-class engineer, architect, analyst, and problem solver.
- When given a question or task, you must deeply analyze it, research all angles, and produce the BEST possible solution.
- Think step by step. Consider edge cases, performance, maintainability, and security.
- Your output should be thorough, well-structured, and production-ready.
- You can handle ANY type of task: coding, analysis, research, architecture, debugging, etc.

CRITICAL RULES:
1. DO NOT create any files as output (no .md, no .txt, no any file). Your text response IS your only output.
2. DO NOT ask follow-up questions. Work with what you have and state your assumptions.
3. If the task involves code, make it complete, runnable, well-commented, and secure.
4. If analyzing something, be exhaustive and precise.
5. Structure your response clearly with labeled sections.
6. Always explain your reasoning, trade-offs considered, and alternatives rejected.
7. Search the internet and read files as needed — be thorough.
8. DO NOT wrap your entire response in a markdown code block — write it as plain structured text.

OUTPUT FORMAT:
- UNDERSTANDING: Brief summary of what you understood the task to be.
- SOLUTION: Your detailed solution/analysis.
- ASSUMPTIONS: List any assumptions you made.
"""

RESEARCHER_IMPROVE_PROMPT_TEMPLATE = """The INSPECTOR agent has reviewed your work and provided critique and suggestions.

--- PREVIOUS ROUNDS CONTEXT ---
{rounds_context}
--- END CONTEXT ---

--- INSPECTOR FEEDBACK (Round {round_num}) ---
{inspector_feedback}
--- END FEEDBACK ---

Based on this feedback, you MUST:
1. Address EVERY point raised by the inspector — do not skip any.
2. Fix all identified bugs, flaws, security issues, and logic errors.
3. Implement all reasonable improvement suggestions.
4. For each point, explicitly state what you changed and why.
5. If you DISAGREE with a point, explain why with solid technical reasoning.

CRITICAL:
- DO NOT create any files. Your text response IS your output.
- Produce an IMPROVED and COMPLETE version of your work — not a diff, the full thing.
- DO NOT wrap your entire response in a markdown code block.
"""

INSPECTOR_SYSTEM_PROMPT_TEMPLATE = """You are the INSPECTOR/SUPERVISOR agent in a multi-agent AI system called "Congress".

YOUR ROLE:
- You are a ruthless but fair reviewer, security auditor, and quality inspector.
- You receive the ORIGINAL user request and the RESEARCHER's response.
- Your job is to find EVERY flaw, bug, security issue, logic error, and missed edge case.

{previous_review_context}

YOUR TASKS:
1. VERIFY: Does the researcher's output actually answer the user's original question completely?
2. BUGS: Find all bugs, logic errors, off-by-one errors, race conditions, null/undefined risks.
3. SECURITY: Identify security vulnerabilities (injection, XSS, CSRF, path traversal, etc).
4. PERFORMANCE: Flag performance issues, unnecessary complexity, O(n^2) where O(n) suffices.
5. EDGE CASES: What inputs/scenarios would break this? Empty inputs, huge inputs, unicode, concurrency.
6. COMPLETENESS: Is anything missing? Unhandled error cases? Missing validation?
7. BEST PRACTICES: Industry conventions, naming, structure, documentation.
8. IMPROVEMENTS: Suggest specific, actionable improvements WITH code examples when applicable.

CRITICAL RULES:
1. DO NOT create any files. Your text response IS your output.
2. Be SPECIFIC. Don't say "improve error handling" — say exactly WHERE and HOW.
3. Provide code snippets for your suggested fixes when possible.
4. Rate the severity of each issue: CRITICAL / HIGH / MEDIUM / LOW.
5. At the VERY END of your review, on its own line, you MUST write exactly one of:
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

        self.master_log_path = os.path.join(self.session_dir, "master.log")
        self.researcher_log_path = os.path.join(self.session_dir, "researcher.log")
        self.inspector_log_path = os.path.join(self.session_dir, "inspector.log")
        self.rounds_dir = os.path.join(self.session_dir, "rounds")
        os.makedirs(self.rounds_dir, exist_ok=True)
        self.meta_path = os.path.join(self.session_dir, "session.json")
        self.meta = {
            "session_id": session_id,
            "started_at": datetime.now().isoformat(),
            "status": "running",
            "rounds": [],
            "user_query": "",
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
        entry = f"[{ts}] [{tag}] {message}\n"
        self._append(self.master_log_path, entry)

    def log_user_query(self, query: str):
        self.meta["user_query"] = query
        self._save_meta()
        self.log_master("USER", f"Query: {query[:200]}...")
        self._append(self.master_log_path,
                     f"\n{'─' * 60}\nUSER QUERY:\n{'─' * 60}\n{query}\n{'─' * 60}\n\n")

    def log_agent_start(self, agent: str, round_num, prompt: str):
        self.log_master(agent.upper(), f"Round {round_num} - Started")
        log_path = self.researcher_log_path if agent.startswith("researcher") else self.inspector_log_path
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
        log_path = self.researcher_log_path if agent.startswith("researcher") else self.inspector_log_path
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
        round_data = {
            "round": round_num,
            "verdict": verdict,
            "completed_at": datetime.now().isoformat(),
        }
        self.meta["rounds"].append(round_data)
        self._save_meta()
        self.log_master("SYSTEM", f"Round {round_num} verdict: {verdict}")

    def log_session_end(self, final_output: str, total_rounds: int,
                        status: str = "completed"):
        self.meta["status"] = status
        self.meta["total_rounds"] = total_rounds
        self.meta["ended_at"] = datetime.now().isoformat()
        self._save_meta()

        final_path = os.path.join(self.session_dir, "final_output.txt")
        try:
            with open(final_path, "w", encoding="utf-8") as f:
                f.write(final_output)
        except OSError:
            pass

        self.log_master("SYSTEM",
                        f"Session {status}. {total_rounds} rounds. Output saved to final_output.txt")


# ============================================================================
# TERMINAL UI
# ============================================================================

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences for accurate length calculation."""
    return re.sub(r'\033\[[0-9;]*m', '', text)


class TerminalUI:
    """Terminal UI using ANSI codes."""

    def __init__(self):
        self._width = min(shutil.get_terminal_size().columns, 120)
        if os.name == "nt":
            os.system("")  # enable ANSI on Windows

    def _center(self, text: str, width: int = 0) -> str:
        w = width or self._width
        return text.center(w)

    def clear(self):
        os.system("cls" if os.name == "nt" else "clear")

    def clear_line(self):
        """Clear current line using actual terminal width."""
        sys.stdout.write("\r" + " " * (self._width - 1) + "\r")
        sys.stdout.flush()

    def banner(self):
        print()
        print(f"{C_CYAN}{C_BOLD}")
        print(self._center("=" * 60))
        print(self._center(""))
        print(self._center("  CONGRESS  "))
        print(self._center("  Multi-AI Debate System  "))
        print(self._center(""))
        print(self._center("  Researcher + Inspector Feedback Loop  "))
        print(self._center(""))
        print(self._center("=" * 60))
        print(f"{C_RESET}")
        print()

    def box(self, title: str, content: str, color: str = C_CYAN):
        w = self._width - 4
        title_visible_len = len(_strip_ansi(title))
        title_pad = max(0, w - title_visible_len)

        print(f"  {color}{BOX_TL}{BOX_H * (w + 2)}{BOX_TR}{C_RESET}")
        print(f"  {color}{BOX_V}{C_RESET} {C_BOLD}{title}{C_RESET}{' ' * title_pad}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_V}{BOX_H * (w + 2)}{BOX_V}{C_RESET}")
        for line in content.split("\n"):
            # Strip ANSI for length calc, but display with ANSI
            clean = _strip_ansi(line)
            if len(clean) > w:
                # Truncate by visible chars — just use clean version for safety
                display = clean[:w]
            else:
                display = line
            visible_len = len(_strip_ansi(display))
            padding = max(0, w - visible_len)
            print(f"  {color}{BOX_V}{C_RESET} {display}{' ' * padding}{color}{BOX_V}{C_RESET}")
        print(f"  {color}{BOX_BL}{BOX_H * (w + 2)}{BOX_BR}{C_RESET}")
        print()

    def status(self, message: str, color: str = C_YELLOW):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  {C_DIM}[{ts}]{C_RESET} {color}{C_BOLD}{message}{C_RESET}")

    def agent_header(self, agent_name: str, round_num: int, total_rounds: int, phase: str):
        if agent_name == "RESEARCHER":
            color = C_BLUE
            icon = "[R]"
        else:
            color = C_MAGENTA
            icon = "[I]"
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
            msg = f"APPROVED - Inspector is satisfied after round {round_num}!"
        elif round_num >= max_rounds:
            color = C_YELLOW
            msg = f"NEEDS REVISION - But max rounds ({max_rounds}) reached. Using latest output."
        else:
            color = C_YELLOW
            msg = f"NEEDS REVISION - Moving to round {round_num + 1}"
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
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args)
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(end.encode("utf-8", errors="replace"))
        sys.stdout.flush()


# ============================================================================
# KEYBOARD INPUT HELPERS (cross-platform)
# ============================================================================

def _kbhit() -> bool:
    """Non-blocking check if a key has been pressed (cross-platform)."""
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
    """Consume one keypress and return it as a lowercase string."""
    try:
        if os.name == "nt":
            import msvcrt
            ch = msvcrt.getch()
            # Special keys (arrows, F-keys) send a 2-byte sequence:
            # prefix (b'\xe0' or b'\x00') + key code.
            # Consume both bytes and return empty to ignore special keys.
            if ch in (b'\xe0', b'\x00'):
                msvcrt.getch()  # consume the second byte
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


# ============================================================================
# CODEX CLI INTERFACE
# ============================================================================

def _resolve_codex_binary(cli_arg: str | None = None) -> str | None:
    """Resolve Codex CLI path on Windows/non-Windows."""
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
    """Thread that reads lines from a pipe and puts them on the queue."""
    try:
        for line in pipe:
            q.put((tag, line))
    except Exception:
        pass
    finally:
        q.put((tag + "_done", None))


def run_codex(prompt: str, codex_bin: str, agent_name: str,
              ui: TerminalUI, logger: CongressLogger,
              round_num, working_dir: str,
              startup_timeout: int | None = None,
              silence_timeout: int | None = None) -> tuple[str, str, int, float]:
    """
    Run Codex CLI with a prompt. ALWAYS starts a new session.
    Returns (stdout, stderr, returncode, duration).

    Timeout logic: we track last activity on EITHER pipe (stdout or stderr).
    If codex is thinking (producing stderr like tool calls, search results),
    it is NOT considered silent. Only truly dead silence triggers timeout.
    """
    _startup_timeout = startup_timeout if startup_timeout is not None else STARTUP_TIMEOUT
    _silence_timeout = silence_timeout if silence_timeout is not None else SILENCE_TIMEOUT
    if os.name == "nt" and codex_bin.lower().endswith((".cmd", ".bat")):
        base_cmd = ["cmd.exe", "/c", codex_bin]
    else:
        base_cmd = [codex_bin]

    cmd = base_cmd + ["exec", APPROVAL_FLAG, "--skip-git-repo-check", "-"]

    logger.log_master(agent_name.upper(), f"Executing: {' '.join(cmd)}")
    logger.log_master(agent_name.upper(), f"Working dir: {working_dir}")

    start_time = time.time()
    proc = None

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
        return "", str(e), -1, duration

    # Write prompt and close stdin
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        logger.log_master(agent_name.upper(), f"STDIN write failed: {e}")
        ui.error(f"Failed to send prompt to Codex: {e}")
        _kill_proc(proc)
        duration = time.time() - start_time
        return "", f"stdin write failed: {e}", -1, duration

    # Reader threads for stdout and stderr
    q = queue.Queue()
    t_out = threading.Thread(target=_pipe_reader, args=(proc.stdout, q, "out"), daemon=True)
    t_err = threading.Thread(target=_pipe_reader, args=(proc.stderr, q, "err"), daemon=True)
    t_out.start()
    t_err.start()

    stdout_lines = []
    stderr_lines = []
    done_flags = set()
    last_activity_time = time.time()   # ANY output on either pipe resets this
    codex_started = False
    line_count = 0
    stderr_count = 0

    try:
        while len(done_flags) < 2:
            try:
                tag, line = q.get(timeout=1.0)
            except queue.Empty:
                silent_secs = int(time.time() - last_activity_time)

                # Startup timeout: no output at all within startup timeout
                if not codex_started and silent_secs >= _startup_timeout:
                    _kill_proc(proc)
                    duration = time.time() - start_time
                    msg = f"Codex did not start within {_startup_timeout}s"
                    logger.log_master(agent_name.upper(), msg)
                    return "", msg, -1, duration

                # Silence timeout: ZERO output on BOTH pipes for silence timeout
                # If stderr is active (codex thinking/searching), this does NOT trigger
                if codex_started and silent_secs >= _silence_timeout:
                    _kill_proc(proc)
                    duration = time.time() - start_time
                    msg = f"Timed out: zero activity for {silent_secs}s"
                    logger.log_master(agent_name.upper(), msg)
                    return "".join(stdout_lines), msg, -1, duration

                # Show waiting indicator
                if silent_secs > 0 and silent_secs % 5 == 0:
                    elapsed_total = int(time.time() - start_time)
                    if not codex_started:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} Waiting for Codex to start... "
                            f"{C_DIM}({silent_secs}s){C_RESET}    ")
                    else:
                        sys.stdout.write(
                            f"\r  {C_YELLOW}|{C_RESET} {agent_name} working... "
                            f"{C_DIM}({elapsed_total}s elapsed, silent {silent_secs}s){C_RESET}    ")
                    sys.stdout.flush()
                continue

            if tag == "out_done":
                done_flags.add("out")
                continue
            if tag == "err_done":
                done_flags.add("err")
                continue

            # ANY output on EITHER pipe counts as activity (thinking = alive)
            last_activity_time = time.time()
            codex_started = True
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
                # Show stderr activity so user knows codex is thinking
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
        return "".join(stdout_lines), "Interrupted by user", -2, duration

    proc.wait()
    duration = time.time() - start_time

    if line_count > 30:
        _print_safe(f"  {C_DIM}  ... {line_count} total lines received{C_RESET}")

    return "".join(stdout_lines), "".join(stderr_lines), proc.returncode, duration


def _kill_proc(proc: subprocess.Popen):
    """Safely kill a subprocess and wait for it to finish."""
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
    """Parse the inspector's verdict from their output.

    Searches from the END of the output to avoid matching meta-discussion
    about verdicts earlier in the text.
    """
    # Search last 500 chars for the verdict line
    tail = inspector_output[-500:] if len(inspector_output) > 500 else inspector_output
    # Regex: VERDICT followed by optional whitespace/colon, then the decision
    match = re.search(r'VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)', tail, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # Fallback: if no verdict found, assume needs revision
    return "NEEDS_REVISION"


# ============================================================================
# PROMPT SIZE GUARD
# ============================================================================

def _truncate_for_prompt(text: str, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """Truncate text to fit within prompt size limits."""
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
        self.max_rounds = max_rounds
        # Accept pre-resolved binary to avoid duplicate filesystem scans
        self.codex_bin = codex_bin_resolved or _resolve_codex_binary(codex_bin)
        self.working_dir = working_dir or os.getcwd()
        self.ui = TerminalUI()
        # Use timestamp + short uuid to prevent session ID collisions
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.logger = CongressLogger(self.session_id)

        # Track all rounds for context passing
        self.round_history: list[dict] = []
        self._interactive = sys.stdin.isatty()

        if not self.codex_bin:
            self.ui.error("Codex CLI not found! Install it or pass --codex-bin=<path>")
            sys.exit(1)

    def _build_researcher_prompt(self, user_query: str, round_num: int) -> str:
        """Build the full researcher prompt with all context from previous rounds."""
        if round_num == 1:
            return (
                f"{RESEARCHER_SYSTEM_PROMPT}\n\n"
                f"{'=' * 60}\n"
                f"USER REQUEST:\n"
                f"{'=' * 60}\n"
                f"{user_query}\n"
            )

        # Round 2+: include the FULL conversation history so codex has context
        # (we no longer use resume --last, every call is a new session)
        rounds_context = self._format_rounds_context()
        last_inspector = self.round_history[-1].get("inspector_output", "")

        return (
            f"{RESEARCHER_SYSTEM_PROMPT}\n\n"
            f"{'=' * 60}\n"
            f"USER REQUEST:\n"
            f"{'=' * 60}\n"
            f"{user_query}\n\n"
            f"{RESEARCHER_IMPROVE_PROMPT_TEMPLATE.format(rounds_context=rounds_context, inspector_feedback=_truncate_for_prompt(last_inspector), round_num=round_num - 1)}"
        )

    def _build_inspector_prompt(self, user_query: str, researcher_output: str,
                                round_num: int) -> str:
        """Build the inspector prompt with context about previous reviews."""
        if round_num == 1:
            previous_context = ""
        else:
            previous_reviews = []
            for rh in self.round_history:
                if "inspector_output" in rh:
                    previous_reviews.append(
                        f"--- Your Round {rh['round']} Review (summary) ---\n"
                        f"{_truncate_for_prompt(rh['inspector_output'], 5000)}\n"
                        f"--- End Round {rh['round']} ---"
                    )
            previous_context = (
                "IMPORTANT: You have reviewed previous versions. Here are your prior reviews:\n\n"
                + "\n\n".join(previous_reviews)
                + "\n\nCheck whether the researcher addressed your previous feedback. "
                "Don't re-flag issues that were properly fixed.\n"
            )

        prompt_template = INSPECTOR_SYSTEM_PROMPT_TEMPLATE.format(
            previous_review_context=previous_context
        )

        return (
            f"{prompt_template}\n\n"
            f"{'=' * 60}\n"
            f"ORIGINAL USER REQUEST:\n"
            f"{'=' * 60}\n"
            f"{user_query}\n\n"
            f"{'=' * 60}\n"
            f"RESEARCHER'S OUTPUT (Round {round_num}):\n"
            f"{'=' * 60}\n"
            f"{_truncate_for_prompt(researcher_output)}\n"
        )

    def _format_rounds_context(self) -> str:
        """Format previous rounds for inclusion in prompts."""
        parts = []
        for rh in self.round_history:
            parts.append(
                f"=== Round {rh['round']} ===\n"
                f"Your output ({len(rh.get('researcher_output', ''))} chars): "
                f"{_truncate_for_prompt(rh.get('researcher_output', ''), 10000)}\n"
            )
        return "\n".join(parts) if parts else "(First round)"

    # ------------------------------------------------------------------
    # Transition menu (pause / resume / output / continue)
    # ------------------------------------------------------------------

    def _transition_menu(self, next_agent: str, round_num: int) -> str:
        """Show transition menu between agents.
        Returns: 'continue', 'output', or 'quit'.
        Skipped entirely in non-interactive mode.
        """
        if not self._interactive:
            return "continue"

        # Flush stale keypresses from during codex execution
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

        # Set cbreak mode on Unix for single-keypress detection
        old_settings = None
        if os.name != "nt":
            try:
                import termios
                import tty
                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                old_settings = None

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
            # Restore terminal mode on Unix
            if old_settings is not None:
                try:
                    import termios
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

    def _menu_countdown(self) -> str:
        """Auto-continue countdown. Returns 'continue', 'pause', 'output', or 'quit'."""
        for remaining in range(AUTO_CONTINUE_SECS, 0, -1):
            sys.stdout.write(
                f"\r  {C_DIM}  Auto-continue in {remaining}s... "
                f"(press C/P/O/Q){C_RESET}    ")
            sys.stdout.flush()

            # Poll for 1 second in small increments
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
                    # Unknown key — ignore, keep countdown
                time.sleep(0.1)
        else:
            # Countdown finished without keypress — auto-continue
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
            self.ui.status("Auto-continuing...", C_DIM)
            return "continue"

    def _menu_pause_loop(self) -> str:
        """Pause loop. Waits for user to press R/O/Q. Returns 'continue', 'output', or 'quit'."""
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
                # Unknown key — ignore
            time.sleep(0.2)
            wait_count += 1
            if wait_count % 75 == 0:  # every 15 seconds
                elapsed = wait_count * 0.2
                self.ui.status(
                    f"Still paused ({elapsed:.0f}s)... "
                    f"[R] Resume  [O] Output  [Q] Quit", C_DIM)

    # ------------------------------------------------------------------
    # Finalize output (one more codex pass to polish)
    # ------------------------------------------------------------------

    def _finalize_output(self, user_query: str, current_output: str) -> str:
        """Send current output through one more codex call to clean/finalize it."""
        self.ui.status("Finalizing output with one more Codex pass...", C_CYAN)
        self.logger.log_master("SYSTEM", "Finalize output requested")

        finalize_prompt = (
            f"{RESEARCHER_SYSTEM_PROMPT}\n\n"
            f"{'=' * 60}\n"
            f"ORIGINAL USER REQUEST:\n"
            f"{'=' * 60}\n"
            f"{user_query}\n\n"
            f"{'=' * 60}\n"
            f"YOUR WORK-IN-PROGRESS (from debate rounds):\n"
            f"{'=' * 60}\n"
            f"{_truncate_for_prompt(current_output)}\n\n"
            f"Now produce the FINAL, polished version of your solution.\n"
            f"Clean up any rough edges, remove references to inspector feedback,\n"
            f"ensure completeness, and output the definitive answer.\n\n"
            f"CRITICAL: DO NOT create, modify, or delete any files.\n"
            f"DO NOT run any commands. Just produce clean text output.\n"
        )

        self.logger.log_agent_start("researcher", "finalize", finalize_prompt)

        stdout, stderr, rc, duration = run_codex(
            finalize_prompt, self.codex_bin,
            "researcher", self.ui, self.logger,
            "finalize", self.working_dir,
            startup_timeout=30, silence_timeout=120
        )

        self.logger.log_agent_output("researcher", "finalize", stdout, stderr, rc, duration)

        result = stdout.strip()
        if not result:
            # Check if rate limited
            is_limited, retry_info = _is_rate_limited(stderr, rc)
            if is_limited:
                self.ui.error(f"Finalize hit rate limit.{retry_info} Using raw output.")
            else:
                self.ui.status("Finalize produced no output. Using raw output.", C_YELLOW)
            self.logger.log_master("SYSTEM", f"Finalize failed (rc={rc}), using raw output")
            return current_output

        self.ui.status(f"Finalized ({duration:.0f}s, {len(result)} chars)", C_GREEN)
        return result

    # ------------------------------------------------------------------
    # Main debate loop
    # ------------------------------------------------------------------

    def run(self, user_query: str) -> str:
        """Run the full Researcher-Inspector loop. Returns final output."""

        self.logger.log_user_query(user_query)
        self.ui.status(f"Session: {self.session_id}")
        self.ui.status(f"Codex: {self.codex_bin}")
        self.ui.status(f"Max rounds: {self.max_rounds}")
        self.ui.status(f"Working dir: {self.working_dir}")
        self.ui.status(f"Logs: {self.logger.session_dir}")
        if self._interactive:
            self.ui.status(
                "Pause: press P at transition prompts between agents", C_DIM)
        print()

        researcher_output = ""
        final_output = ""
        session_status = "completed"

        for round_num in range(1, self.max_rounds + 1):
            round_data = {"round": round_num}

            # ── RESEARCHER PHASE ──
            researcher_prompt = self._build_researcher_prompt(user_query, round_num)

            self.ui.agent_header("RESEARCHER", round_num, self.max_rounds,
                                 "Initial Analysis" if round_num == 1 else "Improving Based on Feedback")
            self.logger.log_agent_start("researcher", round_num, researcher_prompt)

            stdout, stderr, rc, duration = run_codex(
                researcher_prompt, self.codex_bin,
                "researcher", self.ui, self.logger, round_num,
                self.working_dir
            )

            self.logger.log_agent_output("researcher", round_num, stdout, stderr, rc, duration)
            researcher_output = stdout.strip()
            total_duration = duration

            # Handle failure
            if rc != 0 and not researcher_output:
                self.ui.error(f"Researcher failed (rc={rc}): {stderr[:200]}")

                if rc == -2:  # user interrupt
                    round_data["researcher_output"] = ""
                    round_data["error"] = "interrupted"
                    self.round_history.append(round_data)
                    session_status = "interrupted"
                    break

                # Check rate limit BEFORE retrying
                is_limited, retry_info = _is_rate_limited(stderr, rc)
                if is_limited:
                    self.ui.error(f"API rate/usage limit hit.{retry_info}")
                    self.logger.log_master("SYSTEM", f"RATE LIMIT: {stderr[:300]}")
                    round_data["researcher_output"] = ""
                    round_data["error"] = "rate_limit"
                    self.round_history.append(round_data)
                    session_status = "rate_limited"
                    break

                # Non-rate-limit failure — retry once
                self.ui.status("Retrying researcher...")
                stdout, stderr, rc, duration = run_codex(
                    researcher_prompt, self.codex_bin,
                    "researcher", self.ui, self.logger, round_num,
                    self.working_dir
                )
                self.logger.log_agent_output(
                    "researcher (retry)", round_num, stdout, stderr, rc, duration)
                researcher_output = stdout.strip()
                total_duration += duration

                if not researcher_output:
                    # Check if retry also hit rate limit
                    is_limited, retry_info = _is_rate_limited(stderr, rc)
                    if is_limited:
                        self.ui.error(f"Retry also hit rate limit.{retry_info}")
                        session_status = "rate_limited"
                    else:
                        self.ui.error(f"Researcher failed twice. Aborting. (rc={rc})")
                        session_status = "failed"
                    round_data["researcher_output"] = ""
                    round_data["error"] = f"failed_twice (rc={rc})"
                    self.round_history.append(round_data)
                    break

            round_data["researcher_output"] = researcher_output
            self.ui.status(
                f"Researcher finished ({total_duration:.0f}s, "
                f"{len(researcher_output)} chars)", C_BLUE)

            # ── TRANSITION MENU: Researcher → Inspector ──
            action = self._transition_menu("Inspector", round_num)
            if action == "output":
                self.round_history.append(round_data)
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = "output_early"
                break
            if action == "quit":
                self.round_history.append(round_data)
                final_output = researcher_output
                session_status = "quit_by_user"
                break

            time.sleep(COOLDOWN_BETWEEN)

            # ── INSPECTOR PHASE ──
            inspector_prompt = self._build_inspector_prompt(
                user_query, researcher_output, round_num)

            self.ui.agent_header("INSPECTOR", round_num, self.max_rounds,
                                 "Reviewing & Critiquing")
            self.logger.log_agent_start("inspector", round_num, inspector_prompt)

            stdout, stderr, rc, duration = run_codex(
                inspector_prompt, self.codex_bin,
                "inspector", self.ui, self.logger, round_num,
                self.working_dir
            )

            self.logger.log_agent_output("inspector", round_num, stdout, stderr, rc, duration)
            inspector_output = stdout.strip()
            inspector_duration = duration

            if not inspector_output:
                self.ui.error(f"Inspector produced no output (rc={rc}): {stderr[:200]}")

                if rc == -2:  # user interrupt
                    round_data["inspector_output"] = ""
                    self.round_history.append(round_data)
                    session_status = "interrupted"
                    break

                # Check rate limit before retrying
                is_limited, retry_info = _is_rate_limited(stderr, rc)
                if is_limited:
                    self.ui.error(f"API rate/usage limit hit.{retry_info}")
                    self.logger.log_master("SYSTEM", f"RATE LIMIT (inspector): {stderr[:300]}")
                    self.ui.status("Using researcher output as final.", C_YELLOW)
                    final_output = researcher_output
                    round_data["inspector_output"] = ""
                    self.round_history.append(round_data)
                    self.logger.log_round_summary(round_num, "RATE_LIMITED")
                    session_status = "rate_limited"
                    break

                # Retry inspector once
                self.ui.status("Retrying inspector...")
                stdout, stderr, rc, duration = run_codex(
                    inspector_prompt, self.codex_bin,
                    "inspector", self.ui, self.logger, round_num,
                    self.working_dir
                )
                self.logger.log_agent_output(
                    "inspector (retry)", round_num, stdout, stderr, rc, duration)
                inspector_output = stdout.strip()
                inspector_duration += duration

                if not inspector_output:
                    is_limited, retry_info = _is_rate_limited(stderr, rc)
                    if is_limited:
                        self.ui.error(f"Inspector retry also hit rate limit.{retry_info}")
                        session_status = "rate_limited"
                    else:
                        self.ui.error("Inspector failed twice. Using researcher output.")
                        session_status = "completed"
                    final_output = researcher_output
                    self.logger.log_round_summary(round_num, "INSPECTOR_FAILED")
                    round_data["inspector_output"] = ""
                    self.round_history.append(round_data)
                    break

            round_data["inspector_output"] = inspector_output
            self.round_history.append(round_data)

            self.ui.status(
                f"Inspector finished ({inspector_duration:.0f}s, {len(inspector_output)} chars)", C_MAGENTA)

            verdict = parse_verdict(inspector_output)
            self.ui.verdict_display(verdict, round_num, self.max_rounds)
            self.logger.log_round_summary(round_num, verdict)

            if verdict == "APPROVED":
                final_output = researcher_output
                self.ui.status(
                    f"Inspector APPROVED after {round_num} round(s)!", C_GREEN)
                self.logger.log_session_end(final_output, round_num)
                self.ui.final_result(round_num, self.logger.session_dir)
                return final_output

            if round_num == self.max_rounds:
                self.ui.status(
                    f"Max rounds ({self.max_rounds}) reached. Using latest researcher output.",
                    C_YELLOW)
                final_output = researcher_output
                break

            # ── TRANSITION MENU: Inspector → next Researcher ──
            action = self._transition_menu("Researcher", round_num + 1)
            if action == "output":
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = "output_early"
                break
            if action == "quit":
                final_output = researcher_output
                session_status = "quit_by_user"
                break

            self.ui.status(f"Preparing round {round_num + 1}...", C_DIM)
            time.sleep(COOLDOWN_BETWEEN)

        if not final_output:
            final_output = researcher_output

        total = len(self.round_history) or 1
        if not final_output:
            session_status = "failed"
        self.logger.log_session_end(final_output, total, session_status)
        self.ui.final_result(total, self.logger.session_dir)
        return final_output


# ============================================================================
# INTERACTIVE LOOP
# ============================================================================

def interactive_mode(max_rounds: int, codex_bin: str | None, working_dir: str):
    """Main interactive loop where the user can send messages."""
    ui = TerminalUI()
    ui.clear()
    ui.banner()

    ui.box("How it works", (
        "1. You type a question or task\n"
        "2. RESEARCHER agent analyzes deeply and produces a solution\n"
        "3. INSPECTOR agent critiques and finds every flaw\n"
        "4. RESEARCHER improves based on feedback\n"
        "5. Loop repeats until APPROVED or max rounds reached\n"
        "\n"
        "Between agents you can Pause, Output early, or Quit.\n"
        "\n"
        "Commands:\n"
        "  'quit'       exit Congress\n"
        "  'logs'       open logs folder\n"
        "  'rounds N'   set max rounds (current: 3)"
    ))

    # Resolve codex once, reuse for all sessions
    codex_path = _resolve_codex_binary(codex_bin)
    if not codex_path:
        ui.error("Codex CLI not found!")
        ui.error("Install: npm install -g @openai/codex")
        sys.exit(1)

    ui.status(f"Codex: {codex_path}", C_GREEN)
    ui.status(f"Max rounds: {max_rounds}", C_GREEN)
    ui.status(f"Working dir: {working_dir}", C_GREEN)
    ui.status(f"Logs: {LOG_DIR}", C_GREEN)
    print()

    while True:
        try:
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

            # Multi-line input: end line with \ to continue
            while user_input.endswith("\\"):
                user_input = user_input[:-1] + "\n"
                more = input(f"  {C_DIM}  ...{C_RESET} ")
                user_input += more

            # Pass pre-resolved binary to avoid re-scanning filesystem
            congress = Congress(
                max_rounds=max_rounds,
                codex_bin_resolved=codex_path,
                working_dir=working_dir,
            )
            final = congress.run(user_input)

            # Display final output
            print()
            if len(final) > 2000:
                ui.box("FINAL OUTPUT (truncated)",
                       final[:2000] + "\n... (see logs for full output)", C_GREEN)
                ui.status(f"Full output ({len(final)} chars) saved in logs.", C_DIM)
            else:
                ui.box("FINAL OUTPUT", final if final else "(empty)", C_GREEN)
            print()

        except KeyboardInterrupt:
            print()
            ui.status("Interrupted. Type 'quit' to exit or ask another question.", C_YELLOW)
            print()
        except EOFError:
            break


def _open_folder(path: str):
    """Open a folder in the system file browser."""
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

    max_rounds = MAX_ROUNDS
    codex_bin = None
    working_dir = os.getcwd()
    query = None

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
        elif arg.startswith("--query="):
            query = arg.split("=", 1)[1].strip().strip('"')
        elif arg in ("--help", "-h"):
            print("CONGRESS - Multi-AI Debate System")
            print()
            print("Usage: python congress.py [options]")
            print()
            print("Options:")
            print("  --max-rounds=N     Max debate rounds (default: 3)")
            print("  --timeout=N        Silence timeout in seconds (default: 600)")
            print("  --codex-bin=PATH   Path to codex binary")
            print("  --workdir=PATH     Working directory for codex (default: cwd)")
            print("  --query=\"...\"      Run a single query non-interactively")
            print("  --approval=FLAG    Codex approval flag")
            print("  --help             Show this help")
            sys.exit(0)

    # Non-interactive mode: run single query and exit
    if query:
        codex_path = _resolve_codex_binary(codex_bin)
        if not codex_path:
            print("ERROR: Codex CLI not found!")
            sys.exit(1)
        congress = Congress(
            max_rounds=max_rounds,
            codex_bin_resolved=codex_path,
            working_dir=working_dir,
        )
        final = congress.run(query)
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
