"""
Targeted tests for congress.py rate-limit recovery behavior.
Tests run without Codex CLI by monkeypatching the module functions.
"""

import os
import threading

import congress as congress_mod
from congress import Congress, C_BOLD, C_GREEN, C_RED, C_RESET


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {C_GREEN}PASS{C_RESET}  {name}")
        passed += 1
    else:
        print(f"  {C_RED}FAIL{C_RESET}  {name}  {C_RED}{detail}{C_RESET}")
        failed += 1


class DummyUI:
    def __init__(self):
        self.statuses = []
        self.errors = []

    def status(self, msg, color=None):
        self.statuses.append(msg)

    def error(self, msg):
        self.errors.append(msg)


class DummyLogger:
    def __init__(self):
        self.master_logs = []

    def log_master(self, tag, msg):
        self.master_logs.append((tag, msg))


def test_wait_for_rate_limit_manual_and_auto_retry():
    print(f"\n{C_BOLD}=== Congress: Rate-limit wait manual + auto retry ==={C_RESET}")

    class DummyCongress:
        def __init__(self):
            self.ui = DummyUI()
            self.logger = DummyLogger()
            self._interrupt_requested = False

    app = DummyCongress()
    app._wait_for_rate_limit = Congress._wait_for_rate_limit.__get__(app, DummyCongress)

    orig_flush = congress_mod._flush_input
    orig_enter = congress_mod._enter_cbreak
    orig_exit = congress_mod._exit_cbreak
    orig_kbhit = congress_mod._kbhit
    orig_consume = congress_mod._consume_key
    orig_sleep = congress_mod.time.sleep
    orig_mono = congress_mod.time.monotonic

    try:
        congress_mod._flush_input = lambda: None
        congress_mod._enter_cbreak = lambda: None
        congress_mod._exit_cbreak = lambda old: None
        congress_mod.time.sleep = lambda secs: None

        # Manual retry
        key_hits = iter([True])
        congress_mod._kbhit = lambda: next(key_hits, False)
        congress_mod._consume_key = lambda: "r"
        congress_mod.time.monotonic = lambda: 0.0

        action = app._wait_for_rate_limit(" Retry after: later")
        check("Manual R returns manual_retry", action == "manual_retry")
        check("Manual retry keeps interrupt clear", app._interrupt_requested is False)
        check("Initial status mentions auto-retry",
              any("Auto-retry in" in msg for msg in app.ui.statuses))
        check("Manual retry is logged",
              any("retry requested by user" in msg.lower() for _, msg in app.logger.master_logs))

        # Auto retry
        app.ui.statuses.clear()
        app.ui.errors.clear()
        app.logger.master_logs.clear()
        app._interrupt_requested = False

        class FakeClock:
            def __init__(self, step):
                self.value = 0
                self.step = step

            def __call__(self):
                self.value += self.step
                return float(self.value)

        congress_mod._kbhit = lambda: False
        congress_mod._consume_key = lambda: ""
        congress_mod.time.monotonic = FakeClock(congress_mod.RATE_LIMIT_AUTO_RETRY_SECONDS)

        action = app._wait_for_rate_limit("")
        check("Timer expiry returns auto_retry", action == "auto_retry")
        check("Auto retry keeps interrupt clear", app._interrupt_requested is False)
        check("Auto retry is logged",
              any("auto-retry" in msg.lower() for _, msg in app.logger.master_logs))
    finally:
        congress_mod._flush_input = orig_flush
        congress_mod._enter_cbreak = orig_enter
        congress_mod._exit_cbreak = orig_exit
        congress_mod._kbhit = orig_kbhit
        congress_mod._consume_key = orig_consume
        congress_mod.time.sleep = orig_sleep
        congress_mod.time.monotonic = orig_mono


def test_rate_limit_retries_are_not_capped():
    print(f"\n{C_BOLD}=== Congress: Rate-limit retries are uncapped ==={C_RESET}")

    class DummyCongress:
        pass

    app = DummyCongress()
    app.ui = DummyUI()
    app.logger = DummyLogger()
    app.codex_bin = "codex"
    app.working_dir = os.getcwd()
    app._interrupt_requested = False
    app.researcher_session_id = "sid-1"
    app._wait_for_internet = lambda: True
    app._do_interactive_pause = lambda: True

    retry_actions = iter(["auto_retry", "auto_retry", "manual_retry"])

    def fake_wait_for_rate_limit(self, retry_info):
        return next(retry_actions)

    app._wait_for_rate_limit = fake_wait_for_rate_limit.__get__(app, DummyCongress)
    app._run_with_recovery = Congress._run_with_recovery.__get__(app, DummyCongress)

    responses = [("", "rate limit exceeded", 1, 0.01, "sid-1")] * 3
    responses.append(("done", "", 0, 0.02, "sid-2"))
    run_calls = {"count": 0}

    orig_run_codex = congress_mod.run_codex
    orig_is_network_error = congress_mod._is_network_error
    orig_is_rate_limited = congress_mod._is_rate_limited
    orig_mono = congress_mod.time.monotonic

    try:
        congress_mod.time.monotonic = (lambda c=iter(range(1000)): float(next(c)))

        def fake_run_codex(prompt, codex_bin, agent_name, ui, logger, round_label,
                           working_dir, session_id=None, startup_timeout=None,
                           silence_timeout=None, pause_event=None):
            idx = run_calls["count"]
            run_calls["count"] += 1
            return responses[idx]

        congress_mod.run_codex = fake_run_codex
        congress_mod._is_network_error = lambda stderr, rc: False
        congress_mod._is_rate_limited = (
            lambda stderr, rc: ("rate limit" in (stderr or "").lower(), "")
        )

        stdout, stderr, rc, duration, sid = app._run_with_recovery(
            "researcher", "task", "R1", threading.Event())

        check("Rate-limit flow eventually succeeds", rc == 0 and stdout == "done")
        check("Rate-limit flow retries past old cap", run_calls["count"] == 4)
        check("Session id still updates on later success", sid == "sid-2")
    finally:
        congress_mod.run_codex = orig_run_codex
        congress_mod._is_network_error = orig_is_network_error
        congress_mod._is_rate_limited = orig_is_rate_limited
        congress_mod.time.monotonic = orig_mono


def test_network_retries_still_capped():
    print(f"\n{C_BOLD}=== Congress: Network retries stay capped ==={C_RESET}")

    class DummyCongress:
        pass

    app = DummyCongress()
    app.ui = DummyUI()
    app.logger = DummyLogger()
    app.codex_bin = "codex"
    app.working_dir = os.getcwd()
    app._interrupt_requested = False
    app.researcher_session_id = "sid-1"
    app._wait_for_internet = lambda: True
    app._wait_for_rate_limit = lambda retry_info: "manual_retry"
    app._do_interactive_pause = lambda: True
    app._run_with_recovery = Congress._run_with_recovery.__get__(app, DummyCongress)

    responses = [("", "network error", -3, 0.01, "sid-1")] * (congress_mod.MAX_RECOVERY_RETRIES + 2)
    run_calls = {"count": 0}

    orig_run_codex = congress_mod.run_codex
    orig_is_network_error = congress_mod._is_network_error
    orig_is_rate_limited = congress_mod._is_rate_limited

    try:
        def fake_run_codex(prompt, codex_bin, agent_name, ui, logger, round_label,
                           working_dir, session_id=None, startup_timeout=None,
                           silence_timeout=None, pause_event=None):
            idx = run_calls["count"]
            run_calls["count"] += 1
            return responses[idx]

        congress_mod.run_codex = fake_run_codex
        congress_mod._is_network_error = lambda stderr, rc: True
        congress_mod._is_rate_limited = lambda stderr, rc: (False, "")

        stdout, stderr, rc, duration, sid = app._run_with_recovery(
            "researcher", "task", "R1", threading.Event())

        check("Network failure still returns failure", rc == -3)
        check("Network retry cap still applies",
              run_calls["count"] == congress_mod.MAX_RECOVERY_RETRIES + 1)
    finally:
        congress_mod.run_codex = orig_run_codex
        congress_mod._is_network_error = orig_is_network_error
        congress_mod._is_rate_limited = orig_is_rate_limited


if __name__ == "__main__":
    print(f"\n{C_BOLD}{'=' * 60}")
    print("  CONGRESS.PY — RATE LIMIT RECOVERY TESTS")
    print(f"{'=' * 60}{C_RESET}")

    test_wait_for_rate_limit_manual_and_auto_retry()
    test_rate_limit_retries_are_not_capped()
    test_network_retries_still_capped()

    print(f"\n{C_BOLD}{'=' * 60}")
    total = passed + failed
    if failed == 0:
        print(f"  {C_GREEN}ALL {total} TESTS PASSED{C_RESET}")
    else:
        print(f"  {C_RED}{failed}/{total} TESTS FAILED{C_RESET}")
    print(f"{'=' * 60}{C_RESET}\n")

    raise SystemExit(1 if failed else 0)
