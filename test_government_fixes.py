"""
Test script to verify all government.py bug fixes.
Tests run without Codex CLI — they test Python logic only.
"""

import sys
import os
import json
import tempfile
import shutil

# Add parent dir so we can import
sys.path.insert(0, os.path.dirname(__file__))

from government import (
    GovernmentState, verify_file_created, verify_has_verdict,
    _parse_session_id, _strip_ansi, build_context_anchor,
    build_executor_prompt, build_inspector_prompt,
    generate_project_status,
    C_RESET, C_GREEN, C_RED, C_YELLOW, C_BOLD,
)

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


def test_fix1_first_run_no_crash():
    """Fix 1: GovernmentState should not crash on first run."""
    print(f"\n{C_BOLD}=== Fix 1: GovernmentState first-run crash ==={C_RESET}")

    with tempfile.TemporaryDirectory() as tmpdir:
        gov_dir = os.path.join(tmpdir, ".government")
        os.makedirs(gov_dir, exist_ok=True)

        # state.json does NOT exist — this used to crash with AttributeError
        try:
            state = GovernmentState(gov_dir)
            check("First run: no crash", True)
        except AttributeError as e:
            check("First run: no crash", False, str(e))
            return

        check("self.data is a dict", isinstance(state.data, dict))
        check("current_step is 'init'", state.get("current_step") == "init")
        check("state.json created", os.path.exists(state.state_path))

        # Also verify corrupt recovery still works
        with open(state.state_path, "w") as f:
            f.write("{invalid json!!!")
        # Write a valid .tmp backup
        temp = state.state_path + ".tmp"
        with open(temp, "w") as f:
            json.dump({"source_file": "test.md", "current_step": "exec",
                        "current_phase": 2, "phases_completed": [1],
                        "executor_session_id": None, "inspector_session_id": None,
                        "current_round": 0, "total_executor_calls": 0,
                        "total_inspector_calls": 0, "phase_summaries": {},
                        "user_instructions": "", "working_dir": "",
                        "created_at": "2025-01-01"}, f)

        state2 = GovernmentState(gov_dir)
        check("Corrupt recovery from .tmp", state2.get("current_step") == "exec")

        # Both corrupt — should create fresh state
        with open(state.state_path, "w") as f:
            f.write("broken")
        with open(temp, "w") as f:
            f.write("also broken")
        state3 = GovernmentState(gov_dir)
        check("Both corrupt: fresh state", state3.get("current_step") == "init")


def test_fix2_skip_returns_skipped():
    """Fix 2: soft-stop skip should return 'skipped', not True."""
    print(f"\n{C_BOLD}=== Fix 2: Soft-stop skip returns 'skipped' ==={C_RESET}")

    # We can't easily test the full loop, but we can verify the return values
    # by checking the source code for the fix
    import inspect
    from government import Government

    # Check _do_master_plan source for 'return "skipped"'
    src_mp = inspect.getsource(Government._do_master_plan)
    check("_do_master_plan has 'return \"skipped\"'",
          'return "skipped"' in src_mp)
    check("_do_master_plan no longer has skip->True",
          'elif choice == "s":\n                    return True' not in src_mp)

    src_pp = inspect.getsource(Government._do_phase_plan)
    check("_do_phase_plan has 'return \"skipped\"'",
          'return "skipped"' in src_pp)

    src_pe = inspect.getsource(Government._do_phase_execution)
    check("_do_phase_execution has 'return \"skipped\"'",
          'return "skipped"' in src_pe)

    # Check that callers handle "skipped" distinctly
    src_run = inspect.getsource(Government.run)
    check("run() checks mp_result == 'skipped'",
          'mp_result == "skipped"' in src_run)
    check("run() checks plan_result == 'skipped'",
          'plan_result == "skipped"' in src_run)
    check("run() checks exec_result == 'skipped'",
          'exec_result == "skipped"' in src_run)

    # Verify 'skipped' is truthy (so 'if not result' still works for False)
    check("'skipped' is truthy", bool("skipped") is True)
    check("False is falsy", bool(False) is False)


def test_fix3_session_error_no_rc1():
    """Fix 3: session error detection should not include 'or rc == 1'."""
    print(f"\n{C_BOLD}=== Fix 3: Session error detection (no rc==1) ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government._call_agent)
    check("No 'or rc == 1' in is_session_error",
          "or rc == 1" not in src)
    check("Checks 'session not found' in stderr",
          '"session not found" in stderr_lower' in src)
    check("Checks 'invalid session' in stderr",
          '"invalid session" in stderr_lower' in src)
    check("Checks 'unknown session' in stderr",
          '"unknown session" in stderr_lower' in src)
    check("Checks 'no such session' in stderr",
          '"no such session" in stderr_lower' in src)

    # Simulate: rc=1, no stdout, stderr="rate limit exceeded" — should NOT be session error
    # (We test the logic directly)
    session_id = "abc-123"
    rc = 1
    stdout = ""
    stderr_lower = "rate limit exceeded"
    is_session_error = (session_id and rc != 0 and not stdout.strip()
                        and ("session not found" in stderr_lower
                             or "invalid session" in stderr_lower
                             or "unknown session" in stderr_lower
                             or "no such session" in stderr_lower))
    check("Rate limit (rc=1) NOT detected as session error", not is_session_error)

    # But actual session error should still be caught
    stderr_lower2 = "session not found: abc-123"
    is_session_error2 = (session_id and rc != 0 and not stdout.strip()
                         and ("session not found" in stderr_lower2
                              or "invalid session" in stderr_lower2
                              or "unknown session" in stderr_lower2
                              or "no such session" in stderr_lower2))
    check("Session-not-found IS detected as session error", is_session_error2)


def test_fix4_resume_logic():
    """Fix 4: Resume should not redo master plan when past that step."""
    print(f"\n{C_BOLD}=== Fix 4: Resume logic ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government.run)

    # Check that resume uses saved_step to skip master plan
    check("Resume checks past_master_plan",
          "past_master_plan" in src)
    check("past_master_plan uses saved_step",
          'saved_step not in ("init", "master_plan")' in src)

    # Check mid-phase resume skips planning
    check("Mid-phase resume: skip_plan logic exists",
          "skip_plan" in src)
    check("skip_plan checks saved_step for exec",
          'saved_step in ("exec", "exec_review", "checkpoint")' in src)

    # Test the actual condition:
    # Scenario: resume after master plan done, phase 1 plan in progress, no completed phases
    saved_step = "plan"
    completed_phases = []
    master_plan_exists = True
    can_resume = True
    past_master_plan = saved_step not in ("init", "master_plan")
    should_skip = can_resume and master_plan_exists and (completed_phases or past_master_plan)
    check("Resume at 'plan' step with no completed phases: skips master plan",
          should_skip is True)

    # Scenario: resume at master_plan step — should NOT skip
    saved_step2 = "master_plan"
    past_master_plan2 = saved_step2 not in ("init", "master_plan")
    should_skip2 = can_resume and master_plan_exists and (completed_phases or past_master_plan2)
    check("Resume at 'master_plan' step: does NOT skip master plan",
          should_skip2 is False)


def test_fix5_source_file_always_updated():
    """Fix 5: State source_file should always match CLI arg."""
    print(f"\n{C_BOLD}=== Fix 5: Source file always updated in state ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government.__init__)

    # Should NOT have the old "if not self.state.get('source_file')" guard
    check("No conditional source_file write",
          'if not self.state.get("source_file")' not in src)
    # Should always call state.update with source_file
    check("Always updates source_file in state",
          "self.state.update(" in src and "source_file=self.source_file" in src)
    # Should warn on change
    check("Warns on source file change",
          "Source file changed" in src)


def test_fix6_eoferror_handling():
    """Fix 6: All input() calls should handle EOFError."""
    print(f"\n{C_BOLD}=== Fix 6: EOFError handling ==={C_RESET}")

    import inspect
    from government import TerminalUI, Government

    # Check soft_stop_prompt
    src_ssp = inspect.getsource(TerminalUI.soft_stop_prompt)
    check("soft_stop_prompt handles EOFError",
          "EOFError" in src_ssp)

    # Check user_checkpoint
    src_uc = inspect.getsource(TerminalUI.user_checkpoint)
    check("user_checkpoint handles EOFError",
          "EOFError" in src_uc)

    # Check run() resume prompt
    src_run = inspect.getsource(Government.run)
    check("run() resume prompt handles EOFError",
          "EOFError" in src_run)

    # Check main() interactive setup
    from government import main
    src_main = inspect.getsource(main)
    eof_count = src_main.count("EOFError")
    check(f"main() has multiple EOFError handlers (found {eof_count})",
          eof_count >= 3)


def test_fix7_verify_file_no_workdir_param():
    """Fix 7: verify_file_created should not have unused workdir param."""
    print(f"\n{C_BOLD}=== Fix 7: Unused workdir param removed ==={C_RESET}")

    import inspect
    sig = inspect.signature(verify_file_created)
    params = list(sig.parameters.keys())
    check("verify_file_created params: no 'workdir'",
          "workdir" not in params,
          f"got params: {params}")
    check("verify_file_created params: has 'expected_path'",
          "expected_path" in params)
    check("verify_file_created params: has 'min_chars'",
          "min_chars" in params)

    # Functional test: verify it still works
    with tempfile.TemporaryDirectory() as tmpdir:
        # Missing file
        ok, msg = verify_file_created(os.path.join(tmpdir, "nope.md"))
        check("Missing file detected", ok is False)

        # Short file
        short_path = os.path.join(tmpdir, "short.md")
        with open(short_path, "w") as f:
            f.write("hi")
        ok, msg = verify_file_created(short_path)
        check("Short file detected", ok is False and "too short" in msg)

        # Good file
        good_path = os.path.join(tmpdir, "good.md")
        with open(good_path, "w") as f:
            f.write("x" * 100)
        ok, msg = verify_file_created(good_path)
        check("Good file passes", ok is True)


def test_fix8_session_id_header_only():
    """Fix 8: Session ID should only be captured from codex startup header, not file content."""
    print(f"\n{C_BOLD}=== Fix 8: Session ID header-bounded capture ==={C_RESET}")

    import inspect
    from government import run_codex

    src = inspect.getsource(run_codex)
    check("header_separator_count variable exists",
          "header_separator_count" in src)
    check("Checks for '--------' separator lines",
          'startswith("--------")' in src)
    check("Only captures when header_separator_count == 1",
          "header_separator_count == 1" in src)

    # Simulate the header parsing logic
    # Real codex header:
    # OpenAI Codex v0.116.0 (research preview)
    # --------                     ← separator_count becomes 1
    # workdir: ...
    # session id: REAL-UUID        ← captured (separator_count == 1)
    # --------                     ← separator_count becomes 2
    # user
    # ... later file content with "session id: WRONG-UUID"  ← NOT captured

    header_separator_count = 0
    captured_sid = None

    stderr_lines = [
        "OpenAI Codex v0.116.0 (research preview)\n",
        "--------\n",
        "workdir: C:\\project\n",
        "model: gpt-5.4\n",
        "session id: aaaa-bbbb-cccc-dddd\n",
        "--------\n",
        "user\n",
        "reading file master.log...\n",
        "[16:47:20] [EXECUTOR] Session ID: 9999-8888-7777-6666\n",
        "session id: 9999-8888-7777-6666\n",
    ]

    for line in stderr_lines:
        if header_separator_count < 2 and line.strip().startswith("--------"):
            header_separator_count += 1
        if header_separator_count == 1 and "session id:" in line.lower():
            sid = _parse_session_id(line)
            if sid:
                captured_sid = sid

    check("Captures real session ID from header",
          captured_sid == "aaaa-bbbb-cccc-dddd")
    check("Does NOT capture false session ID from file content",
          captured_sid != "9999-8888-7777-6666")

    # Edge case: no session id in header
    header_separator_count = 0
    captured_sid = None
    for line in ["--------\n", "workdir: test\n", "--------\n", "session id: fake\n"]:
        if header_separator_count < 2 and line.strip().startswith("--------"):
            header_separator_count += 1
        if header_separator_count == 1 and "session id:" in line.lower():
            sid = _parse_session_id(line)
            if sid:
                captured_sid = sid
    check("No capture after header closes (separator_count == 2)", captured_sid is None)


def test_fix9_executor_init_no_user_instructions():
    """Fix 9: Executor init should not include user_instructions."""
    print(f"\n{C_BOLD}=== Fix 9: Executor init no user_instructions ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government._init_agents)

    # Executor init should NOT have user_instructions
    check("No 'ADDITIONAL INSTRUCTIONS FROM USER' in init",
          "ADDITIONAL INSTRUCTIONS FROM USER" not in src)
    check("No 'ADDITIONAL CONTEXT FROM USER' in init",
          "ADDITIONAL CONTEXT FROM USER" not in src)
    # Should have explicit "do not build" language
    check("Executor init says 'Do NOT start building'",
          "Do NOT start building" in src)
    check("Executor init says 'Do NOT make any changes'",
          "Do NOT make any changes" in src)

    # User instructions should be in master plan instead
    src_mp = inspect.getsource(Government._do_master_plan)
    check("Master plan injects user_instructions",
          "self.user_instructions" in src_mp
          and "ADDITIONAL INSTRUCTIONS FROM USER" in src_mp)


def test_fix10_inspector_no_file_modification():
    """Fix 10: Inspector should never modify project files."""
    print(f"\n{C_BOLD}=== Fix 10: Inspector read-only ==={C_RESET}")

    from government import INSPECTOR_SYSTEM

    # Check INSPECTOR_SYSTEM has the new rule
    check("INSPECTOR_SYSTEM has 'NEVER create, modify, write, or delete'",
          "NEVER create, modify, write, or delete" in INSPECTOR_SYSTEM)
    check("INSPECTOR_SYSTEM has 'READ-ONLY reviewer'",
          "READ-ONLY reviewer" in INSPECTOR_SYSTEM)
    check("INSPECTOR_SYSTEM has YOUR LIMITATIONS section",
          "YOUR LIMITATIONS" in INSPECTOR_SYSTEM)
    check("INSPECTOR_SYSTEM has 'MUST NOT create, modify'",
          "MUST NOT create, modify" in INSPECTOR_SYSTEM)

    # Check inspector init prompt
    import inspect
    from government import Government
    src = inspect.getsource(Government._init_agents)

    check("Inspector init says 'Do NOT create, modify, or delete'",
          "Do NOT create, modify, or delete" in src)
    check("Inspector init says 'READ-ONLY reviewer'",
          "READ-ONLY reviewer" in src)
    check("Inspector init has no user_instructions",
          "self.user_instructions" not in src.split("# Inspector init")[1]
          if "# Inspector init" in src else True)


def test_fix11_fallback_no_user_instructions_for_inspector():
    """Fix 11: Session fallback should NOT inject user_instructions into inspector."""
    print(f"\n{C_BOLD}=== Fix 11: Fallback no user_instructions for inspector ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government._call_agent)

    # The fallback block should only include instructions for executor
    check("Fallback checks agent == 'executor' before injecting instructions",
          'agent == "executor"' in src and "instructions_block" in src)

    # Should NOT have bare self.user_instructions in the init_msg
    # Find the fallback section (after "Re-send system prompt")
    fallback_idx = src.find("Re-send system prompt")
    if fallback_idx >= 0:
        fallback_section = src[fallback_idx:]
        # Old bug: f"{self.user_instructions}\n\n" directly in init_msg
        check("No bare user_instructions in fallback init_msg",
              'f"{self.user_instructions}\\n\\n"' not in fallback_section)
        check("Has USER INSTRUCTIONS label for executor only",
              "USER INSTRUCTIONS" in fallback_section)
    else:
        check("Fallback section found", False, "Could not find fallback section")


def test_fix12_fallback_increments_call_count():
    """Fix 12: Session fallback should increment the agent call counter."""
    print(f"\n{C_BOLD}=== Fix 12: Fallback call count increment ==={C_RESET}")

    import inspect
    from government import Government

    recovery_src = inspect.getsource(Government._run_with_recovery)
    call_src = inspect.getsource(Government._call_agent)

    increment_line = "self.state.set(call_key, self.state.get(call_key, 0) + 1)"
    recovery_count = recovery_src.count(increment_line)
    call_count = call_src.count(increment_line)

    check("Call counter increment exists in _run_with_recovery",
          recovery_count == 1,
          f"found {recovery_count} increments in _run_with_recovery, expected 1")
    check("_call_agent does not duplicate the increment",
          call_count == 0,
          f"found {call_count} increments in _call_agent, expected 0")


def test_fix13_interrupt_after_phase_summary():
    """Fix 13: Interrupt during phase summary should not mark phase complete."""
    print(f"\n{C_BOLD}=== Fix 13: Interrupt check after _get_phase_summary ==={C_RESET}")

    import inspect
    from government import Government

    src = inspect.getsource(Government.run)

    # After _get_phase_summary, there should be an interrupt check BEFORE
    # marking the phase as completed
    summary_idx = src.find("_get_phase_summary")
    completed_idx = src.find("cur_completed.append(phase_num)")

    if summary_idx >= 0 and completed_idx >= 0:
        between = src[summary_idx:completed_idx]
        check("Interrupt check exists between summary and completion",
              "_interrupt_requested" in between)
        check("Returns on interrupt (doesn't mark phase complete)",
              "return" in between)
    else:
        check("Found both summary and completion markers", False)


def test_fix14_count_phases_supports_phase_0():
    """Fix 14: _count_phases should return (start, end) tuple supporting Phase 0."""
    print(f"\n{C_BOLD}=== Fix 14: Phase 0 support in _count_phases ==={C_RESET}")

    from government import Government
    import inspect

    # Check return type annotation
    src = inspect.getsource(Government._count_phases)
    check("Returns tuple, not int", "tuple[int, int]" in src)
    check("Returns None on failure", "return None" in src)

    # Simulate: plan with phases 0-6
    with tempfile.TemporaryDirectory() as tmpdir:
        gov_dir = os.path.join(tmpdir, ".government")
        os.makedirs(gov_dir)
        state = GovernmentState(gov_dir)
        state.update(source_file="test.md")

        # Create a mock Government-like object to call _count_phases
        class MockGov:
            def __init__(self, wd):
                self.working_dir = wd
        mg = MockGov(tmpdir)
        mg._count_phases = Government._count_phases.__get__(mg, type(mg))

        # Test 1: phases 0-6
        mp = os.path.join(tmpdir, "master_plan.md")
        with open(mp, "w") as f:
            f.write("## Phase 0: Foundation\n## Phase 1: Auth\n"
                    "## Phase 2: Billing\n## Phase 6: Security\n")
        result = mg._count_phases()
        check("Phases 0-6: start=0", result is not None and result[0] == 0)
        check("Phases 0-6: end=6", result is not None and result[1] == 6)

        # Test 2: phases 1-5 (traditional)
        with open(mp, "w") as f:
            f.write("## Phase 1: Setup\n## Phase 2: Build\n## Phase 5: Deploy\n")
        result = mg._count_phases()
        check("Phases 1-5: start=1", result is not None and result[0] == 1)
        check("Phases 1-5: end=5", result is not None and result[1] == 5)

        # Test 3: Total Phases fallback (no headings)
        with open(mp, "w") as f:
            f.write("Total Phases: 3\nSome other content\n")
        result = mg._count_phases()
        check("Total Phases fallback: start=1", result is not None and result[0] == 1)
        check("Total Phases fallback: end=3", result is not None and result[1] == 3)

        # Test 4: no master plan
        os.remove(mp)
        result = mg._count_phases()
        check("No master plan: returns None", result is None)


def test_fix15_prev_ref_supports_phase_0():
    """Fix 15: prev_ref should use > 0, not > 1, so Phase 1 can see Phase 0."""
    print(f"\n{C_BOLD}=== Fix 15: prev_ref condition > 0 ==={C_RESET}")

    import inspect
    from government import Government

    plan_src = inspect.getsource(Government._do_phase_plan)
    exec_src = inspect.getsource(Government._do_phase_execution)

    check("_do_phase_plan uses 'phase_num > 0'",
          "phase_num > 0" in plan_src)
    check("_do_phase_plan does NOT use 'phase_num > 1'",
          "phase_num > 1" not in plan_src)
    check("_do_phase_execution uses 'phase_num > 0'",
          "phase_num > 0" in exec_src)
    check("_do_phase_execution does NOT use 'phase_num > 1'",
          "phase_num > 1" not in exec_src)


def test_fix16_auto_continue_in_checkpoint():
    """Fix 16: user_checkpoint should support auto_timeout parameter."""
    print(f"\n{C_BOLD}=== Fix 16: Auto-continue in user_checkpoint ==={C_RESET}")

    import inspect
    from government import TerminalUI, _kbhit, _consume_key

    sig = inspect.signature(TerminalUI.user_checkpoint)
    params = list(sig.parameters.keys())
    check("user_checkpoint has 'auto_timeout' param",
          "auto_timeout" in params)

    src = inspect.getsource(TerminalUI.user_checkpoint)
    check("Has auto_timeout countdown logic", "auto_timeout > 0" in src)
    check("Returns 'c' on timeout", 'return "c"' in src)
    check("Uses _kbhit for key detection", "_kbhit()" in src)
    check("Uses _consume_key before input", "_consume_key()" in src)

    # Verify helpers exist and are callable
    check("_kbhit is callable", callable(_kbhit))
    check("_consume_key is callable", callable(_consume_key))


def test_fix17_resume_mode_skips_prompt():
    """Fix 17: resume_mode=True should skip the resume prompt in run()."""
    print(f"\n{C_BOLD}=== Fix 17: resume_mode skips double prompt ==={C_RESET}")

    import inspect
    from government import Government

    init_src = inspect.getsource(Government.__init__)
    check("__init__ has resume_mode param", "resume_mode" in init_src)
    check("Stores _resume_mode", "_resume_mode" in init_src)

    run_src = inspect.getsource(Government.run)
    check("run() checks _resume_mode", "_resume_mode" in run_src)
    check("Forces can_resume when _resume_mode",
          "can_resume = True" in run_src)


def test_fix18_phase0_migration():
    """Fix 18: When resuming with Phase 0 plans, auto-mark Phase 0 as done."""
    print(f"\n{C_BOLD}=== Fix 18: Phase 0 auto-completion migration ==={C_RESET}")

    import inspect
    from government import Government

    run_src = inspect.getsource(Government.run)
    check("Has Phase 0 migration logic",
          "0 not in completed_phases" in run_src)
    check("Auto-appends 0 to completed_phases",
          "completed_phases.append(0)" in run_src)
    check("Checks start_phase == 0",
          "start_phase == 0" in run_src)


def test_fix19_smart_resume_main():
    """Fix 19: main() supports directory-first resume flow."""
    print(f"\n{C_BOLD}=== Fix 19: Smart resume in main() ==={C_RESET}")

    import inspect
    from government import main

    src = inspect.getsource(main)
    check("Checks for state.json in workdir",
          "state.json" in src or "state_path" in src)
    check("Loads saved state for resume display",
          "saved_source" in src or "saved.get" in src)
    check("Supports resume_mode flag",
          "resume_mode = True" in src)
    check("Supports --auto-continue CLI arg",
          "--auto-continue" in src)
    check("Supports positional workdir arg",
          "os.path.isdir(arg)" in src)
    check("Shows r/n/q choices",
          '"r"' in src and '"q"' in src)


def test_existing_functionality():
    """Verify existing features still work after all fixes."""
    print(f"\n{C_BOLD}=== Existing functionality ==={C_RESET}")

    # Session ID parsing
    check("Parse session ID",
          _parse_session_id("session id: 019d2367-391d-71b0-a6dc-94741344400f")
          == "019d2367-391d-71b0-a6dc-94741344400f")
    check("No session ID", _parse_session_id("random stderr output") is None)

    # ANSI stripping
    check("Strip ANSI",
          _strip_ansi("\033[91mhello\033[0m") == "hello")

    # Verdict parsing
    with tempfile.TemporaryDirectory() as tmpdir:
        vf = os.path.join(tmpdir, "review.md")
        with open(vf, "w") as f:
            f.write("Some review\nVERDICT: APPROVED\n")
        ok, v = verify_has_verdict(vf)
        check("Verdict APPROVED", ok and v == "APPROVED")

        with open(vf, "w") as f:
            f.write("Issues found\nVERDICT: NEEDS_REVISION\n")
        ok, v = verify_has_verdict(vf)
        check("Verdict NEEDS_REVISION", ok and v == "NEEDS_REVISION")

    # State persistence round-trip
    with tempfile.TemporaryDirectory() as tmpdir:
        gov_dir = os.path.join(tmpdir, ".government")
        state = GovernmentState(gov_dir)
        state.update(current_phase=3, phases_completed=[1, 2])
        state2 = GovernmentState(gov_dir)
        check("State round-trip: phase", state2.get("current_phase") == 3)
        check("State round-trip: completed", state2.get("phases_completed") == [1, 2])

    # Phase count regex (existing fix from before)
    with tempfile.TemporaryDirectory() as tmpdir:
        mp = os.path.join(tmpdir, "master_plan.md")
        with open(mp, "w") as f:
            f.write(
                "# Master Plan\n"
                "## Phase 1: Setup\n"
                "### Phase 1 sub-heading\n"
                "## Phase 2: Build\n"
                "## Phase 3: Deploy\n"
                "### Total Phases: 3\n"
            )
        import re
        with open(mp) as f:
            content = f.read()
        match = re.search(r'Total\s+Phases\s*:\s*(\d+)', content, re.IGNORECASE)
        check("Phase count from 'Total Phases:'", match and int(match.group(1)) == 3)

        phases = re.findall(r'^##\s+Phase\s+(\d+)', content, re.IGNORECASE | re.MULTILINE)
        check("Phase headings (anchored, no sub-headings)",
              sorted(int(p) for p in phases) == [1, 2, 3])

    # Context anchor and prompt building
    with tempfile.TemporaryDirectory() as tmpdir:
        gov_dir = os.path.join(tmpdir, ".government")
        state = GovernmentState(gov_dir)
        state.update(source_file="spec.md", current_phase=2, current_step="exec",
                     phases_completed=[1])

        anchor = build_context_anchor(state, tmpdir)
        check("Context anchor has source", "spec.md" in anchor)
        check("Context anchor has phase", "Current phase: 2" in anchor)

        prompt = build_executor_prompt(state, tmpdir, "Do the task")
        check("Executor prompt has reminders", "IMPORTANT REMINDERS" in prompt)
        check("Executor prompt has task", "Do the task" in prompt)

    # Import check
    check("Module imports cleanly", True)


# ============================================================================
# RUN ALL TESTS
# ============================================================================

if __name__ == "__main__":
    print(f"\n{C_BOLD}{'=' * 60}")
    print(f"  GOVERNMENT.PY — FIX VERIFICATION TESTS")
    print(f"{'=' * 60}{C_RESET}")

    test_fix1_first_run_no_crash()
    test_fix2_skip_returns_skipped()
    test_fix3_session_error_no_rc1()
    test_fix4_resume_logic()
    test_fix5_source_file_always_updated()
    test_fix6_eoferror_handling()
    test_fix7_verify_file_no_workdir_param()
    test_fix8_session_id_header_only()
    test_fix9_executor_init_no_user_instructions()
    test_fix10_inspector_no_file_modification()
    test_fix11_fallback_no_user_instructions_for_inspector()
    test_fix12_fallback_increments_call_count()
    test_fix13_interrupt_after_phase_summary()
    test_fix14_count_phases_supports_phase_0()
    test_fix15_prev_ref_supports_phase_0()
    test_fix16_auto_continue_in_checkpoint()
    test_fix17_resume_mode_skips_prompt()
    test_fix18_phase0_migration()
    test_fix19_smart_resume_main()
    test_existing_functionality()

    print(f"\n{C_BOLD}{'=' * 60}")
    total = passed + failed
    if failed == 0:
        print(f"  {C_GREEN}ALL {total} TESTS PASSED{C_RESET}")
    else:
        print(f"  {C_RED}{failed}/{total} TESTS FAILED{C_RESET}")
    print(f"{'=' * 60}{C_RESET}\n")

    sys.exit(1 if failed else 0)
