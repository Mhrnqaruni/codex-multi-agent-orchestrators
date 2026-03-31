# Government.py — Critical Update: Rate Limit & Death Spiral Protection

## Root Cause (from TypeTalk Phase 4 incident)

At 13:54 on 2026-03-26, the OpenAI/Codex API hit a usage limit during Phase 4 execution round 2. Codex returned:
```
ERROR: You've hit your usage limit. Upgrade to Pro... try again at 4:17 PM.
```

**What happened:** Codex exited with `rc=1`, empty stdout, and stderr containing the codex startup header (which includes `"session id: ..."` text). Our `is_session_error` check matched on `"session" in stderr_lower` — misidentifying a **rate limit** as a **session-not-found error**. This triggered the fallback (new session), which also hit the rate limit, creating a death spiral: 6 rounds × 2 agents = 12 wasted codex calls in 2 minutes, all producing 0 output.

## Fix 1: Detect Rate Limit / Billing Errors Before Session Error Check

**File:** `government.py`
**Location:** `_call_agent()`, after the first `run_codex()` call returns, BEFORE the `is_session_error` check.

**Logic:** Check stderr for rate-limit / billing / quota keywords. If found, do NOT fallback to a new session — it will fail the same way. Instead, log the error, show the user a clear message with the retry time (if parseable), and set `_interrupt_requested = True` to halt the loop.

**Keywords to detect (case-insensitive in stderr):**
- `"usage limit"`
- `"rate limit"`
- `"rate_limit"`
- `"quota"`
- `"billing"`
- `"try again at"`
- `"too many requests"`
- `"429"`

**Code sketch:**
```python
# In _call_agent(), after run_codex() returns and before is_session_error check:

stderr_lower = stderr.lower() if stderr else ""

# Check for rate limit / billing errors FIRST — do NOT fallback on these
RATE_LIMIT_SIGNALS = ("usage limit", "rate limit", "rate_limit", "quota",
                      "billing", "try again at", "too many requests", "429")
if rc != 0 and any(sig in stderr_lower for sig in RATE_LIMIT_SIGNALS):
    # Extract "try again at TIME" if present
    retry_match = re.search(r'try again at\s+(.+?)[\.\n]', stderr, re.IGNORECASE)
    retry_info = f" Retry after: {retry_match.group(1)}" if retry_match else ""
    msg = f"API rate/usage limit hit for {agent}.{retry_info}"
    self.ui.error(msg)
    self.logger.master(agent.upper(), f"RATE LIMIT: {stderr[:300]}")
    self._interrupt_requested = True
    return ""
```

**Why before `is_session_error`:** The session error check looks for `"session"` in stderr. Codex's startup header ALWAYS contains `"session id: ..."`, so ANY codex stderr that includes the header will match `"session" in stderr_lower`. The rate limit check must run first to prevent false classification.

## Fix 2: Detect Death Spiral (Fallback Also Produces Empty Output)

**File:** `government.py`
**Location:** `_call_agent()`, after the fallback `run_codex()` call.

**Logic:** If the fallback session ALSO returns empty stdout, something systemic is wrong (rate limit, API down, auth expired). Don't let the loop continue as if the agent did work — set `_interrupt_requested` and stop.

**Code sketch:**
```python
# After the fallback run_codex() call, before the rc == -2 check:

if is_session_error:
    # ... existing fallback code ...
    stdout, stderr, rc, duration, new_sid = run_codex(
        init_msg, self.codex_bin, None, ...)

    # NEW: Check if fallback also failed
    if not stdout.strip() and rc != 0:
        msg = (f"Fallback session for {agent} also returned no output (rc={rc}). "
               f"Possible API outage or billing issue. Halting.")
        self.ui.error(msg)
        self.logger.master(agent.upper(), f"FALLBACK FAILED: rc={rc}, stderr={stderr[:300]}")
        self._interrupt_requested = True
        return ""

    # ... rest of existing fallback code (log, count, save session) ...
```

**Why this matters:** Without this check, the loop continues to the inspector, which also gets empty output, writes a meaningless review with NEEDS_REVISION, and the cycle repeats. Each round wastes ~15 seconds and creates garbage files.

## Fix 3: Prevent `is_session_error` False Positive on Startup Header

**File:** `government.py`
**Location:** The `is_session_error` condition in `_call_agent()`.

**Current code:**
```python
is_session_error = (session_id and rc != 0 and not stdout.strip()
                    and ("session" in stderr_lower or "not found" in stderr_lower
                         or "invalid" in stderr_lower))
```

**Problem:** Every codex stderr includes the startup header with `"session id: ..."`. So `"session" in stderr_lower` matches ALL failed codex runs, not just session-not-found errors. This is why rate limits were misidentified.

**Fix:** Make the session error detection more specific — look for patterns that actually indicate a session problem, not just the word "session":

```python
is_session_error = (session_id and rc != 0 and not stdout.strip()
                    and ("session not found" in stderr_lower
                         or "invalid session" in stderr_lower
                         or "unknown session" in stderr_lower
                         or "no such session" in stderr_lower
                         or ("not found" in stderr_lower
                             and "session" in stderr_lower)))
```

**Why:** `"session" in stderr_lower` is too broad — it matches `"session id: abc123"` in the startup header. Requiring `"session not found"` or `"invalid session"` as phrases eliminates false positives. The `"not found" + "session"` combo is kept as a fallback for varied error message formats.

## Execution Order

Apply fixes in this order:
1. **Fix 3** first (tighten `is_session_error`) — prevents false positives
2. **Fix 1** second (rate limit detection) — catches billing/quota errors before session check
3. **Fix 2** third (fallback failure detection) — safety net for any other systemic failure

## Impact on TypeTalk Resume

After applying fixes, resuming TypeTalk:
- `state.json` has `current_phase: 4`, `current_step: "exec"`, `current_round: 7`, `phases_completed: [1, 2, 0, 3]`
- The executor/inspector session IDs in state are from the death spiral (short-lived, likely expired)
- On resume, the first `_call_agent` will try to resume these sessions → will likely fail → fallback creates new sessions
- The fallback sessions will work normally (assuming the usage limit has reset)
- Phase 4 execution restarts from round 1 (round counter is local to the loop)
- The plan is still approved (`plan_approved.md` exists) — no need to re-plan

## Files Modified

| File | Changes |
|------|---------|
| `government.py` | `_call_agent()`: add rate limit detection, tighten `is_session_error`, add fallback failure detection |

## Test Scenarios

1. **Rate limit hit on primary call** → should show error message with retry time, halt loop, show exit info
2. **Rate limit hit on fallback call** → should show "fallback failed" error, halt loop
3. **Genuine session-not-found** → should still trigger fallback (not blocked by rate limit check)
4. **Normal operation** → no change in behavior
5. **Codex startup header with "session id:" in stderr** → should NOT trigger `is_session_error` by itself
