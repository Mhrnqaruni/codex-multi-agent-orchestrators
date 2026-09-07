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
import errno
import re
import time
import shutil
import socket
import threading
import queue
import json
import uuid
import hashlib
import fnmatch
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
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
LOCK_STALE_SECONDS    = 24 * 60 * 60
CONGRESS_ROUNDS_DIR   = "congress_rounds"   # stable round output files (in working_dir)
CONGRESS_STATE_FILE   = "congress_state.json"  # resume state (in working_dir)
CONGRESS_RESULT_FILE  = "congress_result.md"  # terminal result/status artifact (in working_dir)
CONGRESS_HISTORY_FILE = "congress_history.md"  # append-only readable lifecycle history
CONGRESS_VERIFICATION_FILE = "congress_verification.md"  # future verification evidence
CONGRESS_BLOCKED_FILE = "congress_blocked.md"  # future blocked-state details
CONGRESS_LOCK_FILE    = "congress.lock"  # future workspace lock
CONGRESS_LOCK_TAKEOVER_FILE = CONGRESS_LOCK_FILE + ".takeover"
RESEARCHER_UPDATED_FILE = "researcher_updated.md"  # managed researcher notes / reasoning doc
INSPECTOR_COMMENTS_FILE = "inspector_comments.md"  # managed inspector review doc
INSPECTOR_2_COMMENTS_FILE = "inspector_2_comments.md"  # future second inspector review doc
SESSION_REQUEST_FILE  = "session_request.md"  # managed copy of the original request + output contract
STATE_VERSION         = 3
STATE_SCHEMA_VERSION  = 3
MAX_RECOVERY_RETRIES  = 3        # legacy interactive retry hint; unattended recovery is not capped by this
RATE_LIMIT_AUTO_RETRY_SECONDS = 180  # auto-retry every 3 minutes while waiting on rate limits
MAX_CONTINUATIONS     = 10       # max "continue" prompts when context limit cuts output
INTERNET_CHECK_INTERVAL = 30     # seconds between internet checks during silence
SOURCE_MANIFEST_MAX_FILES = 80
SOURCE_MANIFEST_MAX_BYTES = 1024 * 1024
SOURCE_CONTEXT_VERSION = 1
SOURCE_CONTEXT_TIER_REQUIRED = 1
SOURCE_CONTEXT_TIER_PROJECT = 2
SOURCE_CONTEXT_TIER_OPTIONAL = 3
SOURCE_CONTEXT_TIER_EXCLUDED = 4
SOURCE_CONTEXT_EXCLUDED_SUMMARY_MAX = 40
SOURCE_CONTEXT_TIER_LABELS = {
    SOURCE_CONTEXT_TIER_REQUIRED: "Tier 1 - Required Source/Input Files",
    SOURCE_CONTEXT_TIER_PROJECT: "Tier 2 - Optional Project Context",
    SOURCE_CONTEXT_TIER_OPTIONAL: "Tier 3 - Optional Discoverable Files",
    SOURCE_CONTEXT_TIER_EXCLUDED: "Tier 4 - Excluded by Default Summary",
}
SOURCE_CONTEXT_PRUNE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", ".tox", ".nox", "dist", "build", "target",
    "coverage", ".coverage", ".cache",
}
SOURCE_CONTEXT_EXCLUDED_DIR_PARTS = {
    "secrets", "secret", "logs", CONGRESS_ROUNDS_DIR.lower(),
}
SOURCE_CONTEXT_SENSITIVE_NAME_PATTERN = (
    r"(?i)(?:api[_-]?key|apikey|token|credential|credentials|password|passwd|"
    r"secret|private[_-]?key|access[_-]?key|refresh[_-]?token|client[_-]?secret)"
)
PREFLIGHT_TIMEOUT_SECONDS = 2
DEFAULT_VERIFICATION_MODE = "auto"
VERIFICATION_MODES = {"auto", "required", "off"}
DEFAULT_MAX_VERIFICATION_TIMEOUT = 3600
MIN_MAX_VERIFICATION_TIMEOUT = 1
VERIFICATION_OUTPUT_EXCERPT_CHARS = 4000
DEFAULT_SECOND_INSPECTOR_MODE = "auto"
SECOND_INSPECTOR_MODES = {"auto", "on", "off"}
DEFAULT_LOG_PROMPTS_MODE = "full"
LOG_PROMPTS_MODES = {"full", "redacted", "off"}
DEFAULT_INTERACTIVE_CI_MODE = False

TASK_CLASS_DOCUMENTATION_ONLY = "documentation_only"
TASK_CLASS_CODE_STATIC = "code_static"
TASK_CLASS_CODE_TESTABLE = "code_testable"
TASK_CLASS_UI_RUNTIME = "ui_runtime"
TASK_CLASS_SERVICE_INTEGRATION = "service_integration"
TASK_CLASS_RESEARCH_CURRENT_INFO = "research_current_info"
TASK_CLASS_SECURITY_SENSITIVE = "security_sensitive"
TASK_CLASS_MIXED = "mixed"

TASK_CLASSES = {
    TASK_CLASS_DOCUMENTATION_ONLY,
    TASK_CLASS_CODE_STATIC,
    TASK_CLASS_CODE_TESTABLE,
    TASK_CLASS_UI_RUNTIME,
    TASK_CLASS_SERVICE_INTEGRATION,
    TASK_CLASS_RESEARCH_CURRENT_INFO,
    TASK_CLASS_SECURITY_SENSITIVE,
    TASK_CLASS_MIXED,
}

RESEARCH_REQUIRED_EVIDENCE = [
    "retrieval_timestamp",
    "concrete_source_list",
    "source_confidence",
    "primary_secondary_distinction",
    "checked_online_scope",
    "not_checked_reasons",
    "stale_unverified_warnings",
]

RESEARCH_EVIDENCE_REQUIREMENT_TEXT = (
    "Research/current-info tasks require retrieval timestamp, concrete source list "
    "with URLs/domains/repository names/paper identifiers/citation details/named "
    "official documents, source confidence, primary/secondary distinction, "
    "checked-online scope, not-checked reasons where applicable, and stale/unverified "
    "source warnings."
)

RESEARCH_STRONG_SIGNAL_PATTERNS = [
    ("explicit_research", r"\bresearch(?:ing|ed)?\b"),
    ("web_search", r"\b(?:web search|search (?:the )?(?:internet|web|online)|look online)\b"),
    ("online_sources", r"\bonline sources?\b"),
    ("external_evidence", r"\bexternal\s+(?:sources?|evidence|references?)\b"),
    ("current_info", r"\b(?:latest|newest|today|recent|up[- ]to[- ]date|news|price|schedule|version)\b|\b(?:current|up[- ]to[- ]date)\s+(?:info|information|docs?|documentation|sources?|references?|version|api)\b|\bcurrent\s+(?:[a-z0-9_-]+\s+){0,3}(?:data|datasets?|prices?|pricing|news|schedules?|versions?|changelogs?|migration\s+guides?|release\s+notes?)\b|\b(?:latest|newest|current|recent|most\s+recent|up[- ]to[- ]date)\s+(?:package\s+version|changelogs?|migration\s+guides?|release\s+notes?)\b|\bcurrent\s+external\s+(?:data|sources?|evidence|information)\b"),
    ("official_website", r"\b(?:official|unofficial)\s+(?:websites?|sites?|docs?|documentation)\b"),
    ("github_source", r"\b(?:research|search|find|check|use|review|compare|cite|source|sources|external)\b[^\n]{0,80}(?<![.\w-])github\b(?!\s+actions\b)|(?<![.\w-])github\b(?!\s+actions\b)[^\n]{0,80}\b(?:repos?|repositories|sources?|references?|examples?|projects?|algorithms?|research)\b"),
    ("forum_source", r"\bforums?\b"),
    ("non_english_source", r"\b(?:chinese|russian|non[- ]english)\s+(?:websites?|sites?|sources?|forums?|docs?|documentation)?\b"),
    ("paper_identifier", r"\b(?:ssrn|arxiv|doi|research papers?|papers?)\b"),
    ("citation_or_reference", r"\b(?:citations?|cite)\b|\b(?:external|cited|research)\s+references?\b|\breferences?\s+(?:used|cited|consulted|for\s+(?:research|external|current|latest))\b"),
    ("source_evidence", r"\b(?:cite|cited|citation|research|researching)\b[^\n]{0,80}\bsources?\b|\b(?:search|find)\b[^\n]{0,80}\b(?:external|online|web|internet|current|latest|research|citation)\b[^\n]{0,80}\bsources?\b|\b(?:search|find)\b[^\n]{0,80}\bsources?\b[^\n]{0,80}\b(?:external|online|web|internet|current|latest|research|citation)\b|\balgorithm\s+search\b[^\n]{0,80}\bsources?\b|\b(?:external|online|current|latest|research|citation)\s+sources?\b|\bsources?\s+(?:used|cited|consulted|checked|for\s+(?:current|latest|external|research|citation|api))\b|\bsource\s+evidence\b"),
]

RESEARCH_WEAK_SIGNAL_PATTERNS = [
    ("docs", r"\b(?:docs?|documentation)\b"),
    ("compare", r"\b(?:compare|comparison)\b"),
    ("best_possible", r"\bbest possible\b"),
    ("algorithm", r"\balgorithms?\b"),
    ("references", r"\breferences?\b"),
    ("analysis_output", r"\banalysis\.md\b"),
    ("handoff_output", r"\bhandoff\.md\b|\bprogrammer handoff\b|\bhandoff\b"),
    ("planning_output", r"\b(?:project[_ -]?plan|master[_ -]?plan|research[_ -]?plan)\.md\b"),
]

RESEARCH_WEAK_OUTPUT_NAMES = {
    "project_plan.md",
    "research_plan.md",
    "master_plan.md",
    "handoff.md",
    "analysis.md",
}

RESEARCH_NEGATABLE_STRONG_SIGNALS = {
    "explicit_research",
    "web_search",
    "online_sources",
    "external_evidence",
    "current_info",
    "official_website",
    "github_source",
    "forum_source",
    "non_english_source",
    "paper_identifier",
    "citation_or_reference",
    "source_evidence",
}

VERIFICATION_PHASE_BASELINE = "baseline"
VERIFICATION_PHASE_POST_CHANGE = "post_change"
VERIFICATION_PHASE_FINAL = "final"
VERIFICATION_STATUS_PENDING = "pending"
VERIFICATION_STATUS_PASSED = "passed"
VERIFICATION_STATUS_FAILED = "failed"
VERIFICATION_STATUS_BLOCKED = "blocked"
VERIFICATION_STATUS_WAIVED = "waived"
VERIFICATION_STATUS_NOT_APPLICABLE = "not_applicable"
VERIFICATION_STATUS_MIXED = "mixed"

VERIFICATION_CHECK_STATUSES = {
    VERIFICATION_STATUS_PENDING,
    VERIFICATION_STATUS_PASSED,
    VERIFICATION_STATUS_FAILED,
    VERIFICATION_STATUS_BLOCKED,
    VERIFICATION_STATUS_WAIVED,
    VERIFICATION_STATUS_NOT_APPLICABLE,
}

REVIEW_VERDICT_APPROVED = "APPROVED"
REVIEW_VERDICT_NEEDS_REVISION = "NEEDS_REVISION"
REVIEW_STATUS_APPROVED = "approved"
REVIEW_STATUS_NEEDS_REVISION = "needs_revision"
REVIEW_STATUS_MALFORMED = "malformed"
REVIEW_STATUS_STALE = "stale"

QUALITY_MODE_BEST = "best"
QUALITY_MODE_STANDARD = "standard"
QUALITY_MODES = {QUALITY_MODE_BEST, QUALITY_MODE_STANDARD}
DEFAULT_QUALITY_MODE = QUALITY_MODE_BEST
QUALITY_MATERIAL_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM"}
QUALITY_NON_MATERIAL_TERMS = (
    "non-material",
    "non material",
    "non-blocking",
    "non blocking",
    "no corresponding",
    "no unresolved",
    "cosmetic",
    "typo",
    "formatting",
    "style only",
    "does not affect",
    "no correctness impact",
    "no material impact",
    "not material",
    "not a defect",
    "no revision required",
    "safeguards remain",
    "safeguard remains",
    "safeguards are present",
    "safeguard is present",
    "restricted to",
    "shadow test",
    "no production-readiness",
    "no production readiness",
    "defect",
)
QUALITY_ACCEPTED_RISK_TERMS = (
    "accepted risk",
    "accepted-risk",
    "documented risk",
    "real blocker",
    "blocked by",
    "cannot be fixed",
    "cannot fix",
    "waived because",
    "explicit waiver",
)
QUALITY_ACCEPTED_RISK_NEGATIVE_PATTERNS = (
    r"\bno\s+accepted[-\s]risk(?:\s+waiver)?\b",
    r"\bno\s+risk\s+waiver\b",
    r"\bno\s+explicit\s+waiver\b",
    r"\bnot\s+accepted[-\s]risk\b",
    r"\bwithout\s+accepted[-\s]risk\b",
    r"\baccepted[-\s]risk\s*[:\-]?\s*(?:none|n/a|na|not applicable|no)\b",
)
QUALITY_NON_MATERIAL_NEGATIVE_PATTERNS = (
    r"\bnot\s+non[-\s]material\b",
    r"\bno\s+non[-\s]material\s+rationale\b",
    r"\bwithout\s+non[-\s]material\s+rationale\b",
    r"\bnon[-\s]material\s*[:\-]?\s*(?:none|n/a|na|not applicable|no)\b",
)
QUALITY_ACTIONABLE_TERMS = (
    "fix",
    "missing",
    "incomplete",
    "incorrect",
    "wrong",
    "unsupported",
    "unclear",
    "ambiguous",
    "stale",
    "unverified",
    "source",
    "reference",
    "evidence",
    "test",
    "verification",
    "edge case",
    "failure mode",
    "bug",
    "risk",
    "should",
    "must",
    "needs",
    "required",
    "implement",
    "update",
)
QUALITY_RATIONALE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "because",
    "by",
    "check",
    "checks",
    "does",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "its",
    "not",
    "of",
    "on",
    "only",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}

ISSUE_STATUS_OPEN = "open"
ISSUE_STATUS_ADDRESSED = "addressed"
ISSUE_STATUS_PARTIALLY_ADDRESSED = "partially_addressed"
ISSUE_STATUS_CLAIMED_ACCEPTED_RISK = "claimed_accepted_risk"
ISSUE_STATUS_CLAIMED_NON_MATERIAL = "claimed_non_material"
ISSUE_STATUS_RESOLVED = "resolved"
ISSUE_STATUS_ACCEPTED_RISK = "accepted_risk"
ISSUE_STATUS_NON_MATERIAL = "non_material"
ISSUE_OPEN_STATUSES = {
    ISSUE_STATUS_OPEN,
    ISSUE_STATUS_ADDRESSED,
    ISSUE_STATUS_PARTIALLY_ADDRESSED,
    ISSUE_STATUS_CLAIMED_ACCEPTED_RISK,
    ISSUE_STATUS_CLAIMED_NON_MATERIAL,
}
ISSUE_CLOSED_STATUSES = {
    ISSUE_STATUS_RESOLVED,
    ISSUE_STATUS_ACCEPTED_RISK,
    ISSUE_STATUS_NON_MATERIAL,
}
ISSUE_STATUS_ALIASES = {
    "fixed": ISSUE_STATUS_ADDRESSED,
    "addressed": ISSUE_STATUS_ADDRESSED,
    "resolved": ISSUE_STATUS_RESOLVED,
    "partially fixed": ISSUE_STATUS_PARTIALLY_ADDRESSED,
    "partially addressed": ISSUE_STATUS_PARTIALLY_ADDRESSED,
    "partially resolved": ISSUE_STATUS_PARTIALLY_ADDRESSED,
    "not fixed": ISSUE_STATUS_OPEN,
    "not addressed": ISSUE_STATUS_OPEN,
    "still present": ISSUE_STATUS_OPEN,
    "open": ISSUE_STATUS_OPEN,
    "accepted risk": ISSUE_STATUS_ACCEPTED_RISK,
    "accepted_risk": ISSUE_STATUS_ACCEPTED_RISK,
    "non material": ISSUE_STATUS_NON_MATERIAL,
    "non-material": ISSUE_STATUS_NON_MATERIAL,
    "non_material": ISSUE_STATUS_NON_MATERIAL,
}

EXCELLENCE_STATUS_PASS = "PASS"
EXCELLENCE_STATUS_FAIL = "FAIL"
EXCELLENCE_STATUS_WAIVED = "WAIVED"
EXCELLENCE_STATUS_UNKNOWN = "UNKNOWN"
EXCELLENCE_STATUSES = {
    EXCELLENCE_STATUS_PASS,
    EXCELLENCE_STATUS_FAIL,
    EXCELLENCE_STATUS_WAIVED,
    EXCELLENCE_STATUS_UNKNOWN,
}
EXCELLENCE_CHECKLIST_ITEMS = (
    ("original_request_answered", "Original request fully answered", ("original request", "fully answered")),
    ("deliverable_directly_usable", "Deliverable directly usable", ("directly usable", "deliverable usable")),
    ("material_issues_resolved", "Material issues resolved", ("material issues", "issues resolved")),
    ("claims_sourced_or_assumptions_labeled", "Claims sourced or assumptions labeled", ("claims sourced", "assumptions labeled")),
    ("implementation_details_present", "Implementation details present", ("implementation details", "details present")),
    ("edge_cases_failure_modes_covered", "Edge cases and failure modes covered", ("edge cases", "failure modes")),
    ("verification_test_strategy_specific", "Verification/test strategy specific", ("verification", "test strategy")),
    ("internally_consistent", "Internally consistent", ("internally consistent", "internal consistency")),
    ("no_raw_secrets", "No raw secrets included", ("raw secrets", "secrets included")),
    ("best_practical_version", "Best practical version", ("best practical", "best possible")),
)
EXCELLENCE_CHECKLIST_KEYS = {item[0] for item in EXCELLENCE_CHECKLIST_ITEMS}

REVIEW_REQUIRED_SECTION_ALIASES = {
    "findings": ("findings", "issues", "review findings", "findings and issues"),
    "evidence": ("evidence reviewed", "evidence", "files reviewed", "review evidence"),
    "commands": ("commands/tests run", "commands and tests run", "commands run", "tests run", "checks run"),
    "output_hashes": ("output hashes", "requested output hashes", "hashes reviewed", "output hashes reviewed"),
    "verification": ("verification evidence", "congress verification", "verification summary"),
    "waived": ("waived checks", "waivers", "checks waived", "waived or skipped checks"),
    "unresolved": ("unresolved issues", "remaining issues", "open issues", "unresolved findings"),
}

REVIEW_SECTION_LABELS = {
    "findings": "findings/issues",
    "evidence": "evidence reviewed",
    "commands": "commands/tests run",
    "output_hashes": "output hashes",
    "verification": "verification evidence",
    "waived": "waived checks",
    "unresolved": "unresolved issues",
}

PHASE_STARTUP = "startup"
PHASE_RESEARCHER_RUNNING = "researcher_running"
PHASE_RESEARCHER_DONE = "researcher_done"
PHASE_INSPECTOR_RUNNING = "inspector_running"
PHASE_INSPECTOR_DONE = "inspector_done"
PHASE_INSPECTOR_2_RUNNING = "inspector_2_running"
PHASE_INSPECTOR_2_DONE = "inspector_2_done"
PHASE_FINALIZING = "finalizing"
PHASE_TERMINAL = "terminal"
PHASE_UNKNOWN = "unknown"

CONTROLLED_PHASES = {
    PHASE_STARTUP,
    PHASE_RESEARCHER_RUNNING,
    PHASE_RESEARCHER_DONE,
    PHASE_INSPECTOR_RUNNING,
    PHASE_INSPECTOR_DONE,
    PHASE_INSPECTOR_2_RUNNING,
    PHASE_INSPECTOR_2_DONE,
    PHASE_FINALIZING,
    PHASE_TERMINAL,
    PHASE_UNKNOWN,
}

STATUS_RUNNING = "running"
STATUS_APPROVED = "approved"
STATUS_APPROVED_WITH_WAIVER = "approved_with_waiver"
STATUS_MAX_ROUNDS_UNAPPROVED = "max_rounds_unapproved"
STATUS_BLOCKED_NEEDS_USER = "blocked_needs_user"
STATUS_BLOCKED_NEEDS_ENVIRONMENT = "blocked_needs_environment"
STATUS_VERIFICATION_FAILED = "verification_failed"
STATUS_INTERRUPTED_RESUMABLE = "interrupted_resumable"
STATUS_REVIEW_FAILED = "review_failed"
STATUS_INSPECTOR_FAILED = "inspector_failed"
STATUS_FAILED_NONRESUMABLE = "failed_nonresumable"
STATUS_QUIT_BY_USER = "quit_by_user"
STATUS_OUTPUT_EARLY = "output_early"
STATUS_INTERNAL_ERROR = "internal_error"

APPROVED_STATUSES = {
    STATUS_APPROVED,
    STATUS_APPROVED_WITH_WAIVER,
}

TERMINAL_STATUSES = {
    STATUS_APPROVED,
    STATUS_APPROVED_WITH_WAIVER,
    STATUS_MAX_ROUNDS_UNAPPROVED,
    STATUS_BLOCKED_NEEDS_USER,
    STATUS_BLOCKED_NEEDS_ENVIRONMENT,
    STATUS_VERIFICATION_FAILED,
    STATUS_INTERRUPTED_RESUMABLE,
    STATUS_REVIEW_FAILED,
    STATUS_INSPECTOR_FAILED,
    STATUS_FAILED_NONRESUMABLE,
    STATUS_QUIT_BY_USER,
    STATUS_OUTPUT_EARLY,
    STATUS_INTERNAL_ERROR,
}

CONTROLLED_STATUSES = TERMINAL_STATUSES | {STATUS_RUNNING}

STATUS_EXIT_CODES = {
    STATUS_APPROVED: 0,
    STATUS_APPROVED_WITH_WAIVER: 0,
    STATUS_FAILED_NONRESUMABLE: 1,
    STATUS_MAX_ROUNDS_UNAPPROVED: 2,
    STATUS_BLOCKED_NEEDS_USER: 3,
    STATUS_BLOCKED_NEEDS_ENVIRONMENT: 4,
    STATUS_VERIFICATION_FAILED: 5,
    STATUS_INTERRUPTED_RESUMABLE: 6,
    STATUS_QUIT_BY_USER: 6,
    STATUS_OUTPUT_EARLY: 6,
    STATUS_REVIEW_FAILED: 7,
    STATUS_INSPECTOR_FAILED: 7,
    STATUS_INTERNAL_ERROR: 8,
}

KNOWN_MANAGED_ARTIFACTS = [
    SESSION_REQUEST_FILE,
    RESEARCHER_UPDATED_FILE,
    INSPECTOR_COMMENTS_FILE,
    INSPECTOR_2_COMMENTS_FILE,
    CONGRESS_ROUNDS_DIR,
    CONGRESS_STATE_FILE,
    CONGRESS_RESULT_FILE,
    CONGRESS_HISTORY_FILE,
    CONGRESS_VERIFICATION_FILE,
    CONGRESS_BLOCKED_FILE,
    CONGRESS_LOCK_FILE,
    CONGRESS_LOCK_TAKEOVER_FILE,
    "logs",
    ".git",
    "__pycache__",
]

MANAGED_DIRECTORY_ARTIFACTS = {
    CONGRESS_ROUNDS_DIR,
    "logs",
    ".git",
    "__pycache__",
}

MANAGED_FILE_ARTIFACTS = {
    SESSION_REQUEST_FILE,
    RESEARCHER_UPDATED_FILE,
    INSPECTOR_COMMENTS_FILE,
    INSPECTOR_2_COMMENTS_FILE,
    CONGRESS_STATE_FILE,
    CONGRESS_RESULT_FILE,
    CONGRESS_HISTORY_FILE,
    CONGRESS_VERIFICATION_FILE,
    CONGRESS_BLOCKED_FILE,
    CONGRESS_LOCK_FILE,
    CONGRESS_LOCK_TAKEOVER_FILE,
}

CRITICAL_MANAGED_FILE_ARTIFACTS = {
    SESSION_REQUEST_FILE,
    RESEARCHER_UPDATED_FILE,
    INSPECTOR_COMMENTS_FILE,
    INSPECTOR_2_COMMENTS_FILE,
    CONGRESS_STATE_FILE,
    CONGRESS_RESULT_FILE,
    CONGRESS_HISTORY_FILE,
    CONGRESS_VERIFICATION_FILE,
    CONGRESS_BLOCKED_FILE,
}

CRITICAL_MANAGED_DIRECTORY_ARTIFACTS = {
    CONGRESS_ROUNDS_DIR,
    "logs",
}

# Backward-compatible symbol for older probes/importers. Active rate-limit
# classification uses strict regex patterns below, not broad substring signals.
RATE_LIMIT_SIGNALS = ()

# Real API rate / usage-cap patterns (need pause + retry).
# Intentionally excludes bare "rate_limit", "429", and "billing" strings.
RATE_LIMIT_PATTERNS = (
    r"\brate limit reached\b",
    r"\bhit your usage limit\b",
    r"\busage limit reached\b",
    r"\btoo many requests\b",
    r"\bhttp\s*429\b",
    r"\bstatus\s*429\b",
    r"\b429\s+too many requests\b",
    r"\btokens per min\b",
    r"\brequests per min\b",
    r"\bquota exceeded\b",
    r'["\']code["\']\s*:\s*["\']rate_limit["\']',
    r"\bcode\s*:\s*rate_limit\b",
    r"\berror code\s+rate_limit\b",
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

_STDOUT_BROKEN = False


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
- You are a real full-access Codex agent. You may inspect files, run real project commands, start/use local tools or services when needed, and gather evidence needed to make the requested deliverables production-ready.

CRITICAL RULES:
1. Read the original request, session_request.md, all Tier 1 required source/input files, every requested output file, and prior Congress-managed docs exactly as instructed every round. Use optional source-context manifest files as needed for a complete answer; do not mechanically read unrelated optional files.
2. Create or update the requested deliverable files and any necessary support files required to make those deliverables complete, runnable, and verifiable. If an output file already exists, read it fully before editing it. List every support file changed in your notes.
3. Do NOT treat stdout as the final deliverable. Stdout must explain what you changed, why you changed it, and which output files were affected.
4. Use real files, real commands, and real environment checks when needed. Do not claim a command/test/build passed unless you actually ran it or clearly state why it was not run.
5. If the task involves code, make it complete, runnable, well-commented, and secure.
6. If analyzing something, be exhaustive and precise.
7. Structure your response clearly with labeled sections.
8. Always explain your reasoning, trade-offs considered, and alternatives rejected.
9. Do NOT modify Congress-managed files yourself unless explicitly instructed: researcher_updated.md, inspector_comments.md, inspector_2_comments.md, session_request.md, congress_state.json, congress_result.md, congress_history.md, congress_verification.md, congress_blocked.md, congress.lock, congress_rounds/.
10. DO NOT wrap your entire response in a markdown code block — write it as plain structured text.
11. Do NOT emit JSON or JSONL as your Congress response contract. Use readable Markdown/plain text notes.
12. If a required user decision, credential, server, UI action, paid API, or external dependency is genuinely missing, do not guess around it. Add a clear Markdown section titled BLOCKED that explains exactly what is needed, what you verified, and what can resume later.
13. In best-output mode, material Inspector feedback is required revision context. Address it directly before seeking approval.
14. When prior Inspector comments exist, include a section titled "## Inspector Issues Addressed" that explains each material issue as Fixed, Partially fixed, Not fixed, Accepted risk, or Non-material with concrete rationale.

OUTPUT FORMAT:
- UNDERSTANDING: Brief summary of what you understood the task to be.
- OUTPUT FILE CHANGES: For each requested output file, state whether you created, updated, or intentionally left it unchanged, and why. Also list every necessary support file you created or modified, with why it was required.
- COMMANDS / EVIDENCE: List real commands, tests, builds, file checks, or environment checks you ran, with outcomes. If none were needed, say why.
- RESEARCH / REASONING: Your detailed solution/analysis.
- INSPECTOR ISSUES ADDRESSED: When Inspector feedback exists, list how each material issue was handled.
- Refrences or ASSUMPTIONS: List all refrence or any assumptions you made (and explain why you did not found any refrence for this aasumption).
"""

INSPECTOR_SYSTEM_PROMPT_TEMPLATE = """You are the INSPECTOR/SUPERVISOR agent in a multi-agent AI system called "Congress".

YOUR ROLE:
- You are a ruthless but fair reviewer, security auditor, and quality inspector.
- You receive the ORIGINAL user request, the requested output files on disk, and the RESEARCHER's explanation report.
- Your job is to find EVERY flaw, bug, security issue, logic error, missed edge case, and deliverable gap in the requested output files. 
- You are a real full-access Codex agent. You may inspect files, run real tests/builds/scripts/environment checks, inspect logs, and create temporary verification artifacts when needed to verify the work honestly.

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
1. Inspect requested deliverables directly from disk before relying on researcher_updated.md. The Researcher's notes are context, not proof.
2. Read every required file from disk exactly as instructed every round, including the requested output files, session_request.md, Tier 1 required source/input files, researcher_updated.md, and prior inspector comments when available. Use optional source context as needed for review confidence; do not treat optional manifest entries as a mandatory full-read list.
3. Be SPECIFIC. Don't say "improve error handling" — say exactly WHERE and HOW. (and how can check and what avoid)
4. Run real commands/tests/builds/scripts/environment checks when they are relevant and available. Report exact command outcomes. If a relevant check cannot be run, explain why.
5. Rate the severity of each issue: CRITICAL / HIGH / MEDIUM / LOW.
6. You may create temporary verification artifacts if needed, but do NOT edit the requested deliverables or Congress-managed files. Clean up temporary artifacts unless they are useful evidence and you clearly name them.
7. Do NOT emit JSON or JSONL as your review contract. Use readable Markdown/plain text.
8. At the VERY END of your review, on its own line, you MUST write exactly one of:
   VERDICT: NEEDS_REVISION
   VERDICT: APPROVED
   Use NEEDS_REVISION if any CRITICAL, HIGH, or MEDIUM material issue remains.
   Use NEEDS_REVISION if any actionable issue would materially improve the requested output.
   Use APPROVED only when the deliverable has no material unresolved issues and is as close as practical to the best possible answer.
   LOW issues may be approved only if they are cosmetic or truly non-material, and your review explains why.
9. Your job is not to be nice or fast. Your job is to prevent weak output from being approved.

OUTPUT FORMAT:
## Summary
Brief overall assessment (2-3 sentences).

## Issues Found
For each issue:
- [SEVERITY] Issue title
- Location: where exactly
- Problem: what's wrong
- Fix: specific fix with code

## Evidence Checked
- Files read from disk
- Commands/tests/builds/scripts run and their outcomes
- Checks that could not be run and why

## Improvement Suggestions
Actionable improvements ranked by impact.

## Verdict
VERDICT: NEEDS_REVISION or VERDICT: APPROVED
"""


# ============================================================================
# REDACTION HELPERS
# ============================================================================

def _redact_sensitive_text(text: str) -> str:
    """Mask common secrets in Congress logs and metadata."""
    if text is None:
        return ""
    redacted = str(text)
    redacted = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "-----BEGIN REDACTED PRIVATE KEY-----",
        redacted,
        flags=re.IGNORECASE | re.DOTALL,
    )
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{12,})",
        "[REDACTED_TOKEN]",
        redacted,
    )
    redacted = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
        r"\1[REDACTED]:[REDACTED]@",
        redacted,
    )
    sensitive_name = (
        r"[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|"
        r"ACCESS[_-]?KEY|AUTHORIZATION)[A-Z0-9_]*"
    )
    redacted = re.sub(
        rf"(?i)\b({sensitive_name})(\s*[:=]\s*)(['\"]?)([^'\"\s,;]+)(['\"]?)",
        r"\1\2\3[REDACTED]\5",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(password|passwd|api[_-]?key|token|secret)(\s*[:=]\s*)(['\"]?)([^'\"\s,;]+)(['\"]?)",
        r"\1\2\3[REDACTED]\5",
        redacted,
    )
    return redacted


def _redact_sensitive_data(value):
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, list):
        return [_redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_data(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_sensitive_data(item) for key, item in value.items()}
    return value


def _extract_credential_names(text: str) -> list[str]:
    if not text:
        return []
    names = set()
    patterns = [
        r'\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASS|KEY|CREDENTIAL)[A-Z0-9_]*)\s*=',
        r'\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASS|KEY|CREDENTIAL)[A-Z0-9_]*)\b',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            names.add(match.group(1))
    return sorted(names)


def _sanitize_user_update(text: str, source: str = "user") -> dict:
    raw = str(text or "").strip()
    redacted = _redact_sensitive_text(raw)
    credential_names = _extract_credential_names(raw)
    redacted_secret = redacted != raw or bool(credential_names)
    if redacted_secret and redacted == raw:
        redacted = "[REDACTED secret-like user update]"
    update = {
        "timestamp": _utc_timestamp(),
        "source": source,
        "text": redacted,
        "redacted": redacted_secret,
        "credential_names": credential_names,
    }
    if redacted_secret:
        update["warning"] = (
            "Secret-like content was redacted. Provide raw credentials through "
            "environment variables or the relevant external credential mechanism."
        )
    return update


def _format_user_update(update: dict) -> str:
    ts = update.get("timestamp") or "unknown-time"
    source = update.get("source") or "user"
    text = update.get("text") or ""
    line = f"- {ts} [{source}] {text}"
    names = update.get("credential_names") or []
    if names:
        line += f" (credential names mentioned: {', '.join(names)})"
    if update.get("warning"):
        line += f"\n  - Note: {update.get('warning')}"
    return line


def _render_user_updates_section(user_updates: list[dict] | None) -> str:
    if not user_updates:
        return ""
    lines = [
        "## User Updates",
        "The following sanitized user updates were added after the original request. Read these before acting.",
    ]
    lines.extend(_format_user_update(update) for update in user_updates)
    return "\n".join(lines) + "\n\n"


def _append_history_note(working_dir: str, title: str, lines: list[str]) -> None:
    path = os.path.join(working_dir, CONGRESS_HISTORY_FILE)
    safe_lines = [_redact_sensitive_text(str(line)) for line in lines]
    text = "\n".join([f"## {_utc_timestamp()} - {title}", "", *safe_lines, ""])
    _append_text(path, text)


def _host_port_from_url(url: str) -> tuple[str, int | None]:
    parsed = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or ""
    port = parsed.port
    if port is None:
        if parsed.scheme == "https":
            port = 443
        elif parsed.scheme == "http":
            port = 80
    return host, port


def _probe_tcp(host: str, port: int, timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tool_names_for_browser() -> list[str]:
    if os.name == "nt":
        return ["chrome", "chrome.exe", "msedge", "msedge.exe", "firefox", "firefox.exe"]
    return ["google-chrome", "chromium", "chromium-browser", "firefox"]


def _clean_requirement_name(value: str) -> str:
    cleaned = str(value or "").strip().strip("`'\"")
    while cleaned and cleaned[-1] in ".,;:!?)]}\"'":
        cleaned = cleaned[:-1]
    while cleaned and cleaned[0] in "([`'\"":
        cleaned = cleaned[1:]
    return cleaned


_PREFLIGHT_NAME_STOPWORDS = {
    "a",
    "an",
    "after",
    "app",
    "before",
    "binary",
    "can",
    "command",
    "connector",
    "continue",
    "database",
    "for",
    "mcp",
    "need",
    "needs",
    "required",
    "requires",
    "service",
    "support",
    "the",
    "to",
    "tool",
    "using",
    "with",
    "work",
}


def _valid_requirement_name(value: str) -> str:
    cleaned = _clean_requirement_name(value)
    if not cleaned:
        return ""
    if cleaned.lower() in _PREFLIGHT_NAME_STOPWORDS:
        return ""
    return cleaned


# ============================================================================
# LOGGING SYSTEM
# ============================================================================

class CongressLogger:
    """Comprehensive logging system that captures everything."""

    def __init__(self, session_id: str, prompt_log_mode: str = DEFAULT_LOG_PROMPTS_MODE):
        self.session_id = session_id
        self.prompt_log_mode = prompt_log_mode if prompt_log_mode in LOG_PROMPTS_MODES else DEFAULT_LOG_PROMPTS_MODE
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
                json.dump(_redact_sensitive_data(self.meta), f, indent=2, default=str)
        except OSError as e:
            _print_safe(f"  {C_DIM}[LOG WARNING] Could not save meta: {e}{C_RESET}")

    def _append(self, filepath: str, text: str):
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(_redact_sensitive_text(text))
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

    def _prompt_for_log(self, prompt: str) -> str:
        if self.prompt_log_mode == "off":
            return "[prompt logging disabled by --log-prompts=off]"
        if self.prompt_log_mode == "redacted":
            return _redact_sensitive_text(prompt)
        return prompt

    def log_agent_start(self, agent: str, round_num, prompt: str):
        self.log_master(agent.upper(), f"Round {round_num} - Started")
        log_path = (self.researcher_log_path if agent.startswith("researcher")
                    else self.inspector_log_path)
        prompt_for_log = self._prompt_for_log(prompt)
        self._append(log_path,
                     f"\n{'=' * 60}\n"
                     f"ROUND {round_num} - STARTED at {datetime.now().strftime('%H:%M:%S')}\n"
                     f"{'=' * 60}\n"
                     f"PROMPT SENT ({len(prompt)} chars, mode={self.prompt_log_mode}):\n"
                     f"{'─' * 40}\n{prompt_for_log}\n{'─' * 40}\n\n")

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
                f.write(
                    _redact_sensitive_text(
                        f"{'=' * 60}\nOUTPUT:\n{'=' * 60}\n{stdout}\n"
                    )
                )
                if stderr.strip():
                    f.write(
                        _redact_sensitive_text(
                            f"\n{'=' * 60}\nSTDERR:\n{'=' * 60}\n{stderr}\n"
                        )
                    )
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
                f.write(_redact_sensitive_text(final_output))
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
        _stdout_write_safe("\r" + " " * (self._width - 1) + "\r")

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
        _print_safe(f"  {C_DIM}[{ts}]{C_RESET} {color}{C_BOLD}{message}{C_RESET}")

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
        _print_safe(f"  {C_RED}{C_BOLD}[ERROR]{C_RESET} {C_RED}{message}{C_RESET}")


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
    except (OSError, ValueError):
        pass


def _stdout_write_safe(text: str, *, flush: bool = True) -> bool:
    try:
        sys.stdout.write(text)
        if flush:
            sys.stdout.flush()
        return True
    except (UnicodeEncodeError, OSError, ValueError):
        try:
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
                if flush:
                    sys.stdout.flush()
                return True
        except (OSError, ValueError):
            pass
    return False


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


def _has_context_limit_signal(text: str) -> bool:
    text_lower = text.lower() if text else ""
    return any(sig in text_lower for sig in CONTEXT_LIMIT_SIGNALS)


def _is_context_limit(stderr: str) -> bool:
    """Return True if stderr indicates context-window / output-token exhaustion.
    This is NOT a rate limit — no pause or retry needed, just accept partial output."""
    return _matches_context_limit_error(stderr)


def _stderr_error_lines(stderr: str) -> str:
    """Return only stderr lines that look relevant for error classification."""
    kept: list[str] = []
    in_transcript_block = False
    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower in {"user", "assistant", "system", "developer"}:
            in_transcript_block = True
            continue
        if in_transcript_block and re.fullmatch(r"-{3,}", stripped):
            in_transcript_block = False
            continue
        starts_error = lower.startswith(("error", "fatal", "traceback"))
        timestamped_error = bool(re.match(r"^\d{4}-\d{2}-\d{2}t\S+\s+(error|fatal)\b", lower))
        if in_transcript_block and not timestamped_error:
            continue
        if timestamped_error:
            in_transcript_block = False
        rate_or_usage = any(
            re.search(pattern, stripped, re.IGNORECASE)
            for pattern in RATE_LIMIT_PATTERNS
        )
        context_error = _has_context_limit_signal(stripped)
        network_error = any(sig in lower for sig in NETWORK_ERROR_SIGNALS)
        known_codex_warning = (
            "failed to refresh available models" in lower
            or "failed to record rollout items" in lower
        )
        if (
            starts_error
            or timestamped_error
            or rate_or_usage
            or context_error
            or network_error
            or known_codex_warning
        ):
            kept.append(stripped)
    return "\n".join(kept)


def _matches_rate_limit_error(stderr: str) -> bool:
    """Return True only for actual API/usage rate-limit error lines."""
    error_text = _stderr_error_lines(stderr)
    if not error_text.strip():
        return False
    return any(re.search(pattern, error_text, re.IGNORECASE) for pattern in RATE_LIMIT_PATTERNS)


def _matches_context_limit_error(stderr: str) -> bool:
    """Return True only for actual context-window error lines."""
    error_text = _stderr_error_lines(stderr)
    if not error_text.strip():
        return False
    return _has_context_limit_signal(error_text)


def _is_network_error(stderr: str, rc: int) -> bool:
    """Return True if stderr indicates a network-level error (not rate/context limit)."""
    if rc == 0:
        return False
    # Don't re-classify rate-limit or context-limit errors as network errors
    if _matches_rate_limit_error(stderr):
        return False
    error_text = _stderr_error_lines(stderr)
    if not error_text.strip():
        return False
    if _has_context_limit_signal(error_text):
        return False
    error_text_lower = error_text.lower()
    return any(sig in error_text_lower for sig in NETWORK_ERROR_SIGNALS)


def _is_rate_limited(stderr: str, rc: int) -> tuple[bool, str]:
    """Return (is_limited, retry_info_string).
    Context-window exhaustion is explicitly excluded — that is not a rate limit."""
    # Context limit takes priority — must not be misclassified as rate limit
    if _matches_context_limit_error(stderr):
        return False, ""
    if rc == 0:
        return False, ""
    if _matches_rate_limit_error(stderr):
        retry_match = re.search(r'try again at\s+(.+?)[\.\n]', stderr, re.IGNORECASE)
        retry_info  = f" Retry after: {retry_match.group(1)}" if retry_match else ""
        return True, retry_info
    return False, ""


def _workspace_root(working_dir: str) -> str:
    return os.path.abspath(working_dir)


def _workspace_real_root(working_dir: str) -> str:
    return os.path.realpath(_workspace_root(working_dir))


def _workspace_abs_path(working_dir: str, relpath: str) -> str:
    return os.path.abspath(os.path.join(working_dir, relpath.replace("/", os.sep)))


def _path_within(parent: str, child: str) -> bool:
    try:
        parent_norm = os.path.normcase(parent)
        child_norm = os.path.normcase(child)
        return os.path.commonpath([parent_norm, child_norm]) == parent_norm
    except ValueError:
        return False


def _relpath_key(relpath: str) -> str:
    return relpath.replace("\\", "/").strip("/").casefold()


def _hardlink_count(path: str) -> int | None:
    try:
        return getattr(os.stat(path), "st_nlink", 1)
    except OSError:
        return None


def _hardlink_error(path: str) -> str | None:
    count = _hardlink_count(path)
    if count is not None and count > 1:
        return f"existing output file has multiple hard links ({count})"
    return None


def _normalize_workspace_relpath(working_dir: str, raw_path: str) -> str:
    raw = raw_path.strip().strip('"').strip("'")
    if not raw:
        raise ValueError("Output file names cannot be empty.")
    if raw.replace("\\", "/").endswith("/"):
        raise ValueError(f"Output path must point to a file, not a directory: {raw_path}")

    candidate = raw if os.path.isabs(raw) else os.path.join(working_dir, raw)
    abs_path = os.path.abspath(os.path.normpath(candidate))
    root = _workspace_root(working_dir)
    if not _path_within(root, abs_path):
        raise ValueError(f"Output path must stay inside the working directory: {raw_path}")

    real_root = _workspace_real_root(working_dir)
    real_path = os.path.realpath(abs_path)
    if not _path_within(real_root, real_path):
        raise ValueError(f"Output path resolves outside the working directory: {raw_path}")

    relpath = os.path.relpath(abs_path, root).replace("\\", "/")
    if relpath in (".", ""):
        raise ValueError(f"Output path must point to a file, not the working directory: {raw_path}")
    if not os.path.basename(relpath):
        raise ValueError(f"Output path must include a file name: {raw_path}")
    if os.path.isdir(abs_path):
        raise ValueError(f"Output path points to a directory, not a file: {raw_path}")
    hardlink_error = _hardlink_error(abs_path) if os.path.isfile(abs_path) else None
    if hardlink_error:
        raise ValueError(f"Output path is unsafe: {raw_path} ({hardlink_error})")
    return relpath


def _reserved_output_relpaths() -> set[str]:
    reserved = {_relpath_key(item) for item in KNOWN_MANAGED_ARTIFACTS}
    for item in MANAGED_FILE_ARTIFACTS:
        reserved.add(_relpath_key(item + ".tmp"))
    return reserved


def _is_reserved_output_relpath(relpath: str) -> bool:
    normalized = _relpath_key(relpath)
    if normalized in _reserved_output_relpaths():
        return True
    for prefix in KNOWN_MANAGED_ARTIFACTS:
        prefix_norm = _relpath_key(prefix)
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
        key = _relpath_key(relpath)
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
        invalid_reason = "not a regular file" if invalid_type else None
        link_count = None
        if exists:
            try:
                stat = os.stat(abs_path)
                size = stat.st_size
                mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
                link_count = getattr(stat, "st_nlink", 1)
                if link_count > 1:
                    invalid_type = True
                    exists = False
                    invalid_reason = f"multiple hard links ({link_count})"
            except OSError:
                exists = False
                size = None
                mtime_ns = None
                invalid_reason = "stat failed"
        statuses.append({
            "path": relpath,
            "exists": exists,
            "invalid_type": invalid_type,
            "invalid_reason": invalid_reason,
            "link_count": link_count,
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


def _utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _status_to_exit_code(status: str) -> int:
    return STATUS_EXIT_CODES.get(status, 8)


def _status_is_approved(status: str) -> bool:
    return status in APPROVED_STATUSES


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _file_sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@dataclass
class CongressResult:
    status: str
    exit_code: int
    approved: bool
    blocking_reasons: list[str] = field(default_factory=list)
    output_files: list[dict] = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    reviews: dict = field(default_factory=dict)
    blocked: dict | None = None
    result_file: str = CONGRESS_RESULT_FILE
    history_file: str = CONGRESS_HISTORY_FILE
    summary_text: str = ""
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class TaskClassification:
    classes: list[str] = field(default_factory=list)
    confidence: str = "low"
    signals: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    skipped_evidence: list[str] = field(default_factory=list)
    rationale: str = ""
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class VerificationCheck:
    id: str
    title: str
    phase: str
    check_type: str
    required: bool
    timeout_seconds: int | None = None
    working_dir: str | None = None
    task_classes: list[str] = field(default_factory=list)
    status: str = VERIFICATION_STATUS_PENDING
    command: list[str] | None = None
    command_display: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    output_hashes: list[dict] = field(default_factory=list)
    target_paths: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    waiver_reason: str = ""
    reason: str = ""
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class IssueRecord:
    id: str
    title: str
    severity: str = "MEDIUM"
    reviewers: list[str] = field(default_factory=list)
    location: str = ""
    round: int | None = None
    status: str = ISSUE_STATUS_OPEN
    materiality: str = "material"
    rationale: str = ""
    source_artifact: str = ""
    fingerprint: str = ""
    first_seen_round: int | None = None
    last_seen_round: int | None = None
    resolution_round: int | None = None
    origins: list[dict] = field(default_factory=list)
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class ExcellenceChecklistItem:
    key: str
    label: str
    status: str = EXCELLENCE_STATUS_UNKNOWN
    evidence: str = ""
    reviewer: str = ""
    round: int | None = None
    blocker: str = ""
    source_artifact: str = ""


@dataclass
class ReviewAssessment:
    reviewer: str
    artifact_path: str
    round: int | None = None
    verdict: str | None = None
    raw_verdict: str | None = None
    effective_verdict: str | None = None
    status: str = REVIEW_STATUS_MALFORMED
    valid: bool = False
    missing_sections: list[str] = field(default_factory=list)
    malformed_reasons: list[str] = field(default_factory=list)
    stale_reasons: list[str] = field(default_factory=list)
    referenced_output_hashes: list[str] = field(default_factory=list)
    expected_output_hashes: list[dict] = field(default_factory=list)
    verification_evidence_referenced: bool = False
    session_marker: str = ""
    session_marker_present: bool = False
    partial_output_required: bool = False
    partial_output_acknowledged: bool = False
    unresolved_issues: str = ""
    quality_mode: str = DEFAULT_QUALITY_MODE
    quality_status: str = REVIEW_STATUS_MALFORMED
    quality_blocking_reasons: list[str] = field(default_factory=list)
    detected_severities: list[str] = field(default_factory=list)
    material_issue_severities: list[str] = field(default_factory=list)
    material_issue_count: int = 0
    non_material_rationale_present: bool = False
    accepted_risk_rationale_present: bool = False
    actionable_unresolved_present: bool = False
    parsed_issues: list[IssueRecord] = field(default_factory=list)
    previous_issue_statuses: list[dict] = field(default_factory=list)
    excellence_checklist: list[ExcellenceChecklistItem] = field(default_factory=list)
    excellence_missing_items: list[str] = field(default_factory=list)
    excellence_blocking_reasons: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class FinalApprovalAssessment:
    approved: bool
    status: str
    blocking_reasons: list[str] = field(default_factory=list)
    waivers: list[str] = field(default_factory=list)
    output_files: list[dict] = field(default_factory=list)
    verification_status: str = "not_recorded"
    inspector_1_status: str | None = None
    inspector_2_status: str | None = None
    second_inspector_required: bool = True
    result_status: str | None = None
    issue_state: dict = field(default_factory=dict)
    unresolved_material_issues: list[dict] = field(default_factory=list)
    excellence_checklist: list[dict] = field(default_factory=list)
    excellence_blocking_reasons: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_utc_timestamp)


def _result_to_dict(result: CongressResult) -> dict:
    return asdict(result)


def _redacted_result_to_dict(result: CongressResult) -> dict:
    data = _result_to_dict(result)
    for key in ("blocking_reasons", "verification", "reviews", "blocked", "summary_text"):
        data[key] = _redact_sensitive_data(data.get(key))
    return data


def _collect_result_output_snapshots(working_dir: str, output_files: list[str]) -> list[dict]:
    snapshots = []
    for item in _collect_output_status(working_dir, output_files):
        enriched = dict(item)
        enriched["sha256"] = None
        if item.get("exists") and not item.get("invalid_type"):
            enriched["sha256"] = _file_sha256(_workspace_abs_path(working_dir, item["path"]))
        snapshots.append(enriched)
    return snapshots


def _final_approval_assessment_to_dict(assessment: FinalApprovalAssessment | dict | None) -> dict:
    if assessment is None:
        return {}
    if isinstance(assessment, dict):
        return dict(assessment)
    return asdict(assessment)


def _latest_review_assessment(
    assessments: list[ReviewAssessment | dict] | None,
    reviewer: str,
) -> dict | None:
    for item in reversed(assessments or []):
        data = _review_assessment_to_dict(item)
        if data.get("reviewer") == reviewer:
            return data
    return None


def _assessment_matches_current_outputs(assessment: dict | None, output_files: list[dict]) -> list[str]:
    if not assessment:
        return ["Review assessment is missing."]
    expected_by_path = {
        str(item.get("path")): item.get("sha256")
        for item in (assessment.get("expected_output_hashes") or [])
        if item.get("path")
    }
    referenced_hashes = set(str(item) for item in (assessment.get("referenced_output_hashes") or []))
    stale: list[str] = []
    for item in output_files:
        path = str(item.get("path"))
        current_hash = item.get("sha256")
        if not current_hash:
            stale.append(f"{path} has no current hash available for review binding.")
            continue
        if expected_by_path.get(path) != current_hash:
            stale.append(f"{path} changed after the {assessment.get('reviewer', 'reviewer')} review.")
        if str(current_hash) not in referenced_hashes:
            stale.append(f"{path} current hash is not referenced by the {assessment.get('reviewer', 'reviewer')} review.")
    return stale


def _normalize_result_file_path(working_dir: str, raw_path: str | None,
                                output_files: list[str] | None = None) -> str:
    raw = (raw_path or CONGRESS_RESULT_FILE).strip().strip('"').strip("'")
    if not raw:
        raise ValueError("Result file path cannot be empty.")
    if raw.replace("\\", "/").endswith("/"):
        raise ValueError(f"Result file path must point to a file, not a directory: {raw_path}")
    candidate = raw if os.path.isabs(raw) else os.path.join(working_dir, raw)
    abs_path = os.path.abspath(os.path.normpath(candidate))
    root = _workspace_root(working_dir)
    if not _path_within(root, abs_path):
        raise ValueError(f"Result file path must stay inside the working directory: {raw_path}")
    real_root = _workspace_real_root(working_dir)
    real_path = os.path.realpath(abs_path)
    if not _path_within(real_root, real_path):
        raise ValueError(f"Result file path resolves outside the working directory: {raw_path}")
    relpath = os.path.relpath(abs_path, root).replace("\\", "/")
    if relpath in (".", ""):
        raise ValueError(f"Result file path must point to a file, not the working directory: {raw_path}")
    if not os.path.basename(relpath):
        raise ValueError(f"Result file path must include a file name: {raw_path}")
    relkey = _relpath_key(relpath)
    output_keys = {_relpath_key(item) for item in (output_files or [])}
    if relkey in output_keys:
        raise ValueError(f"Result file must not collide with a requested output: {relpath}")
    if relkey == _relpath_key(CONGRESS_RESULT_FILE):
        return CONGRESS_RESULT_FILE
    if os.path.isdir(abs_path):
        raise ValueError(f"Result file path points to a directory, not a file: {raw_path}")
    hardlink_error = _hardlink_error(abs_path) if os.path.isfile(abs_path) else None
    if hardlink_error:
        raise ValueError(f"Result file path is unsafe: {raw_path} ({hardlink_error})")
    if relkey != _relpath_key(CONGRESS_RESULT_FILE) and _is_reserved_output_relpath(relpath):
        raise ValueError(f"Result file path is reserved by Congress: {relpath}")
    return relpath


def _default_verification_placeholder(status: str) -> dict:
    return {
        "status": "not_implemented_phase_2",
        "required": False,
        "note": (
            "Phase 2 records the verification placeholder. "
            "The real verification runner is scheduled for Phase 6."
        ),
        "terminal_status": status,
    }


def _default_review_placeholder(status: str) -> dict:
    return {
        "inspector_1_status": None,
        "inspector_2_status": None,
        "final_approval_status": status if _status_is_approved(status) else None,
        "note": (
            "Phase 2 records review status placeholders. "
            "Full Markdown review validation and Inspector 2 are scheduled for Phase 7."
        ),
    }


def _review_assessment_to_dict(assessment: ReviewAssessment | dict | None) -> dict:
    if assessment is None:
        return {}
    if isinstance(assessment, dict):
        return dict(assessment)
    return asdict(assessment)


def _review_assessments_to_summary(
    assessments: list[ReviewAssessment | dict] | None,
    final_status: str | None = None,
    quality_mode: str | None = None,
    issue_records: list[IssueRecord | dict] | None = None,
) -> dict:
    items = [_review_assessment_to_dict(item) for item in (assessments or [])]
    latest_1 = next((item for item in reversed(items) if item.get("reviewer") == "inspector_1"), None)
    latest_2 = next((item for item in reversed(items) if item.get("reviewer") == "inspector_2"), None)
    resolved_quality_mode = _normalize_quality_mode(
        quality_mode
        or (latest_1 or {}).get("quality_mode")
        or (latest_2 or {}).get("quality_mode"),
        strict=False,
    )
    issue_state = _issue_records_to_state(issue_records or [])
    latest_excellence = []
    for latest in (latest_1, latest_2):
        if latest:
            latest_excellence.extend(latest.get("excellence_checklist") or [])
    return {
        "inspector_1_status": latest_1.get("status") if latest_1 else None,
        "inspector_2_status": latest_2.get("status") if latest_2 else None,
        "inspector_1_raw_verdict": latest_1.get("raw_verdict") if latest_1 else None,
        "inspector_1_effective_verdict": latest_1.get("effective_verdict") if latest_1 else None,
        "inspector_2_raw_verdict": latest_2.get("raw_verdict") if latest_2 else None,
        "inspector_2_effective_verdict": latest_2.get("effective_verdict") if latest_2 else None,
        "quality_mode": resolved_quality_mode,
        "quality_blocking_reasons": (
            (latest_1 or {}).get("quality_blocking_reasons")
            or (latest_2 or {}).get("quality_blocking_reasons")
            or []
        ),
        "issue_state": issue_state,
        "excellence_checklist": latest_excellence,
        "final_approval_status": final_status if final_status in APPROVED_STATUSES else None,
        "assessments": items[-10:],
        "note": (
            "Markdown reviews are validated for required sections, current output hashes, "
            "verification evidence, session freshness, stale approval markers, and "
            "best-output quality blockers when quality_mode=best."
        ),
    }


def _next_action_for_status(status: str) -> str:
    if status in APPROVED_STATUSES:
        return "Use the requested output files; Congress reached an approved terminal status."
    if status == STATUS_MAX_ROUNDS_UNAPPROVED:
        return "Review inspector feedback and rerun Congress with more rounds or revised instructions."
    if status in (STATUS_INTERRUPTED_RESUMABLE, STATUS_QUIT_BY_USER, STATUS_OUTPUT_EARLY):
        return "Resume or rerun Congress if approval is still required."
    if status in (STATUS_REVIEW_FAILED, STATUS_INSPECTOR_FAILED):
        return "Inspect the reviewer output and rerun the review step after fixing the cause."
    if status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT):
        return "Provide the required input or environment dependency, then resume."
    if status == STATUS_VERIFICATION_FAILED:
        return "Fix the failing verification evidence before approval."
    if status == STATUS_INTERNAL_ERROR:
        return "Inspect the blocking reason and logs, then rerun after fixing the error."
    return "Inspect the blocking reasons and logs before rerunning."


def render_congress_result_markdown(result: CongressResult) -> str:
    redacted_reasons = [_redact_sensitive_text(reason) for reason in result.blocking_reasons]
    redacted_verification = _redact_sensitive_data(result.verification)
    redacted_reviews = _redact_sensitive_data(result.reviews)
    redacted_summary = _redact_sensitive_text(result.summary_text or "(no summary text)")
    lines = [
        "# Congress Result",
        "",
        f"- Final status: `{result.status}`",
        f"- Approved: `{str(result.approved).lower()}`",
        f"- Exit code: `{result.exit_code}`",
        f"- Result file: `{result.result_file}`",
        f"- History file: `{result.history_file}`",
        f"- Timestamp: `{result.timestamp}`",
        "",
        "## Requested Outputs",
    ]
    if result.output_files:
        for item in result.output_files:
            marker = "OK" if item.get("exists") and not item.get("invalid_type") else "MISSING"
            if item.get("invalid_type"):
                marker = "INVALID"
            size = item.get("size")
            size_text = f"{size} bytes" if size is not None else "no size"
            if item.get("invalid_reason"):
                size_text = f"{size_text}; {item.get('invalid_reason')}"
            sha = item.get("sha256") or "unavailable"
            lines.append(f"- [{marker}] `{item.get('path')}` ({size_text}, sha256: `{sha}`)")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Blocking Reasons"])
    if redacted_reasons:
        lines.extend(f"- {reason}" for reason in redacted_reasons)
    else:
        lines.append("- None recorded.")

    if result.blocked:
        redacted_blocked = _redact_sensitive_data(result.blocked)
        lines.extend([
            "",
            "## Blocked State",
            f"- Status: `{redacted_blocked.get('status', result.status)}`",
            f"- Category: `{redacted_blocked.get('category', 'unknown')}`",
            f"- Source: `{redacted_blocked.get('source_agent', 'unknown')}`",
            f"- Round: `{redacted_blocked.get('round', 'unknown')}`",
            f"- Reason: {redacted_blocked.get('reason', 'No reason recorded.')}",
            f"- Required action: {redacted_blocked.get('required_action', 'No action recorded.')}",
        ])

    lines.extend([
        "",
        "## Verification",
        f"- Status: `{redacted_verification.get('status', 'not_recorded')}`",
        f"- Mode: `{redacted_verification.get('mode', 'unknown')}`",
        f"- Required now: `{str(redacted_verification.get('required', False)).lower()}`",
        f"- Checks recorded: `{redacted_verification.get('total_checks', 0)}`",
        f"- Passed/failed/blocked/waived/not-applicable: "
        f"`{redacted_verification.get('passed', 0)}/"
        f"{redacted_verification.get('failed', 0)}/"
        f"{redacted_verification.get('blocked', 0)}/"
        f"{redacted_verification.get('waived', 0)}/"
        f"{redacted_verification.get('not_applicable', 0)}`",
        f"- Evidence file: `{CONGRESS_VERIFICATION_FILE}`",
        f"- Note: {redacted_verification.get('note', 'No verification note recorded.')}",
        "",
        "## Reviews",
        f"- Inspector 1 status: `{redacted_reviews.get('inspector_1_status')}`",
        f"- Inspector 2 status: `{redacted_reviews.get('inspector_2_status')}`",
        f"- Quality mode: `{redacted_reviews.get('quality_mode', DEFAULT_QUALITY_MODE)}`",
        f"- Inspector 1 raw/effective verdict: "
        f"`{redacted_reviews.get('inspector_1_raw_verdict')}` / "
        f"`{redacted_reviews.get('inspector_1_effective_verdict')}`",
        f"- Inspector 2 raw/effective verdict: "
        f"`{redacted_reviews.get('inspector_2_raw_verdict')}` / "
        f"`{redacted_reviews.get('inspector_2_effective_verdict')}`",
        f"- Final approval status: `{redacted_reviews.get('final_approval_status')}`",
        f"- Note: {redacted_reviews.get('note', 'No review note recorded.')}",
    ])
    quality_reasons = redacted_reviews.get("quality_blocking_reasons") or []
    if quality_reasons:
        lines.append("- Quality blockers: " + "; ".join(str(reason) for reason in quality_reasons[:4]))
    issue_state = redacted_reviews.get("issue_state") or {}
    if issue_state:
        lines.extend([
            "",
            "### Issue Lifecycle",
            f"- Total tracked issues: `{issue_state.get('total_count', 0)}`",
            f"- Open issues: `{issue_state.get('open_count', 0)}`",
            f"- Open material issues: `{issue_state.get('open_material_count', 0)}`",
        ])
        for item in (issue_state.get("issues") or [])[:8]:
            reviewers = ", ".join(str(r) for r in item.get("reviewers") or []) or "unknown"
            lines.append(
                f"- `{item.get('id')}` [{item.get('severity')}] status=`{item.get('status')}` "
                f"reviewers=`{reviewers}`: {item.get('title')}"
            )
    excellence = redacted_reviews.get("excellence_checklist") or []
    if excellence:
        lines.extend(["", "### Excellence Checklist"])
        for item in excellence[:12]:
            lines.append(
                f"- `{item.get('reviewer', 'congress')}` {item.get('label')}: "
                f"`{item.get('status')}`"
            )
    assessments = redacted_reviews.get("assessments") or []
    if assessments:
        lines.extend(["", "### Review Assessments"])
        for item in assessments[-4:]:
            lines.append(
                f"- `{item.get('reviewer', 'unknown')}` round `{item.get('round', 'unknown')}`: "
                f"status=`{item.get('status')}`, verdict=`{item.get('verdict')}`, "
                f"raw=`{item.get('raw_verdict')}`, effective=`{item.get('effective_verdict')}`, "
                f"artifact=`{item.get('artifact_path')}`"
            )
            reasons = (item.get("malformed_reasons") or []) + (item.get("stale_reasons") or [])
            if reasons:
                lines.append("  - Reasons: " + "; ".join(str(reason) for reason in reasons[:4]))
            quality_item_reasons = item.get("quality_blocking_reasons") or []
            if quality_item_reasons:
                lines.append(
                    "  - Quality blockers: "
                    + "; ".join(str(reason) for reason in quality_item_reasons[:4])
                )

    lines.extend([
        "",
        "## Trust Model",
        "- Congress may ask real Codex agents to inspect and edit files in the working directory to complete the requested deliverables.",
        "- Congress-managed files are reserved: `session_request.md`, `researcher_updated.md`, `inspector_comments.md`, `inspector_2_comments.md`, `congress_state.json`, `congress_result.md`, `congress_history.md`, `congress_verification.md`, `congress_blocked.md`, `congress.lock`, and `congress_rounds/`.",
        "- Agents and Congress-owned verification may run real project commands, scripts, tests, builds, browser checks, and environment probes when needed.",
        "- `blocked_needs_user` means user input or credentials are required; `blocked_needs_environment` means an external tool, service, server, or environment dependency is unavailable.",
        "- `approved` requires requested outputs, current hashes, verification evidence, Inspector 1 review, and Inspector 2 review to agree.",
        "- `approved_with_waiver` means an explicit waiver was used, such as `--second-inspector=off`; inspect the waiver before trusting the result.",
        "- Inspect Markdown artifacts for evidence: `congress_result.md`, `congress_history.md`, `congress_verification.md`, `inspector_comments.md`, and `inspector_2_comments.md`.",
        "",
        "## Next Action",
        f"- {_next_action_for_status(result.status)}",
        "",
        "## Summary",
        "```text",
        redacted_summary,
        "```",
        "",
    ])
    return "\n".join(lines)


def _render_history_event(result: CongressResult) -> str:
    reasons = (
        "; ".join(_redact_sensitive_text(reason) for reason in result.blocking_reasons)
        if result.blocking_reasons else "none"
    )
    lines = [
        f"## {result.timestamp} - {result.status}",
        "",
        f"- Approved: `{str(result.approved).lower()}`",
        f"- Exit code: `{result.exit_code}`",
        f"- Result file: `{result.result_file}`",
        f"- History file: `{result.history_file}`",
        f"- Blocking reasons: {reasons}",
        "- Outputs:",
    ]
    if result.output_files:
        for item in result.output_files:
            exists = "exists" if item.get("exists") else "missing"
            if item.get("invalid_type"):
                exists = "invalid"
            lines.append(f"  - `{item.get('path')}`: {exists}, sha256: `{item.get('sha256') or 'unavailable'}`")
    else:
        lines.append("  - (none)")
    if result.blocked:
        blocked = _redact_sensitive_data(result.blocked)
        lines.extend([
            "- Blocked state:",
            f"  - Category: `{blocked.get('category', 'unknown')}`",
            f"  - Source: `{blocked.get('source_agent', 'unknown')}`",
            f"  - Reason: {blocked.get('reason', 'No reason recorded.')}",
        ])
    verification = _redact_sensitive_data(result.verification or {})
    if verification:
        lines.append("- Verification:")
        lines.append(f"  - Status: `{verification.get('status', 'not_recorded')}`")
        lines.append(f"  - Mode: `{verification.get('mode', 'unknown')}`")
        lines.append(f"  - Checks recorded: `{verification.get('total_checks', 0)}`")
        lines.append(f"  - Evidence file: `{CONGRESS_VERIFICATION_FILE}`")
    reviews = _redact_sensitive_data(result.reviews or {})
    if reviews:
        lines.append("- Reviews:")
        lines.append(f"  - Inspector 1: `{reviews.get('inspector_1_status')}`")
        lines.append(f"  - Inspector 2: `{reviews.get('inspector_2_status')}`")
        lines.append(f"  - Quality mode: `{reviews.get('quality_mode', DEFAULT_QUALITY_MODE)}`")
        quality_reasons = reviews.get("quality_blocking_reasons") or []
        if quality_reasons:
            lines.append(
                "  - Quality blockers: "
                + "; ".join(str(reason) for reason in quality_reasons[:4])
            )
        issue_state = reviews.get("issue_state") or {}
        if issue_state:
            lines.append(
                "  - Issue lifecycle: "
                f"open_material=`{issue_state.get('open_material_count', 0)}`, "
                f"open=`{issue_state.get('open_count', 0)}`, "
                f"total=`{issue_state.get('total_count', 0)}`"
            )
        for item in (reviews.get("assessments") or [])[-4:]:
            lines.append(
                f"  - `{item.get('reviewer', 'unknown')}` round `{item.get('round', 'unknown')}` "
                f"status=`{item.get('status')}` raw=`{item.get('raw_verdict')}` "
                f"effective=`{item.get('effective_verdict')}` artifact=`{item.get('artifact_path')}`"
            )
    inventory = _redact_sensitive_data((result.verification or {}).get("capability_inventory") or {})
    if inventory:
        lines.append("- Capability/preflight inventory:")
        codex = inventory.get("codex") or {}
        if codex:
            lines.append(f"  - Codex available: `{str(bool(codex.get('available'))).lower()}`")
        preflight_results = inventory.get("preflight_results") or []
        if preflight_results:
            lines.append("  - Preflight results:")
            for item in preflight_results[:20]:
                req = item.get("requirement") or {}
                label = req.get("label") or req.get("value") or req.get("name") or req.get("url") or req.get("type")
                state = "pass" if item.get("ok") else "fail"
                lines.append(f"    - `{label}`: {state} - {item.get('message', 'no message')}")
        capability_hints = inventory.get("capability_hints") or []
        if capability_hints:
            lines.append("  - Capability hints: `" + ", ".join(str(item) for item in capability_hints[:20]) + "`")
    lines.append("")
    return "\n".join(lines)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_text(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def _blocked_status_for_category(category: str) -> str:
    if category == "user":
        return STATUS_BLOCKED_NEEDS_USER
    return STATUS_BLOCKED_NEEDS_ENVIRONMENT


def _make_blocked_state(status: str, category: str, reason: str,
                        required_action: str, source_agent: str,
                        round_num=None, evidence: list[str] | None = None,
                        requirement: dict | None = None) -> dict:
    return {
        "status": status,
        "category": category,
        "reason": _redact_sensitive_text(reason or "Blocked with no reason recorded."),
        "required_action": _redact_sensitive_text(required_action or "Resolve the blocker and resume."),
        "source_agent": source_agent,
        "round": round_num,
        "timestamp": _utc_timestamp(),
        "evidence": [_redact_sensitive_text(item) for item in (evidence or [])],
        "requirement": _redact_sensitive_data(requirement or {}),
        "artifact": CONGRESS_BLOCKED_FILE,
    }


def _render_congress_blocked_markdown(blocked: dict) -> str:
    redacted = _redact_sensitive_data(blocked or {})
    lines = [
        "# Congress Blocked",
        "",
        f"- Status: `{redacted.get('status', 'unknown')}`",
        f"- Category: `{redacted.get('category', 'unknown')}`",
        f"- Source: `{redacted.get('source_agent', 'unknown')}`",
        f"- Round: `{redacted.get('round', 'unknown')}`",
        f"- Timestamp: `{redacted.get('timestamp', _utc_timestamp())}`",
        "",
        "## Reason",
        redacted.get("reason", "No reason recorded."),
        "",
        "## Required Action",
        redacted.get("required_action", "Resolve the blocker and resume."),
        "",
        "## Resume",
        "- Resolve the required user input or environment dependency.",
        "- Resume Congress with `--resume` and optionally add `--resume-comment=\"...\"`.",
        "- Do not place raw secret values in resume comments; set credentials in the environment.",
    ]
    evidence = redacted.get("evidence") or []
    if evidence:
        lines.extend(["", "## Evidence"])
        lines.extend(f"- {item}" for item in evidence)
    requirement = redacted.get("requirement") or {}
    if requirement:
        lines.extend(["", "## Requirement"])
        for key in sorted(requirement):
            lines.append(f"- {key}: `{requirement.get(key)}`")
    lines.append("")
    return "\n".join(lines)


def _write_blocked_artifact(working_dir: str, blocked: dict) -> None:
    path = os.path.join(working_dir, CONGRESS_BLOCKED_FILE)
    _atomic_write_text(path, _render_congress_blocked_markdown(blocked))


def _classify_blocked_category(text: str) -> str:
    lower = (text or "").lower()
    user_keywords = (
        "credential", "api key", "token", "password", "secret", "login",
        "account", "approval", "user decision", "confirm", "paid api",
        "permission", "ui action", "manual action",
    )
    environment_keywords = (
        "localhost", "local server", "server", "port", "tool", "command",
        "binary", "browser", "playwright", "network", "package registry",
        "npm", "pip", "service", "database", "connection refused",
        "unreachable", "not installed", "missing dependency",
    )
    if any(keyword in lower for keyword in user_keywords):
        return "user"
    if any(keyword in lower for keyword in environment_keywords):
        return "environment"
    return "environment"


def _extract_blocked_field(section: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        pattern = rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*(.+?)\s*$"
        match = re.search(pattern, section)
        if match:
            return match.group(1).strip()
    return ""


def _parse_blocked_output(text: str, source_agent: str, round_num=None) -> dict | None:
    if not text:
        return None
    match = re.search(r"(?im)^\s*#{1,3}\s*BLOCKED\b.*$", text)
    if match:
        section = text[match.start():]
        next_heading = re.search(r"(?m)^\s*#{1,3}\s+(?!BLOCKED\b).*$", section[len(match.group(0)):])
        if next_heading:
            section = section[:len(match.group(0)) + next_heading.start()]
    else:
        match = re.search(r"(?im)^\s*BLOCKED\s*:\s*(.+)$", text)
        if not match:
            return None
        section = text[match.start():]

    reason = _extract_blocked_field(section, ("Reason", "Problem", "Blocked because"))
    required_action = _extract_blocked_field(
        section,
        ("Required action", "Action required", "Need", "Needed", "Resume action"),
    )
    if not reason:
        lines = [
            line.strip(" -*\t")
            for line in section.splitlines()
            if line.strip() and "blocked" not in line.lower()
        ]
        reason = lines[0] if lines else "Agent reported a blocker."
    if not required_action:
        required_action = "Resolve the missing requirement described by the agent, then resume."
    category = _classify_blocked_category(section)
    status = _blocked_status_for_category(category)
    return _make_blocked_state(
        status,
        category,
        reason,
        required_action,
        source_agent,
        round_num=round_num,
        evidence=["Detected Markdown BLOCKED output from agent stdout."],
    )


class WorkspaceLockError(RuntimeError):
    pass


def _workspace_lock_path(working_dir: str) -> str:
    return os.path.join(working_dir, CONGRESS_LOCK_FILE)


def _workspace_lock_takeover_path(working_dir: str) -> str:
    return os.path.join(working_dir, CONGRESS_LOCK_TAKEOVER_FILE)


def _read_workspace_lock_metadata(path: str) -> tuple[dict | None, str | None]:
    if not os.path.exists(path):
        return None, "lock file does not exist"
    if not os.path.isfile(path):
        return None, "lock path exists but is not a regular file"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"lock metadata is unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "lock metadata is not an object"
    return data, None


def _lock_file_age_seconds(path: str) -> float | None:
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _metadata_age_seconds(metadata: dict | None, path: str) -> float | None:
    if metadata:
        try:
            return max(0.0, time.time() - float(metadata.get("created_at_epoch")))
        except (TypeError, ValueError):
            pass
    return _lock_file_age_seconds(path)


def _pid_is_running(pid) -> bool | None:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return False
    if pid_int == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            if kernel32.GetLastError() == 5:
                return True
            return False
        except Exception:
            return None
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _lock_stale_reason(metadata: dict | None, path: str,
                       stale_after_seconds: int = LOCK_STALE_SECONDS,
                       read_error: str | None = None) -> str | None:
    age = _metadata_age_seconds(metadata, path)
    if metadata:
        pid_state = _pid_is_running(metadata.get("pid"))
        if pid_state is True:
            return None
        if pid_state is False:
            return f"lock owner pid {metadata.get('pid')} is not running"
        return None
    if read_error:
        return None
    return None


def _format_lock_owner(metadata: dict | None) -> str:
    if not metadata:
        return "unknown owner"
    parts = []
    for key in ("session_id", "pid", "hostname", "created_at", "working_dir", "status"):
        value = metadata.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "unknown owner"


def _same_lock_metadata(left: dict | None, right: dict | None) -> bool:
    if not left or not right:
        return False
    for key in ("owner_token", "session_id", "pid", "created_at_epoch", "working_dir"):
        if left.get(key) != right.get(key):
            return False
    return True


def _build_lock_metadata(working_dir: str, session_id: str, status: str,
                         owner_token: str | None = None) -> dict:
    now = time.time()
    return {
        "lock_version": 1,
        "session_id": session_id,
        "owner_token": owner_token or uuid.uuid4().hex,
        "pid": os.getpid(),
        "hostname": socket.gethostname() if hasattr(socket, "gethostname") else "",
        "created_at": _utc_timestamp(),
        "created_at_epoch": now,
        "working_dir": os.path.abspath(working_dir),
        "status": status,
    }


def _acquire_workspace_lock_file(working_dir: str, session_id: str,
                                 status: str = STATUS_RUNNING,
                                 stale_after_seconds: int = LOCK_STALE_SECONDS) -> dict:
    path = _workspace_lock_path(working_dir)
    takeover_path = _workspace_lock_takeover_path(working_dir)
    metadata = _build_lock_metadata(working_dir, session_id, status)
    stale_replacement_reasons: list[str] = []
    while True:
        try:
            if os.path.exists(takeover_path):
                raise WorkspaceLockError(
                    f"Workspace lock stale takeover is already in progress: {CONGRESS_LOCK_TAKEOVER_FILE}"
                )
            if stale_replacement_reasons:
                metadata["replaced_stale_lock_reasons"] = list(stale_replacement_reasons)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, default=str)
            return metadata
        except FileExistsError:
            existing, read_error = _read_workspace_lock_metadata(path)
            if existing and existing.get("session_id") == session_id and existing.get("pid") == os.getpid():
                return existing
            stale_reason = _lock_stale_reason(existing, path, stale_after_seconds, read_error)
            if stale_reason:
                takeover_metadata = {
                    "takeover_version": 1,
                    "session_id": session_id,
                    "pid": os.getpid(),
                    "created_at": _utc_timestamp(),
                    "created_at_epoch": time.time(),
                    "working_dir": os.path.abspath(working_dir),
                    "target_owner_token": existing.get("owner_token") if existing else None,
                    "target_session_id": existing.get("session_id") if existing else None,
                    "reason": stale_reason,
                }
                try:
                    takeover_fd = os.open(takeover_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(takeover_fd, "w", encoding="utf-8") as f:
                        json.dump(_redact_sensitive_data(takeover_metadata), f, indent=2, default=str)
                except FileExistsError as exc:
                    raise WorkspaceLockError(
                        f"Workspace lock stale takeover is already in progress: {CONGRESS_LOCK_TAKEOVER_FILE}"
                    ) from exc
                except OSError as exc:
                    raise WorkspaceLockError(
                        f"Could not create workspace lock takeover guard: {exc}"
                    ) from exc
                try:
                    current, current_read_error = _read_workspace_lock_metadata(path)
                    current_stale_reason = _lock_stale_reason(
                        current, path, stale_after_seconds, current_read_error
                    )
                    if current_read_error:
                        raise WorkspaceLockError(
                            "Workspace lock became unreadable during stale takeover: "
                            + current_read_error
                        )
                    if not current_stale_reason:
                        continue
                    if not _same_lock_metadata(existing, current):
                        continue
                    stale_replacement_reasons.append(current_stale_reason)
                    try:
                        os.remove(path)
                    except OSError as exc:
                        raise WorkspaceLockError(
                            f"Workspace lock appears stale ({current_stale_reason}) "
                            f"but could not be removed: {exc}"
                        ) from exc
                    if stale_replacement_reasons:
                        metadata["replaced_stale_lock_reasons"] = list(stale_replacement_reasons)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(metadata, f, indent=2, default=str)
                    return metadata
                finally:
                    try:
                        os.remove(takeover_path)
                    except OSError:
                        pass
            if read_error:
                raise WorkspaceLockError(
                    f"Workspace lock exists and cannot be safely interpreted: {read_error}"
                )
            raise WorkspaceLockError(
                "Another Congress session is active in this working directory: "
                + _format_lock_owner(existing)
            )
        except OSError as exc:
            raise WorkspaceLockError(f"Could not create workspace lock: {exc}") from exc


def _release_workspace_lock_file(working_dir: str, session_id: str,
                                 owner_token: str | None) -> bool:
    path = _workspace_lock_path(working_dir)
    if not os.path.exists(path):
        return True
    metadata, read_error = _read_workspace_lock_metadata(path)
    if read_error or not metadata:
        return False
    token_matches = bool(owner_token and metadata.get("owner_token") == owner_token)
    session_matches = metadata.get("session_id") == session_id and metadata.get("pid") == os.getpid()
    if not token_matches and not session_matches:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _resume_lock_conflict(working_dir: str, session_id: str | None) -> str | None:
    path = _workspace_lock_path(working_dir)
    if not os.path.exists(path):
        return None
    metadata, read_error = _read_workspace_lock_metadata(path)
    stale_reason = _lock_stale_reason(metadata, path, LOCK_STALE_SECONDS, read_error)
    if stale_reason:
        return None
    if read_error:
        return f"Workspace lock prevents safe resume: {read_error}"
    if metadata and session_id and metadata.get("session_id") == session_id:
        pid_state = _pid_is_running(metadata.get("pid"))
        if pid_state is True and metadata.get("pid") != os.getpid():
            return "Saved session appears to still be active in another process."
        return None
    return (
        "Workspace lock is held by another Congress session: "
        + _format_lock_owner(metadata)
    )


def _managed_artifact_path_errors(working_dir: str) -> list[str]:
    errors = []
    for filename in sorted(CRITICAL_MANAGED_FILE_ARTIFACTS):
        path = os.path.join(working_dir, filename)
        if os.path.exists(path) and not os.path.isfile(path):
            errors.append(f"{filename} exists but is not a regular file")
        tmp_path = path + ".tmp"
        if os.path.exists(tmp_path) and not os.path.isfile(tmp_path):
            errors.append(f"{filename}.tmp exists but is not a regular file")
    for dirname in sorted(CRITICAL_MANAGED_DIRECTORY_ARTIFACTS):
        path = os.path.join(working_dir, dirname)
        if os.path.exists(path) and not os.path.isdir(path):
            errors.append(f"{dirname} exists but is not a directory")
    return errors


def _state_is_resumable(saved: dict | None) -> bool:
    return bool(saved and saved.get("status") in (
        STATUS_RUNNING,
        STATUS_INTERRUPTED_RESUMABLE,
        STATUS_BLOCKED_NEEDS_USER,
        STATUS_BLOCKED_NEEDS_ENVIRONMENT,
        STATUS_MAX_ROUNDS_UNAPPROVED,
        STATUS_REVIEW_FAILED,
    ))


def _state_requests_interactive(saved: dict | None) -> bool:
    if not saved:
        return False
    return bool(
        saved.get("interactive_requested")
        or (saved.get("ci_mode_explicit") and saved.get("ci_mode") is False)
    )


def _validate_resume_state(saved: dict | None, working_dir: str) -> dict:
    normalized = _normalize_state_v3(saved, working_dir) if saved else None
    errors: list[str] = []
    output_files: list[str] = []
    if not normalized:
        errors.append("No v3-normalizable state was found.")
    else:
        if not _state_is_resumable(normalized):
            errors.append(f"State status is not resumable: {normalized.get('status')}")
        saved_working_dir = normalized.get("working_dir")
        if saved_working_dir:
            saved_root = os.path.normcase(os.path.abspath(saved_working_dir))
            current_root = os.path.normcase(os.path.abspath(working_dir))
            if saved_root != current_root:
                errors.append(
                    f"State working directory does not match current workspace: {saved_working_dir}"
                )
        substep = normalized.get("current_substep", "")
        phase = _phase_from_substep(substep)
        if (
            phase == PHASE_UNKNOWN
            and not (
                normalized.get("status") in {STATUS_MAX_ROUNDS_UNAPPROVED, STATUS_REVIEW_FAILED}
                and substep == PHASE_TERMINAL
            )
        ):
            errors.append(f"State current_substep is not resumable: {substep}")
        outputs_raw = normalized.get("output_files") or normalized.get("requested_outputs") or []
        if not outputs_raw:
            errors.append("State does not contain requested output files.")
        else:
            try:
                output_files = _normalize_output_file_list(list(outputs_raw), working_dir)
            except ValueError as exc:
                errors.append(str(exc))
        errors.extend(_managed_artifact_path_errors(working_dir))
        lock_error = _resume_lock_conflict(working_dir, normalized.get("session_id"))
        if lock_error:
            errors.append(lock_error)
    return {
        "ok": not errors,
        "state": normalized,
        "output_files": output_files,
        "errors": errors,
    }


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


def _render_original_request_source_of_truth_markdown(user_query: str) -> str:
    return (
        "## Original User Request / Source of Truth\n"
        "The original user prompt is the highest-priority task definition after system/developer safety rules. "
        "Source manifests, verification summaries, previous notes, and optional context must not narrow or override the original user request. "
        "If any context conflicts with the original user request, follow the original user request and explain the conflict.\n\n"
        f"{user_query}\n\n"
    )


def _source_manifest_items_by_tier(source_manifest: list[dict] | None) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {
        SOURCE_CONTEXT_TIER_REQUIRED: [],
        SOURCE_CONTEXT_TIER_PROJECT: [],
        SOURCE_CONTEXT_TIER_OPTIONAL: [],
        SOURCE_CONTEXT_TIER_EXCLUDED: [],
    }
    for item in source_manifest or []:
        tier = item.get("tier")
        if item.get("excluded"):
            tier = SOURCE_CONTEXT_TIER_EXCLUDED
        elif item.get("required"):
            tier = SOURCE_CONTEXT_TIER_REQUIRED
        elif tier not in grouped:
            tier = SOURCE_CONTEXT_TIER_OPTIONAL
        grouped.setdefault(tier, []).append(item)
    return grouped


def _render_source_manifest_section(source_manifest: list[dict] | None) -> str:
    if not source_manifest:
        return ""
    grouped = _source_manifest_items_by_tier(source_manifest)
    lines = [
        "## Source Context Manifest",
        "Optional source context is available to inspect as needed; Tier 2 and Tier 3 files are not mandatory full-read-first context.",
        "Read all Tier 1 required files when present. Use optional files when they are clearly relevant to a complete and trustworthy answer.",
    ]

    required_items = grouped.get(SOURCE_CONTEXT_TIER_REQUIRED, [])
    if required_items:
        lines.append("")
        lines.append(f"### {SOURCE_CONTEXT_TIER_LABELS[SOURCE_CONTEXT_TIER_REQUIRED]}")
        for item in required_items:
            relpath = item.get("relpath") or item.get("path") or ""
            reason = item.get("reason") or item.get("tier_name") or "required source"
            sensitive = " sensitive-direct-request" if item.get("sensitive") else ""
            lines.append(f"- `{relpath}` ({reason}{sensitive})")

    for tier in (SOURCE_CONTEXT_TIER_PROJECT, SOURCE_CONTEXT_TIER_OPTIONAL):
        items = grouped.get(tier, [])
        if not items:
            continue
        lines.append("")
        lines.append(f"### {SOURCE_CONTEXT_TIER_LABELS[tier]}")
        lines.append("These files are optional context. Inspect them as needed; do not mechanically read unrelated optional files.")
        for item in items[:SOURCE_MANIFEST_MAX_FILES]:
            relpath = item.get("relpath") or item.get("path") or ""
            reason = item.get("reason") or "source"
            priority = item.get("priority", "normal")
            lines.append(f"- `{relpath}` ({reason}, priority={priority})")

    excluded_items = grouped.get(SOURCE_CONTEXT_TIER_EXCLUDED, [])
    if excluded_items:
        counts: dict[str, int] = {}
        for item in excluded_items:
            reason = str(item.get("reason") or "excluded by default")
            counts[reason] = counts.get(reason, 0) + 1
        lines.append("")
        lines.append(f"### {SOURCE_CONTEXT_TIER_LABELS[SOURCE_CONTEXT_TIER_EXCLUDED]}")
        lines.append("These paths are excluded by default unless the user directly requests them. Sensitive excluded file names are summarized, not listed.")
        for reason, count in sorted(counts.items()):
            lines.append(f"- {reason}: {count} item(s)")
    return "\n".join(lines) + "\n\n"


def _render_capability_inventory_section(inventory: dict | None) -> str:
    if not inventory:
        return ""
    redacted = _redact_sensitive_data(inventory)
    lines = [
        "## Capability and Preflight Inventory",
        "This inventory is informational context, not a restrictive command policy.",
    ]
    codex = redacted.get("codex") or {}
    lines.append(f"- Codex binary: `{codex.get('path') or 'not_detected'}`")
    lines.append(f"- Codex available: `{str(bool(codex.get('available'))).lower()}`")
    tools = redacted.get("tools") or {}
    if tools:
        tool_bits = [f"{name}={'present' if present else 'missing'}" for name, present in sorted(tools.items())]
        lines.append("- Tools on PATH: `" + ", ".join(tool_bits) + "`")
    credentials = redacted.get("credentials") or {}
    if credentials:
        cred_bits = [f"{name}={state}" for name, state in sorted(credentials.items())]
        lines.append("- Credentials by presence only: `" + ", ".join(cred_bits) + "`")
    hints = redacted.get("project_hints") or []
    if hints:
        lines.append("- Project hints:")
        lines.extend(f"  - `{hint}`" for hint in hints[:20])
    capability_hints = redacted.get("capability_hints") or []
    if capability_hints:
        lines.append("- Capability hints:")
        lines.extend(f"  - `{hint}`" for hint in capability_hints[:20])
    preflight = redacted.get("preflight_requirements") or []
    if preflight:
        lines.append("- Preflight requirements:")
        for item in preflight:
            lines.append(f"  - `{item.get('type')}`: {item.get('label') or item.get('value')}")
    preflight_results = redacted.get("preflight_results") or []
    if preflight_results:
        lines.append("- Preflight results:")
        for item in preflight_results:
            req = item.get("requirement") or {}
            label = req.get("label") or req.get("value") or req.get("name") or req.get("url") or req.get("type")
            state = "pass" if item.get("ok") else "fail"
            lines.append(f"  - `{label}`: {state} - {item.get('message', 'no message')}")
    return "\n".join(lines) + "\n\n"


def _render_verification_context_section(classification: dict | None,
                                         verification_summary: dict | None) -> str:
    if not classification and not verification_summary:
        return ""
    classification = _redact_sensitive_data(classification or {})
    verification_summary = _redact_sensitive_data(
        _verification_summary_for_current_context(verification_summary)
    )
    lines = [
        "## Congress Verification Context",
        "Congress-owned task classification and verification evidence are informational context for agents. Agents must still inspect files and run any additional real checks needed.",
        "Mechanical verification passing does not mean the deliverable is excellent, and it is not quality approval. Inspectors must still judge content quality, research depth, correctness, completeness, usefulness, source trust, and fit to the original request.",
    ]
    classes = classification.get("classes") or []
    if classes:
        lines.append("- Task classes: `" + ", ".join(classes) + "`")
        lines.append(f"- Classification confidence: `{classification.get('confidence', 'unknown')}`")
        required = classification.get("required_evidence") or []
        if required:
            lines.append("- Required evidence: `" + ", ".join(str(item) for item in required) + "`")
        if TASK_CLASS_RESEARCH_CURRENT_INFO in classes:
            lines.append("- " + RESEARCH_EVIDENCE_REQUIREMENT_TEXT)
    if verification_summary:
        lines.extend([
            f"- Verification status: `{verification_summary.get('status', 'not_started')}`",
            f"- Verification mode: `{verification_summary.get('mode', 'unknown')}`",
            f"- Checks recorded: `{verification_summary.get('total_checks', 0)}`",
            f"- Passed/failed/blocked/waived/not-applicable: "
            f"`{verification_summary.get('passed', 0)}/"
            f"{verification_summary.get('failed', 0)}/"
            f"{verification_summary.get('blocked', 0)}/"
            f"{verification_summary.get('waived', 0)}/"
            f"{verification_summary.get('not_applicable', 0)}`",
            f"- Full evidence file: `{CONGRESS_VERIFICATION_FILE}`",
        ])
    return "\n".join(lines) + "\n\n"


def _render_review_context_section(review_summary: dict | None,
                                   audience: str = "default") -> str:
    if not review_summary:
        return ""
    if audience == "inspector_2_pre_review":
        return (
            "## Congress Review Context\n"
            "Inspector 2 pre-review independence mode is active. Prior Inspector 1 review context is intentionally withheld from this session request until Inspector 2 produces independent findings.\n\n"
        )
    data = _redact_sensitive_data(review_summary)
    lines = [
        "## Congress Review Context",
        "Congress records Markdown review assessments here so later agents can avoid relying on stale approvals.",
        f"- Inspector 1 status: `{data.get('inspector_1_status')}`",
        f"- Inspector 2 status: `{data.get('inspector_2_status')}`",
        f"- Final approval status: `{data.get('final_approval_status')}`",
    ]
    assessments = data.get("assessments") or []
    if assessments:
        lines.append("- Recent review assessments:")
        for item in assessments[-4:]:
            lines.append(
                f"  - `{item.get('reviewer', 'unknown')}` round `{item.get('round', 'unknown')}`: "
                f"status=`{item.get('status')}`, verdict=`{item.get('verdict')}`, "
                f"artifact=`{item.get('artifact_path')}`"
            )
    issue_state = data.get("issue_state") or {}
    if issue_state:
        lines.append("- Issue lifecycle:")
        lines.append(
            f"  - Open material: `{issue_state.get('open_material_count', 0)}`; "
            f"open: `{issue_state.get('open_count', 0)}`; total: `{issue_state.get('total_count', 0)}`"
        )
        for item in (issue_state.get("issues") or [])[:6]:
            reviewers = ", ".join(str(r) for r in item.get("reviewers") or []) or "unknown"
            lines.append(
                f"  - `{item.get('id')}` [{item.get('severity')}] status=`{item.get('status')}` "
                f"reviewers=`{reviewers}`: {item.get('title')}"
            )
    return "\n".join(lines) + "\n\n"


def _write_session_request(working_dir: str, user_query: str, output_files: list[str],
                           user_updates: list[dict] | None = None,
                           source_manifest: list[dict] | None = None,
                           capability_inventory: dict | None = None,
                           task_classification: dict | None = None,
                           verification_summary: dict | None = None,
                           review_summary: dict | None = None,
                           audience: str = "default") -> bool:
    path = os.path.join(working_dir, SESSION_REQUEST_FILE)
    listing = "\n".join(f"- {item}" for item in output_files) if output_files else "- (none)"
    if audience == "inspector_2_pre_review":
        artifact_guidance = (
            f"{RESEARCHER_UPDATED_FILE} is the Researcher's explanation/process notes.\n"
            f"{INSPECTOR_2_COMMENTS_FILE} is the file where Inspector 2's independent review will be saved.\n"
            "Inspector 2 pre-review independence guardrail: do not open or read "
            "prior Inspector 1 review artifacts or any Inspector 1 review excerpts "
            "before producing your independent findings. Congress will compare reviews after your independent review.\n\n"
        )
    else:
        artifact_guidance = (
            f"{RESEARCHER_UPDATED_FILE} is the Researcher's explanation/process notes.\n"
            f"{INSPECTOR_COMMENTS_FILE} is the Inspector's review.\n\n"
            f"{INSPECTOR_2_COMMENTS_FILE} is Inspector 2's independent review when present.\n\n"
        )
    content = (
        "# Congress Session Request\n\n"
        "This file is generated by Congress and should be re-read every round.\n\n"
        f"{_render_original_request_source_of_truth_markdown(user_query)}"
        "## Requested Output Files\n"
        f"{listing}\n\n"
        "The files above are the real deliverables for the user.\n"
        f"{artifact_guidance}"
        f"{_render_user_updates_section(user_updates)}"
        f"{_render_source_manifest_section(source_manifest)}"
        f"{_render_capability_inventory_section(capability_inventory)}"
        f"{_render_verification_context_section(task_classification, verification_summary)}"
        f"{_render_review_context_section(review_summary, audience=audience)}"
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


def _phase_from_substep(substep: str | None) -> str:
    value = (substep or "").strip()
    mapping = {
        "": PHASE_STARTUP,
        "start": PHASE_STARTUP,
        "researcher": PHASE_RESEARCHER_RUNNING,
        "researcher_running": PHASE_RESEARCHER_RUNNING,
        "researcher_done": PHASE_RESEARCHER_DONE,
        "inspector": PHASE_INSPECTOR_RUNNING,
        "inspector_running": PHASE_INSPECTOR_RUNNING,
        "inspector_done": PHASE_INSPECTOR_DONE,
        "inspector_2": PHASE_INSPECTOR_2_RUNNING,
        "inspector_2_running": PHASE_INSPECTOR_2_RUNNING,
        "inspector_2_done": PHASE_INSPECTOR_2_DONE,
        "finalizing": PHASE_FINALIZING,
        "blocked": PHASE_TERMINAL,
    }
    return mapping.get(value, PHASE_UNKNOWN)


def _normalize_phase(value: str | None, substep: str | None = None) -> str:
    if value in CONTROLLED_PHASES:
        return value
    return _phase_from_substep(substep)


def _normalize_status(value: str | None) -> str:
    if value in CONTROLLED_STATUSES:
        return value
    legacy = (value or "").strip().lower()
    if legacy == "completed":
        return STATUS_MAX_ROUNDS_UNAPPROVED
    if legacy == "failed":
        return STATUS_FAILED_NONRESUMABLE
    if legacy == "interrupted":
        return STATUS_INTERRUPTED_RESUMABLE
    if legacy == "inspector_failed":
        return STATUS_REVIEW_FAILED
    return STATUS_RUNNING


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_quality_mode(value: str | None, *, strict: bool = True) -> str:
    if value is None or str(value).strip() == "":
        return DEFAULT_QUALITY_MODE
    normalized = str(value).strip().lower()
    if normalized in QUALITY_MODES:
        return normalized
    if strict:
        expected = ", ".join(sorted(QUALITY_MODES))
        raise ValueError(f"Invalid quality mode: {value}. Expected one of: {expected}.")
    return DEFAULT_QUALITY_MODE


def _managed_files_state() -> list[str]:
    return list(KNOWN_MANAGED_ARTIFACTS)


def _normalize_state_v3(data: dict | None, working_dir: str | None = None) -> dict | None:
    if data is None:
        return None
    now = _utc_timestamp()
    legacy_substep = data.get("current_substep") or data.get("stage") or ""
    user_query = data.get("user_query", "")
    output_files = list(data.get("output_files") or data.get("requested_outputs") or [])
    source_files = list(data.get("source_files") or [])
    stored_required_source_files = list(data.get("required_source_files") or [])
    stored_source_manifest = list(data.get("source_manifest") or [])
    saved_source_context_version = data.get("source_context_version")
    if saved_source_context_version == SOURCE_CONTEXT_VERSION:
        required_source_files = stored_required_source_files or source_files
    else:
        required_source_files = []
        if working_dir and user_query:
            try:
                required_source_files = [
                    item["path"]
                    for item in _extract_user_mentioned_source_paths(
                        user_query,
                        working_dir,
                        output_files,
                    )
                ]
            except Exception:
                required_source_files = []
    saved_working_dir = data.get("working_dir")
    status = _normalize_status(data.get("status"))
    phase = _normalize_phase(data.get("phase"), legacy_substep)
    ci_mode_explicit = bool(data.get("ci_mode_explicit", False))
    interactive_requested = bool(data.get("interactive_requested", False))
    quality_mode = _normalize_quality_mode(data.get("quality_mode"), strict=False)
    issue_state = data.get("issue_state") or (data.get("review_state") or {}).get("issue_state") or {}
    explicit_manual = interactive_requested or (
        ci_mode_explicit and data.get("ci_mode") is False
    )
    normalized_ci_mode = False if explicit_manual else True
    if status in TERMINAL_STATUSES and status != STATUS_INTERRUPTED_RESUMABLE:
        phase = PHASE_TERMINAL

    state = {
        "state_version": STATE_VERSION,
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": data.get("run_id") or data.get("session_id") or uuid.uuid4().hex,
        "session_id": data.get("session_id") or uuid.uuid4().hex,
        "created_at": data.get("created_at") or now,
        "updated_at": now,
        "phase": phase,
        "status": status,
        "final_status": data.get("final_status") or (status if status in TERMINAL_STATUSES else None),
        "current_round": _safe_int(data.get("current_round", data.get("round_num", 1)), 1),
        "max_rounds": _safe_int(data.get("max_rounds"), MAX_ROUNDS),
        "user_query": user_query,
        "original_request_hash": data.get("original_request_hash") or _hash_text(user_query),
        "requested_outputs": list(data.get("requested_outputs") or output_files),
        "output_files": output_files,
        "source_files": source_files,
        "source_context_version": SOURCE_CONTEXT_VERSION,
        "required_source_files": required_source_files,
        "source_manifest": stored_source_manifest,
        "legacy_source_files": [] if saved_source_context_version == SOURCE_CONTEXT_VERSION else source_files,
        "managed_files": list(data.get("managed_files") or _managed_files_state()),
        "researcher_session_id": data.get("researcher_session_id"),
        "inspector_session_id": data.get("inspector_session_id"),
        "inspector_2_session_id": data.get("inspector_2_session_id"),
        "user_updates": list(data.get("user_updates") or []),
        "blocked_state": data.get("blocked_state"),
        "preflight_state": data.get("preflight_state"),
        "verification_mode": data.get("verification_mode") or DEFAULT_VERIFICATION_MODE,
        "max_verification_timeout": _safe_int(data.get("max_verification_timeout"), DEFAULT_MAX_VERIFICATION_TIMEOUT),
        "second_inspector_mode": data.get("second_inspector_mode") or DEFAULT_SECOND_INSPECTOR_MODE,
        "log_prompts_mode": data.get("log_prompts_mode") or DEFAULT_LOG_PROMPTS_MODE,
        "quality_mode": quality_mode,
        "result_file": data.get("result_file") or CONGRESS_RESULT_FILE,
        "strict_exit_codes": bool(data.get("strict_exit_codes", True)),
        "ci_mode": normalized_ci_mode,
        "ci_mode_explicit": ci_mode_explicit,
        "interactive_requested": interactive_requested,
        "retry_state": data.get("retry_state"),
        "task_classification": data.get("task_classification") or {},
        "verification_state": data.get("verification_state") or {},
        "verification_checks": list(data.get("verification_checks") or []),
        "review_state": data.get("review_state") or {},
        "issue_state": issue_state if isinstance(issue_state, dict) else {"issues": list(issue_state or [])},
        "latest_verification_status": data.get("latest_verification_status") or "not_started",
        "inspector_1_status": data.get("inspector_1_status") or (data.get("review_state") or {}).get("inspector_1_status"),
        "inspector_2_status": data.get("inspector_2_status") or (data.get("review_state") or {}).get("inspector_2_status"),
        "final_approval_status": data.get("final_approval_status"),
        "last_result": data.get("last_result"),
        # Compatibility fields used by the current interactive resume flow and old fixtures.
        "current_substep": legacy_substep,
        "legacy_state_version": data.get("state_version", data.get("version")),
        "legacy_status": data.get("status"),
        "version": data.get("version"),
    }
    if saved_working_dir:
        state["working_dir"] = os.path.abspath(saved_working_dir)
    elif working_dir:
        state["working_dir"] = os.path.abspath(working_dir)
    return state


def _build_state_v3(
    *,
    working_dir: str,
    session_id: str,
    user_query: str,
    output_files: list[str],
    source_files: list[str],
    max_rounds: int,
    current_round: int,
    current_substep: str,
    required_source_files: list[str] | None = None,
    source_manifest: list[dict] | None = None,
    source_context_version: int = SOURCE_CONTEXT_VERSION,
    status: str = STATUS_RUNNING,
    final_status: str | None = None,
    researcher_session_id: str | None = None,
    inspector_session_id: str | None = None,
    inspector_2_session_id: str | None = None,
    last_result: dict | None = None,
    user_updates: list[dict] | None = None,
    blocked_state: dict | None = None,
    preflight_state: dict | None = None,
    verification_mode: str = DEFAULT_VERIFICATION_MODE,
    max_verification_timeout: int = DEFAULT_MAX_VERIFICATION_TIMEOUT,
    second_inspector_mode: str = DEFAULT_SECOND_INSPECTOR_MODE,
    log_prompts_mode: str = DEFAULT_LOG_PROMPTS_MODE,
    quality_mode: str = DEFAULT_QUALITY_MODE,
    result_file: str = CONGRESS_RESULT_FILE,
    strict_exit_codes: bool = True,
    ci_mode: bool = True,
    ci_mode_explicit: bool = False,
    interactive_requested: bool = False,
    retry_state: dict | None = None,
    task_classification: dict | None = None,
    verification_state: dict | None = None,
    verification_checks: list[dict] | None = None,
    review_state: dict | None = None,
    issue_state: dict | None = None,
) -> dict:
    base = {
        "state_version": STATE_VERSION,
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": session_id,
        "session_id": session_id,
        "created_at": _utc_timestamp(),
        "phase": _phase_from_substep(current_substep),
        "status": status,
        "final_status": final_status,
        "current_round": current_round,
        "max_rounds": max_rounds,
        "user_query": user_query,
        "original_request_hash": _hash_text(user_query),
        "requested_outputs": list(output_files),
        "output_files": list(output_files),
        "source_files": list(source_files),
        "source_context_version": source_context_version,
        "required_source_files": list(required_source_files if required_source_files is not None else source_files),
        "source_manifest": list(source_manifest or []),
        "legacy_source_files": None,
        "managed_files": _managed_files_state(),
        "researcher_session_id": researcher_session_id,
        "inspector_session_id": inspector_session_id,
        "inspector_2_session_id": inspector_2_session_id,
        "user_updates": list(user_updates or []),
        "blocked_state": blocked_state,
        "preflight_state": preflight_state,
        "verification_mode": verification_mode,
        "max_verification_timeout": max_verification_timeout,
        "second_inspector_mode": second_inspector_mode,
        "log_prompts_mode": log_prompts_mode,
        "quality_mode": _normalize_quality_mode(quality_mode, strict=False),
        "result_file": result_file,
        "strict_exit_codes": bool(strict_exit_codes),
        "ci_mode": bool(ci_mode),
        "ci_mode_explicit": bool(ci_mode_explicit),
        "interactive_requested": bool(interactive_requested),
        "retry_state": retry_state,
        "task_classification": task_classification or {},
        "verification_state": verification_state or {},
        "verification_checks": list(verification_checks or []),
        "review_state": review_state or {},
        "issue_state": issue_state or {},
        "latest_verification_status": _verification_summary_for_current_context(verification_state).get("status", "not_started"),
        "inspector_1_status": (review_state or {}).get("inspector_1_status"),
        "inspector_2_status": (review_state or {}).get("inspector_2_status"),
        "final_approval_status": status if status in APPROVED_STATUSES else None,
        "last_result": last_result,
        "current_substep": current_substep,
        "legacy_state_version": None,
        "legacy_status": None,
        "version": None,
    }
    state = _normalize_state_v3(base, working_dir) or base
    state["created_at"] = base["created_at"]
    state["updated_at"] = _utc_timestamp()
    state["phase"] = (
        PHASE_TERMINAL
        if status in TERMINAL_STATUSES and status != STATUS_INTERRUPTED_RESUMABLE
        else _phase_from_substep(current_substep)
    )
    state["status"] = status
    state["final_status"] = final_status or (status if status in TERMINAL_STATUSES else None)
    state["source_context_version"] = source_context_version
    state["required_source_files"] = list(required_source_files if required_source_files is not None else source_files)
    state["source_manifest"] = list(source_manifest or [])
    state["last_result"] = last_result
    state["user_updates"] = list(user_updates or [])
    state["blocked_state"] = blocked_state
    state["preflight_state"] = preflight_state
    state["verification_mode"] = verification_mode
    state["max_verification_timeout"] = max_verification_timeout
    state["second_inspector_mode"] = second_inspector_mode
    state["log_prompts_mode"] = log_prompts_mode
    state["quality_mode"] = _normalize_quality_mode(quality_mode, strict=False)
    state["result_file"] = result_file
    state["strict_exit_codes"] = bool(strict_exit_codes)
    state["ci_mode"] = bool(ci_mode)
    state["ci_mode_explicit"] = bool(ci_mode_explicit)
    state["interactive_requested"] = bool(interactive_requested)
    state["retry_state"] = retry_state
    state["task_classification"] = task_classification or {}
    state["verification_state"] = verification_state or {}
    state["verification_checks"] = list(verification_checks or [])
    state["review_state"] = review_state or {}
    state["issue_state"] = issue_state or {}
    state["latest_verification_status"] = _verification_summary_for_current_context(verification_state).get("status", "not_started")
    state["inspector_1_status"] = (review_state or {}).get("inspector_1_status")
    state["inspector_2_status"] = (review_state or {}).get("inspector_2_status")
    return state


def _load_state(working_dir: str, normalize: bool = False) -> dict | None:
    """Load congress_state.json from working_dir. Returns None if missing/corrupt."""
    try:
        with open(_state_path(working_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
            return _normalize_state_v3(data, working_dir) if normalize else data
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


def _load_gitignore_patterns(working_dir: str) -> list[str]:
    path = os.path.join(working_dir, ".gitignore")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    patterns = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line.strip("/"))
    return patterns


def _matches_ignore_pattern(relpath: str, patterns: list[str]) -> bool:
    rel = relpath.replace("\\", "/")
    name = os.path.basename(rel)
    for pattern in patterns:
        pat = pattern.replace("\\", "/").strip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if "/" not in pat and any(part == pat for part in rel.split("/")):
            return True
        if rel.startswith(pat + "/"):
            return True
    return False


def _is_probably_binary(path: str, sample_size: int = 4096) -> bool:
    try:
        with open(path, "rb") as f:
            sample = f.read(sample_size)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control / max(1, len(sample)) > 0.20


def _source_context_relkey(working_dir: str, raw_path: str) -> str:
    rel = _relpath_from_workspace(working_dir, raw_path)
    return _relpath_key(rel)


def _source_output_relkeys(working_dir: str, output_files: list[str] | None,
                           excluded_relpaths: list[str] | None = None) -> set[str]:
    keys: set[str] = set()
    for item in list(output_files or []) + list(excluded_relpaths or []):
        keys.add(_source_context_relkey(working_dir, item))
    return keys


def _clean_source_path_candidate(raw: str) -> str:
    value = str(raw or "").strip().strip("`").strip('"').strip("'").strip("<>")
    value = value.strip()
    value = value.rstrip(").,;")
    return value.strip()


def _is_url_path_candidate(value: str) -> bool:
    return bool(re.match(r"(?i)^[a-z][a-z0-9+.-]*://", value or ""))


def _source_relpath_from_candidate(working_dir: str, raw: str) -> tuple[str, str] | None:
    candidate = _clean_source_path_candidate(raw)
    if not candidate or _is_url_path_candidate(candidate):
        return None
    if "\n" in candidate or "\r" in candidate or len(candidate) > 260:
        return None
    normalized = candidate.replace("/", os.sep).replace("\\", os.sep)
    root = _workspace_root(working_dir)
    if os.path.isabs(normalized):
        abs_path = os.path.abspath(os.path.normpath(normalized))
        if not _path_within(root, abs_path):
            return None
    else:
        if normalized in (".", ".."):
            return None
        abs_path = os.path.abspath(os.path.normpath(os.path.join(root, normalized)))
        if not _path_within(root, abs_path):
            return None
    if not os.path.isfile(abs_path):
        return None
    relpath = os.path.relpath(abs_path, root).replace("\\", "/")
    return relpath, abs_path


def _source_workspace_name_matches(working_dir: str, filename: str) -> list[tuple[str, str]]:
    clean = _clean_source_path_candidate(filename)
    if not clean or "/" in clean or "\\" in clean or _is_url_path_candidate(clean):
        return []
    root = _workspace_root(working_dir)
    matches: list[tuple[str, str]] = []
    target = clean.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SOURCE_CONTEXT_PRUNE_DIRS and not d.startswith(".git")
        )
        for name in sorted(filenames):
            if name.lower() != target:
                continue
            abs_path = os.path.join(dirpath, name)
            relpath = os.path.relpath(abs_path, root).replace("\\", "/")
            matches.append((relpath, abs_path))
        if len(matches) >= 4:
            break
    return matches


def _source_path_candidates_from_user_query(user_query: str) -> list[str]:
    text = re.sub(r"https?://[^\s`'\"<>]+", " ", user_query or "", flags=re.IGNORECASE)
    candidates: list[str] = []
    for match in re.finditer(r"[`'\"]([^`'\"]{1,260})[`'\"]", text):
        candidates.append(match.group(1))
    path_pattern = re.compile(
        r"(?<![\w./:-])("
        r"(?:[A-Za-z]:\\[^\s`'\"<>|]+)"
        r"|(?:\.{1,2}[\\/])?(?:[A-Za-z0-9_@+(). -]+[\\/])*"
        r"[A-Za-z0-9_@+().-]+\."
        r"(?:md|txt|py|json|yaml|yml|toml|csv|ini|cfg|conf|log|html|css|scss|"
        r"js|jsx|ts|tsx|sh|ps1|bat|cmd|doc|docx|pdf|env)"
        r"|\.env(?:\.[A-Za-z0-9_-]+)?"
        r")",
        re.IGNORECASE,
    )
    for match in path_pattern.finditer(text):
        candidates.append(match.group(1))
    return candidates


def _source_manifest_item(path: str, relpath: str, *, tier: int, tier_name: str,
                          reason: str, required: bool = False,
                          optional: bool = False, excluded: bool = False,
                          sensitive: bool = False,
                          priority: int | None = None,
                          size: int | None = None) -> dict:
    if size is None:
        try:
            size = os.stat(path).st_size
        except OSError:
            size = 0
    if priority is None:
        priority, _ = _source_priority_and_reason(relpath)
    return {
        "path": path,
        "relpath": relpath.replace("\\", "/"),
        "size": size,
        "priority": priority,
        "reason": reason,
        "tier": tier,
        "tier_name": tier_name,
        "required": bool(required),
        "optional": bool(optional),
        "excluded": bool(excluded),
        "sensitive": bool(sensitive),
        "source_context_version": SOURCE_CONTEXT_VERSION,
    }


def _source_context_exclusion(path: str, relpath: str, stat: os.stat_result | None = None,
                              *, direct_request: bool = False) -> tuple[str | None, bool]:
    rel = relpath.replace("\\", "/")
    lower = rel.lower()
    name = os.path.basename(lower)
    parts = lower.split("/")
    sensitive = False

    if _is_reserved_output_relpath(rel):
        return "Congress-managed artifact", False
    if any(part in SOURCE_CONTEXT_EXCLUDED_DIR_PARTS for part in parts):
        sensitive = any(part in ("secrets", "secret") for part in parts)
        if CONGRESS_ROUNDS_DIR.lower() in parts:
            return "old Congress run artifact", False
        if "logs" in parts:
            return "generated log artifact", sensitive
        return "sensitive secrets directory", True
    if name == ".env" or name.startswith(".env."):
        return "environment/secret file", True
    if re.search(SOURCE_CONTEXT_SENSITIVE_NAME_PATTERN, name):
        return "credential-like file name", True
    if name.endswith(".log"):
        return "generated log artifact", False
    if name in ("congress.py", "congress2.py") and not direct_request:
        return "Congress internals not directly requested", False
    if any(part in SOURCE_CONTEXT_PRUNE_DIRS for part in parts):
        return "cache/build/dependency directory", False
    try:
        size = stat.st_size if stat is not None else os.stat(path).st_size
    except OSError:
        return "unreadable source path", False
    if size > SOURCE_MANIFEST_MAX_BYTES:
        return "huge file above source-context limit", False
    if _is_probably_binary(path):
        return "binary file", False
    return None, sensitive


def _extract_user_mentioned_source_paths(user_query: str, working_dir: str,
                                         output_files: list[str] | None = None) -> list[dict]:
    output_keys = _source_output_relkeys(working_dir, output_files)
    seen: set[str] = set()
    entries: list[dict] = []
    for candidate in _source_path_candidates_from_user_query(user_query):
        resolved = _source_relpath_from_candidate(working_dir, candidate)
        if resolved is None:
            name_matches = _source_workspace_name_matches(working_dir, candidate)
        else:
            name_matches = [resolved]
        for relpath, abs_path in name_matches:
            key = _relpath_key(relpath)
            if key in seen or key in output_keys:
                continue
            seen.add(key)
            try:
                stat = os.stat(abs_path)
            except OSError:
                continue
            exclusion_reason, sensitive = _source_context_exclusion(
                abs_path,
                relpath,
                stat,
                direct_request=True,
            )
            reason = "user-mentioned required source"
            if exclusion_reason:
                reason = f"direct user-mentioned source; normally excluded by default: {exclusion_reason}"
            entries.append(
                _source_manifest_item(
                    abs_path,
                    relpath,
                    tier=SOURCE_CONTEXT_TIER_REQUIRED,
                    tier_name="user_mentioned_required",
                    reason=reason,
                    required=True,
                    optional=False,
                    excluded=False,
                    sensitive=sensitive,
                    priority=0,
                    size=stat.st_size,
                )
            )
    return entries


def _caller_provided_source_items(working_dir: str,
                                  caller_source_files: list[str] | None = None) -> list[dict]:
    seen: set[str] = set()
    entries: list[dict] = []
    for item in caller_source_files or []:
        resolved = _source_relpath_from_candidate(working_dir, item)
        if resolved is None:
            continue
        relpath, abs_path = resolved
        key = _relpath_key(relpath)
        if key in seen:
            continue
        seen.add(key)
        try:
            stat = os.stat(abs_path)
        except OSError:
            continue
        exclusion_reason, sensitive = _source_context_exclusion(
            abs_path,
            relpath,
            stat,
            direct_request=True,
        )
        reason = "caller-provided required source"
        if exclusion_reason:
            reason = f"caller-provided required source; normally excluded by default: {exclusion_reason}"
        entries.append(
            _source_manifest_item(
                abs_path,
                relpath,
                tier=SOURCE_CONTEXT_TIER_REQUIRED,
                tier_name="caller_provided_required",
                reason=reason,
                required=True,
                optional=False,
                excluded=False,
                sensitive=sensitive,
                priority=0,
                size=stat.st_size,
            )
        )
    return entries


def _source_priority_and_reason(relpath: str) -> tuple[int, str]:
    rel = relpath.replace("\\", "/")
    name = os.path.basename(rel).lower()
    parts = rel.lower().split("/")
    if name == "agents.md":
        return 0, "agent guidance"
    if name.startswith("readme"):
        return 1, "readme"
    if name in (
        "pyproject.toml", "package.json", "requirements.txt", "setup.py",
        "pom.xml", "go.mod", "cargo.toml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "gradlew", "gradlew.bat",
    ) or name.endswith((".sln", ".csproj", ".fsproj", ".vbproj")):
        return 2, "project manifest"
    if "tests" in parts or "test" in parts:
        return 3, "tests"
    if parts[0] in ("src", "app", "lib", "packages"):
        return 4, "source"
    if parts[0] in ("docs", "doc"):
        return 5, "docs"
    if name.endswith((".toml", ".yaml", ".yml", ".json")):
        return 6, "config"
    return 10, "source"


def _build_source_manifest(working_dir: str, excluded_relpaths: list[str] | None = None,
                           max_files: int = SOURCE_MANIFEST_MAX_FILES,
                           user_query: str | None = None,
                           output_files: list[str] | None = None,
                           caller_source_files: list[str] | None = None) -> list[dict]:
    source_extensions = {
        ".txt", ".md", ".pdf", ".doc", ".docx",
        ".json", ".yaml", ".yml", ".toml", ".csv",
        ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb",
        ".html", ".css", ".scss", ".sh", ".ps1", ".bat", ".cmd",
        ".mjs", ".cjs", ".kt", ".kts", ".cs", ".fs", ".vb", ".sln",
        ".csproj", ".fsproj", ".vbproj", ".gradle",
    }
    always_names = {
        "AGENTS.md", "README", "README.md", "package.json", "pyproject.toml",
        "requirements.txt", "setup.py", "go.mod", "Cargo.toml", "pom.xml",
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "gradlew", "gradlew.bat",
    }
    excluded_norm = _source_output_relkeys(working_dir, output_files, excluded_relpaths)
    gitignore_patterns = _load_gitignore_patterns(working_dir)
    root = os.path.abspath(working_dir)
    required_items: list[dict] = []
    required_seen: set[str] = set()

    for item in _extract_user_mentioned_source_paths(user_query or "", working_dir, output_files):
        key = _relpath_key(item.get("relpath") or "")
        if key and key not in required_seen:
            required_seen.add(key)
            required_items.append(item)
    for item in _caller_provided_source_items(working_dir, caller_source_files):
        key = _relpath_key(item.get("relpath") or "")
        if key and key not in required_seen:
            required_seen.add(key)
            required_items.append(item)

    optional_items: list[dict] = []
    excluded_items: list[dict] = []
    excluded_seen: set[str] = set()

    def add_excluded(path: str, relpath: str, reason: str, sensitive: bool,
                     stat: os.stat_result | None = None) -> None:
        key = _relpath_key(relpath)
        if key in excluded_seen or key in required_seen:
            return
        excluded_seen.add(key)
        if len(excluded_items) >= SOURCE_CONTEXT_EXCLUDED_SUMMARY_MAX:
            return
        size = stat.st_size if stat is not None else None
        excluded_items.append(
            _source_manifest_item(
                path,
                relpath,
                tier=SOURCE_CONTEXT_TIER_EXCLUDED,
                tier_name="excluded_by_default",
                reason=reason,
                required=False,
                optional=False,
                excluded=True,
                sensitive=sensitive,
                priority=99,
                size=size,
            )
        )

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        filtered_dirs = []
        for dirname in sorted(dirnames):
            rel = f"{rel_dir}/{dirname}".strip("/")
            key = _relpath_key(rel)
            lower_parts = rel.lower().split("/")
            if dirname in SOURCE_CONTEXT_PRUNE_DIRS or key in excluded_norm or _matches_ignore_pattern(rel, gitignore_patterns):
                continue
            if any(part in SOURCE_CONTEXT_PRUNE_DIRS for part in lower_parts):
                continue
            filtered_dirs.append(dirname)
        dirnames[:] = filtered_dirs

        for filename in sorted(filenames):
            relpath = f"{rel_dir}/{filename}".strip("/")
            key = _relpath_key(relpath)
            if key in required_seen:
                continue
            if key in excluded_norm:
                continue
            path = os.path.join(root, relpath.replace("/", os.sep))
            try:
                stat = os.stat(path)
            except OSError:
                continue
            exclusion_reason, sensitive = _source_context_exclusion(path, relpath, stat)
            if exclusion_reason:
                add_excluded(path, relpath, exclusion_reason, sensitive, stat)
                continue
            if filename.startswith(".") and filename != ".gitignore":
                continue
            if _matches_ignore_pattern(relpath, gitignore_patterns):
                continue
            if stat.st_size > SOURCE_MANIFEST_MAX_BYTES:
                add_excluded(path, relpath, "huge file above source-context limit", False, stat)
                continue
            ext = os.path.splitext(filename)[1].lower()
            if filename not in always_names and ext not in source_extensions:
                continue
            if _is_probably_binary(path):
                add_excluded(path, relpath, "binary file", False, stat)
                continue
            priority, reason = _source_priority_and_reason(relpath)
            tier = (
                SOURCE_CONTEXT_TIER_PROJECT
                if priority <= 2
                else SOURCE_CONTEXT_TIER_OPTIONAL
            )
            tier_name = (
                "project_context"
                if tier == SOURCE_CONTEXT_TIER_PROJECT
                else "optional_discoverable"
            )
            optional_items.append(
                _source_manifest_item(
                    path,
                    relpath,
                    tier=tier,
                    tier_name=tier_name,
                    reason=reason,
                    required=False,
                    optional=True,
                    excluded=False,
                    sensitive=False,
                    priority=priority,
                    size=stat.st_size,
                )
            )
    optional_items.sort(key=lambda item: (item["tier"], item["priority"], item["relpath"].lower()))
    excluded_items.sort(key=lambda item: (item["reason"], item["relpath"].lower()))
    optional_limit = max(0, max_files - len(required_items))
    return required_items + optional_items[:optional_limit] + excluded_items[:SOURCE_CONTEXT_EXCLUDED_SUMMARY_MAX]


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
        CONGRESS_RESULT_FILE,
        CONGRESS_HISTORY_FILE,
        CONGRESS_VERIFICATION_FILE,
        CONGRESS_BLOCKED_FILE,
        CONGRESS_LOCK_FILE,
        RESEARCHER_UPDATED_FILE, # living researcher doc (auto-generated)
        INSPECTOR_COMMENTS_FILE, # living inspector doc (auto-generated)
        INSPECTOR_2_COMMENTS_FILE,
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


def _extract_preflight_requirements(user_query: str, output_files: list[str] | None = None,
                                    source_manifest: list[dict] | None = None,
                                    working_dir: str | None = None) -> list[dict]:
    text = user_query or ""
    requirements: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(req: dict) -> None:
        key = (req.get("type", ""), req.get("value") or req.get("name") or req.get("url") or req.get("label", ""))
        if key in seen:
            return
        seen.add(key)
        req.setdefault("essential", True)
        requirements.append(req)

    for match in re.finditer(r'https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?[^\s<>)"\']*', text, re.I):
        url = match.group(0).rstrip(".,;")
        add({"type": "local_url", "url": url, "value": url, "label": url, "category": "environment"})
    for match in re.finditer(r'\b(?:localhost|127\.0\.0\.1):(\d{2,5})\b', text, re.I):
        url = f"http://localhost:{match.group(1)}"
        add({"type": "local_url", "url": url, "value": url, "label": url, "category": "environment"})

    env_context = re.compile(
        r'\b(?i:requires?|needs?|need|missing|set|provide|with|using|configure)\b.{0,100}?\b'
        r'([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASS|KEY|CREDENTIAL)[A-Z0-9_]*|'
        r'[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b'
    )
    for match in env_context.finditer(text):
        name = _valid_requirement_name(match.group(1))
        if name:
            add({"type": "env_var", "name": name, "value": name, "label": name, "category": "user"})

    tool_context = re.compile(
        r'(?i)\b(?:requires?|needs?|need|missing)\s+(?:the\s+)?(?:command|tool|binary)\s+[`\'"]?([A-Za-z0-9_.-]+)'
    )
    for match in tool_context.finditer(text):
        name = _valid_requirement_name(match.group(1))
        if name:
            add({"type": "tool", "name": name, "value": name, "label": name, "category": "environment"})

    def add_connector(kind: str, name_value: str) -> None:
        kind = (kind or "").lower()
        name = _valid_requirement_name(name_value)
        if not name:
            return
        req_type = "mcp_connector" if kind == "mcp" else "app_connector"
        category = "environment" if kind == "mcp" else "user"
        label = f"{kind} connector {name}"
        add({"type": req_type, "name": name, "value": name, "label": label, "category": category})

    def add_service(name_value: str) -> None:
        name = _valid_requirement_name(name_value)
        if name:
            add({"type": "service", "name": name, "value": name, "label": f"service {name}", "category": "environment"})

    connector_context = re.compile(
        r'(?i)\b(?:requires?|needs?|need|missing)\b.{0,40}?\b(mcp|app)\s+connector\s+[`\'"]?([A-Za-z0-9_.:/-]+)'
    )
    for match in connector_context.finditer(text):
        add_connector(match.group(1), match.group(2))

    connector_before_context = re.compile(
        r'(?i)\b(?:requires?|needs?|need|missing)\b.{0,70}?\b[`\'"]?([A-Za-z0-9_.:/-]+)\s+(mcp|app)\s+connector\b'
    )
    for match in connector_before_context.finditer(text):
        add_connector(match.group(2), match.group(1))

    service_context = re.compile(
        r'(?i)\b(?:requires?|needs?|need|missing)\b.{0,40}?\b(?:service|database)\s+[`\'"]?([A-Za-z0-9_.:/-]+)'
    )
    for match in service_context.finditer(text):
        add_service(match.group(1))

    service_before_context = re.compile(
        r'(?i)\b(?:requires?|needs?|need|missing)\b.{0,70}?\b[`\'"]?([A-Za-z0-9_.:/-]+)\s+(?:service|database)\b'
    )
    for match in service_before_context.finditer(text):
        add_service(match.group(1))

    if re.search(r'(?i)\b(?:requires?|needs?|need)\b.{0,40}\b(browser|chrome|playwright|screenshot|ui)\b', text):
        add({"type": "browser", "value": "browser", "label": "browser tooling", "category": "environment"})

    if re.search(r'(?i)\b(?:requires?|needs?|need)\b.{0,50}\b(package registry|npm registry|pypi|network)\b', text):
        add({"type": "package_registry", "value": "package registry", "label": "package registry/network", "category": "environment"})

    for item in source_manifest or []:
        if item.get("excluded"):
            continue
        name = os.path.basename(item.get("relpath", "")).lower()
        if name == "package.json":
            add({"type": "tool", "name": "npm", "value": "npm", "label": "npm from package.json", "category": "environment", "essential": False})
        elif name == "pyproject.toml" or name == "requirements.txt":
            add({"type": "tool", "name": "python", "value": "python", "label": "python project tooling", "category": "environment", "essential": False})

    return requirements


def _probe_preflight_requirement(requirement: dict) -> dict:
    req = dict(requirement)
    req_type = req.get("type")
    ok = True
    message = "available"
    if req_type == "local_url":
        url = req.get("url") or req.get("value") or ""
        host, port = _host_port_from_url(url)
        ok = bool(host and port and _probe_tcp(host, port))
        message = "reachable" if ok else f"unreachable local service: {url}"
    elif req_type == "env_var":
        name = req.get("name") or req.get("value") or ""
        ok = bool(name and os.environ.get(name))
        message = (
            f"required environment variable {name} is present"
            if ok else
            f"required environment variable {name} is missing"
        )
    elif req_type == "tool":
        name = req.get("name") or req.get("value") or ""
        ok = bool(name and shutil.which(name))
        message = f"{name}: present" if ok else f"{name}: missing on PATH"
    elif req_type == "browser":
        browsers = _tool_names_for_browser()
        found = [name for name in browsers if shutil.which(name)]
        ok = bool(found)
        message = "browser present: " + ", ".join(found) if ok else "browser tooling missing"
    elif req_type == "package_registry":
        try:
            urllib.request.urlopen("https://registry.npmjs.org/", timeout=PREFLIGHT_TIMEOUT_SECONDS).close()
            ok = True
            message = "package registry reachable"
        except Exception:
            ok = False
            message = "package registry/network unreachable"
    elif req_type == "service":
        label = req.get("label") or req.get("name") or req.get("value") or "service"
        ok = False
        message = f"required service not confirmed available: {label}"
    elif req_type == "mcp_connector":
        label = req.get("label") or req.get("name") or req.get("value") or "MCP connector"
        ok = False
        message = f"required MCP connector not confirmed available: {label}"
    elif req_type == "app_connector":
        label = req.get("label") or req.get("name") or req.get("value") or "app connector"
        ok = False
        message = f"required app connector not confirmed connected: {label}"
    result = {
        "requirement": _redact_sensitive_data(req),
        "ok": ok,
        "message": _redact_sensitive_text(message),
        "timestamp": _utc_timestamp(),
    }
    return result


def _run_preflight_requirements(requirements: list[dict],
                                probe_func=None) -> dict:
    probe = probe_func or _probe_preflight_requirement
    results = [probe(req) for req in requirements]
    blocked = None
    for result in results:
        req = result.get("requirement") or {}
        if result.get("ok") or req.get("essential") is False:
            continue
        category = req.get("category") or ("user" if req.get("type") == "env_var" else "environment")
        status = _blocked_status_for_category(category)
        label = req.get("label") or req.get("value") or req.get("name") or req.get("url") or req.get("type")
        if req.get("type") == "app_connector":
            action = f"Connect or authorize the required app connector `{label}`, then resume."
        elif req.get("type") == "mcp_connector":
            action = f"Enable the required MCP connector `{label}`, then resume."
        elif req.get("type") == "service":
            action = f"Start or make available the required service `{label}`, then resume."
        elif category == "user":
            action = (
                f"Provide required user input or set credential `{label}` through the environment, "
                "then resume."
            )
        else:
            action = f"Start or install the required environment dependency `{label}`, then resume."
        blocked = _make_blocked_state(
            status,
            category,
            result.get("message") or f"Preflight requirement failed: {label}",
            action,
            "congress_preflight",
            evidence=["Congress-owned preflight probe failed before launching Codex."],
            requirement=req,
        )
        break
    return {
        "requirements": _redact_sensitive_data(requirements),
        "results": _redact_sensitive_data(results),
        "blocked": blocked,
        "timestamp": _utc_timestamp(),
    }


def _collect_capability_inventory(working_dir: str, codex_bin: str | None = None,
                                  requirements: list[dict] | None = None) -> dict:
    tools_to_check = [
        "python", "git", "node", "npm", "pip", "pytest", "playwright",
        *_tool_names_for_browser(),
    ]
    tools = {name: bool(shutil.which(name)) for name in sorted(set(tools_to_check))}
    credentials = {
        name: "present"
        for name, value in os.environ.items()
        if value and _extract_credential_names(name)
    }
    hints = []
    for rel in ("AGENTS.md", ".codex/config.toml", ".codex", "package.json", "pyproject.toml"):
        path = os.path.join(working_dir, rel)
        if os.path.exists(path):
            hints.append(rel)
    capability_hints = []
    for rel in (
        ".codex/hooks",
        ".codex/skills",
        ".mcp.json",
        "mcp.json",
        "mcp.config.json",
        ".cursor/mcp.json",
        "connectors.json",
        "apps.json",
    ):
        path = os.path.join(working_dir, rel)
        if os.path.exists(path):
            capability_hints.append(rel)
    codex_available = bool(codex_bin and (os.path.exists(codex_bin) or shutil.which(codex_bin)))
    codex = {"path": codex_bin, "available": codex_available, "version": "not_checked"}
    if codex_available and codex_bin:
        try:
            proc = subprocess.run(
                [codex_bin, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
                check=False,
            )
            version = (proc.stdout or proc.stderr or "").strip().splitlines()
            codex["version"] = version[0] if version else "unknown"
        except Exception as exc:
            codex["version"] = f"unavailable: {type(exc).__name__}"
    return _redact_sensitive_data({
        "timestamp": _utc_timestamp(),
        "codex": codex,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "tools": tools,
        "credentials": credentials,
        "project_hints": hints,
        "capability_hints": capability_hints,
        "preflight_requirements": requirements or [],
    })


# ============================================================================
# TASK CLASSIFICATION & VERIFICATION HELPERS
# ============================================================================

def _task_classification_to_dict(classification: TaskClassification | dict | None) -> dict:
    if classification is None:
        return {}
    if isinstance(classification, TaskClassification):
        return asdict(classification)
    return dict(classification)


def _verification_check_to_dict(check: VerificationCheck | dict) -> dict:
    if isinstance(check, VerificationCheck):
        return asdict(check)
    return dict(check)


def _verification_check_from_dict(check: VerificationCheck | dict | None) -> VerificationCheck | None:
    if check is None:
        return None
    if isinstance(check, VerificationCheck):
        return check
    if not isinstance(check, dict):
        return None
    allowed = set(VerificationCheck.__dataclass_fields__.keys())
    values = {key: value for key, value in check.items() if key in allowed}
    try:
        return VerificationCheck(**values)
    except TypeError:
        return None


def _verification_command_display(command: list[str] | None) -> str:
    if not command:
        return ""
    parts = []
    for part in command:
        value = str(part)
        if not value:
            parts.append('""')
        elif re.search(r"\s", value):
            parts.append('"' + value.replace('"', '\\"') + '"')
        else:
            parts.append(value)
    return " ".join(parts)


def _excerpt_output(text: str, max_chars: int = VERIFICATION_OUTPUT_EXCERPT_CHARS) -> str:
    redacted = _redact_sensitive_text(text or "")
    if len(redacted) <= max_chars:
        return redacted
    half = max_chars // 2
    omitted = len(redacted) - max_chars
    return (
        redacted[:half]
        + f"\n[... {omitted} chars omitted from verification excerpt ...]\n"
        + redacted[-half:]
    )


def _relpath_from_workspace(working_dir: str, path: str) -> str:
    try:
        if os.path.isabs(path):
            return os.path.relpath(path, working_dir).replace("\\", "/")
        return str(path).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _existing_relpaths(working_dir: str, paths: list[str]) -> list[str]:
    rels = []
    for path in paths:
        rel = _relpath_from_workspace(working_dir, path)
        abs_path = _workspace_abs_path(working_dir, rel)
        if os.path.isfile(abs_path):
            rels.append(rel)
    return rels


def _manifest_relpaths(source_manifest: list[dict] | None) -> list[str]:
    rels = []
    for item in source_manifest or []:
        if item.get("excluded"):
            continue
        rel = item.get("relpath") or item.get("path") or ""
        if rel:
            rels.append(str(rel).replace("\\", "/"))
    return rels


def _path_extensions(paths: list[str]) -> set[str]:
    return {os.path.splitext(path)[1].lower() for path in paths if os.path.splitext(path)[1]}


def _safe_json_load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _project_has_tests(working_dir: str, relpaths: list[str]) -> bool:
    if any("/tests/" in f"/{rel.lower()}/" or rel.lower().startswith("tests/") for rel in relpaths):
        return True
    if any(os.path.basename(rel).lower().startswith("test_") and rel.lower().endswith(".py") for rel in relpaths):
        return True
    for name in ("pytest.ini", "tox.ini", "noxfile.py"):
        if os.path.exists(os.path.join(working_dir, name)):
            return True
    pyproject = os.path.join(working_dir, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            text = Path(pyproject).read_text(encoding="utf-8", errors="replace").lower()
            return "[tool.pytest" in text or "pytest" in text
        except OSError:
            return False
    return False


def _package_scripts(working_dir: str) -> dict:
    package_path = os.path.join(working_dir, "package.json")
    data = _safe_json_load(package_path)
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _workspace_has_any(working_dir: str, names: tuple[str, ...]) -> bool:
    return any(os.path.exists(os.path.join(working_dir, name)) for name in names)


def _workspace_glob_any(working_dir: str, patterns: tuple[str, ...]) -> bool:
    root = Path(working_dir)
    return any(any(root.glob(pattern)) for pattern in patterns)


def _verification_code_artifact_paths(output_files: list[str] | None,
                                      changed_files: list[str] | None) -> list[str]:
    code_exts = {
        ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java",
        ".kt", ".kts", ".go", ".rs", ".rb", ".cs", ".fs", ".vb",
        ".html", ".css", ".scss", ".sh", ".ps1", ".bat", ".cmd",
        ".json", ".toml", ".yaml", ".yml",
    }
    paths = []
    for item in list(output_files or []) + list(changed_files or []):
        rel = str(item).replace("\\", "/")
        if _is_reserved_output_relpath(rel):
            continue
        if os.path.splitext(rel)[1].lower() in code_exts:
            paths.append(rel)
    return sorted(set(paths))


def _verification_manifest_signals(working_dir: str,
                                   source_manifest: list[dict] | None = None) -> set[str]:
    rels = {rel.lower() for rel in _manifest_relpaths(source_manifest)}
    root_names = {
        "package.json": "node_package",
        "pyproject.toml": "python_project",
        "requirements.txt": "python_project",
        "pytest.ini": "python_tests",
        "tox.ini": "python_tests",
        "cargo.toml": "rust_project",
        "go.mod": "go_project",
        "pom.xml": "maven_project",
        "build.gradle": "gradle_project",
        "build.gradle.kts": "gradle_project",
        "settings.gradle": "gradle_project",
        "settings.gradle.kts": "gradle_project",
    }
    signals = set()
    for rel in rels:
        name = os.path.basename(rel)
        if name in root_names:
            signals.add(root_names[name])
        if name.endswith((".sln", ".csproj", ".fsproj", ".vbproj")):
            signals.add("dotnet_project")
    for name, signal in root_names.items():
        if os.path.exists(os.path.join(working_dir, name)):
            signals.add(signal)
    if _workspace_glob_any(working_dir, ("*.sln", "*.csproj", "*.fsproj", "*.vbproj")):
        signals.add("dotnet_project")
    if _workspace_has_any(working_dir, ("gradlew", "gradlew.bat")):
        signals.add("gradle_project")
    return signals


def _regex_signal_matches(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    matches = []
    for name, pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            matches.append(name)
    return matches


def _research_negation_scopes(text: str) -> list[tuple[int, int]]:
    scopes: list[tuple[int, int]] = []
    for match in re.finditer(r"\b(?:do\s+not|don't|dont|without|no|not|avoid|exclude|skip)\b", text, flags=re.IGNORECASE):
        start = match.start()
        max_end = min(len(text), match.end() + 100)
        terminator = re.search(r"[\n.;!?,]|\bbut\b|\bexcept\b", text[match.end():max_end], flags=re.IGNORECASE)
        end = match.end() + terminator.start() if terminator else max_end
        scopes.append((start, end))
    return scopes


def _span_inside_any_scope(span: tuple[int, int], scopes: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(scope_start <= start and end <= scope_end for scope_start, scope_end in scopes)


def _negated_research_strong_signals(text: str, strong: list[str]) -> set[str]:
    scopes = _research_negation_scopes(text)
    if not scopes:
        return set()
    patterns = dict(RESEARCH_STRONG_SIGNAL_PATTERNS)
    removed: set[str] = set()
    for name in strong:
        if name not in RESEARCH_NEGATABLE_STRONG_SIGNALS:
            continue
        pattern = patterns.get(name)
        if not pattern:
            continue
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        if matches and all(_span_inside_any_scope(match.span(), scopes) for match in matches):
            removed.add(name)
    return removed


def _is_local_github_maintenance_query(text: str) -> bool:
    if not re.search(r"(?<![.\w-])github\b|(?<![.\w-])\.github\b", text, flags=re.IGNORECASE):
        return False
    has_local_maintenance = re.search(
        r"(?<![.\w-])\.github\b|"
        r"\bgithub\s+actions?\b|"
        r"\bgithub\s+ci\b|"
        r"\bgithub\b[^\n]{0,80}\b(?:config|configuration|ya?ml|yml|files?)\b|"
        r"\bci\.ya?ml\b|"
        r"\bci\s+(?:config|configuration|ya?ml|file|files?)\b|"
        r"\bworkflows?\b|"
        r"\bworkflow\s+(?:configuration|config|syntax|file|files?|ya?ml)\b|"
        r"\bissue[-_\s]*templates?\b|"
        r"\bpull[-_\s]*request[-_\s]*templates?\b|"
        r"\bpr[-_\s]*templates?\b",
        text,
        flags=re.IGNORECASE,
    )
    if not has_local_maintenance:
        return False
    has_external_research_context = re.search(
        r"\b(?:research|external|online|web|internet|sources?|repos?|repositories|examples?|projects?|algorithms?|cite|citation|references?|evidence)\b",
        text,
        flags=re.IGNORECASE,
    )
    return not bool(has_external_research_context)


def _is_local_installed_tool_version_query(text: str) -> bool:
    if not re.search(r"\bversion\b", text, flags=re.IGNORECASE):
        return False
    has_explicit_local_version_context = re.search(
        r"\b(?:installed|local|system|runtime|environment|running)\b|\b[a-z][a-z0-9_.+-]{0,40}\s+--version\b",
        text,
        flags=re.IGNORECASE,
    )
    if (
        re.search(r"\b(?:latest|newest|most\s+recent|recent|up[- ]to[- ]date)\b", text, flags=re.IGNORECASE)
        and not has_explicit_local_version_context
    ):
        return False
    has_external_version_context = re.search(
        r"\b(?:latest|newest|current|recent|up[- ]to[- ]date)\b[^\n]{0,80}\b(?:package|api|external|online|web|internet|registry|npm|pypi|crates\.io|maven|rubygems|nuget|docs?|documentation|changelogs?|release\s+notes?)\b|"
        r"\b(?:external|online|web|internet|registry|npm\s+registry|pypi|crates\.io|maven|rubygems|nuget|official|unofficial)\b[^\n]{0,80}\bversion\b|"
        r"\bversion\b[^\n]{0,80}\b(?:external|online|web|internet|registry|api|docs?|documentation)\b",
        text,
        flags=re.IGNORECASE,
    )
    if has_external_version_context:
        return False
    tool = r"(?:[a-z][a-z0-9_.+-]{0,40})"
    has_tool_version_request = re.search(
        rf"\b(?:print|show|display|check|get|run|execute|report)\b[^\n]{{0,80}}\b(?:installed|local|system|runtime|environment)?\s*{tool}\b[^\n]{{0,50}}\bversion\b|"
        rf"\b(?:installed|local|system|runtime|environment)\b[^\n]{{0,40}}\b{tool}\b[^\n]{{0,50}}\bversion\b|"
        rf"\b(?:what|which)\s+version\s+of\s+{tool}\b[^\n]{{0,50}}\b(?:is\s+)?(?:installed|local|system|runtime|environment|running)\b|"
        rf"\b{tool}\b[^\n]{{0,40}}\b(?:installed|local|system|runtime|environment|running)\b[^\n]{{0,40}}\bversion\b|"
        rf"\b(?:print|show|display|check|get|run|execute)\b[^\n]{{0,40}}\b{tool}\b\s+--version\b|"
        rf"\b{tool}\b\s+--version\b",
        text,
        flags=re.IGNORECASE,
    )
    return bool(has_tool_version_request)


def _is_creative_news_writing_query(text: str) -> bool:
    if not re.search(r"\bnews\b|\bnews[- ]style\b", text, flags=re.IGNORECASE):
        return False
    has_explicit_creative_context = re.search(
        r"\b(?:fictional|fiction|fake|mock|sample|placeholder|dummy|imaginary|imagination|imagine|made[- ]up|invented|satirical|news[- ]style)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_authoring_verb = re.search(
        r"\b(?:create|write|generate|draft|compose|invent|make)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_local_content_artifact = re.search(
        r"\b(?:copy|ticker|headlines?|text|cards?|items?|demo|prototype|game\s+ui|ui\s+copy|ui\s+text)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_local_content_context = bool(has_authoring_verb and has_local_content_artifact)
    has_external_current_source_request = re.search(
        r"\b(?:from|using|based\s+on|based\s+upon|with)\b[^\n]{0,80}\b(?:latest|current|recent|today(?:'s)?|online|web|internet|sources?|real|actual|factual|headlines?)\b|"
        r"\b(?:latest|current|recent|today(?:'s)?|online|web|internet|real|actual|factual)\b[^\n]{0,80}\b(?:news|headlines?|sources?)\b|"
        r"\btoday(?:'s)?\s+headlines?\b|"
        r"\bnews\s+sources?\b|\bsources?\s+online\b",
        text,
        flags=re.IGNORECASE,
    )
    has_no_real_source_request = _is_creative_news_no_current_source_query(text)
    if has_external_current_source_request and not has_no_real_source_request:
        return False
    has_actual_information_request = re.search(
        r"\b(?:research|find|search|look\s+up|lookup|summari[sz]e|analy[sz]e|report|brief|explain|overview|roundup|digest|sources?|online|web|internet|external|current|latest|newest|recent|up[- ]to[- ]date|today|actual|real|factual|verify|cite|citation|evidence)\b",
        text,
        flags=re.IGNORECASE,
    )
    if has_actual_information_request and not (has_explicit_creative_context and has_local_content_context):
        return False
    return bool(has_explicit_creative_context or has_local_content_context)


def _is_creative_news_no_current_source_query(text: str) -> bool:
    if not re.search(r"\bnews\b|\bnews[- ]style\b", text, flags=re.IGNORECASE):
        return False
    has_creative_context = re.search(
        r"\b(?:fictional|fiction|fake|mock|sample|placeholder|dummy|imaginary|imagination|imagine|made[- ]up|invented|satirical|news[- ]style|copy|ticker|headlines?|text|cards?|demo|prototype|game\s+ui|ui\s+copy|ui\s+text)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_no_source_request = re.search(
        r"\b(?:do\s+not|don't|without|no|not)\b[^\n]{0,40}\b(?:real|current|online|web|internet|external|factual|actual|sources?|headlines?)\b|"
        r"\bfrom\s+imagination\b|\bmade[- ]up\s+only\b|\bfictional\s+only\b",
        text,
        flags=re.IGNORECASE,
    )
    return bool(has_creative_context and has_no_source_request)


def _is_local_current_source_task(text: str) -> bool:
    if not re.search(r"\b(?:current|latest|newest|recent|up[- ]to[- ]date|today|version|schedule|price|news)\b", text, flags=re.IGNORECASE):
        return False
    has_local_file_context = re.search(
        r"\b(?:current|latest|newest|recent|up[- ]to[- ]date|today)\b[^\n]{0,80}\bsources?\b[^\n]{0,80}\b(?:files?|folders?|directories|directory|generated|build|todo|comments?|local|repo|repository|manifest|cleanup|layout|structure|map|fixtures?|tests?|exports?|package\.json)\b|"
        r"\bsources?\b[^\n]{0,80}\b(?:files?|folders?|directories|directory|generated|build|todo|comments?|local|repo|repository|manifest|cleanup|layout|structure|map|fixtures?|tests?|exports?|package\.json)\b[^\n]{0,80}\b(?:current|latest|newest|recent|up[- ]to[- ]date|today)\b|"
        r"\b(?:current|latest|newest|recent|up[- ]to[- ]date|today|version|schedule|price|news)\b[^\n]{0,80}\b(?:generated|build|built|dist|artifact|artifacts|migration|migrations|files?|folders?|directories|directory|logs?|reports?|outputs?|local|repo|repository|fixtures?|tests?|exports?|data|fields?|formatting|products?\.csv|products?|pricing|cards?|components?|pages?|layouts?|package\.json|pyproject\.toml|cargo\.toml|go\.mod|version\s+(?:files?|numbers?)|package\s+version|changelogs?|release\s+notes?|readme|commits?|changes?|today\.md|schedule\.md|cron|crontab|config|configuration)\b|"
        r"\b(?:generated|build|built|dist|artifact|artifacts|migration|migrations|files?|folders?|directories|directory|logs?|reports?|outputs?|local|repo|repository|fixtures?|tests?|exports?|data|fields?|formatting|products?\.csv|products?|pricing|cards?|components?|pages?|layouts?|package\.json|pyproject\.toml|cargo\.toml|go\.mod|version\s+(?:files?|numbers?)|package\s+version|changelogs?|release\s+notes?|readme|commits?|changes?|today\.md|schedule\.md|cron|crontab|config|configuration)\b[^\n]{0,80}\b(?:current|latest|newest|recent|up[- ]to[- ]date|today|version|schedule|price|news)\b|"
        r"\b(?:update|open|edit|review|bump|write)\b[^\n]{0,80}\b(?:version|pyproject\.toml|cargo\.toml|go\.mod|package\.json|changelogs?|release\s+notes?|readme|commits?|changes?|today\.md|schedule\.md|cron|crontab)\b|"
        r"\b(?:version|pyproject\.toml|cargo\.toml|go\.mod|package\.json|changelogs?|release\s+notes?|readme|commits?|changes?|today\.md|schedule\.md|cron|crontab)\b[^\n]{0,80}\b(?:update|open|edit|review|bump|write|notes?|changes?)\b|"
        r"\b(?:update|open|edit|review|fix)\b[^\n]{0,80}\b(?:price|news)\b[^\n]{0,80}\b(?:fields?|formatting|lists?|products?\.csv|products?|pricing|cards?|components?|pages?|fixtures?|data|files?|layouts?|schedule)\b|"
        r"\b(?:price|news)\b[^\n]{0,80}\b(?:fields?|formatting|lists?|products?\.csv|products?|pricing|cards?|components?|pages?|fixtures?|data|files?|layouts?|schedule|local)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not has_local_file_context:
        return False
    has_factual_news_request = re.search(
        r"\b(?:summari[sz]e|analy[sz]e|report|brief|explain|overview|roundup|digest)\b[^\n]{0,80}\bnews\b|"
        r"\bnews\b[^\n]{0,80}\b(?:about|on)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_local_news_maintenance_context = re.search(
        r"\b(?:update|open|edit|review|fix)\b[^\n]{0,80}\bnews\b[^\n]{0,80}\b(?:page|component|fixture|card|layout|local|file)\b|"
        r"\bnews\b[^\n]{0,80}\b(?:page|component|fixture|card|layout|local|file)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_external_news_subject_context = re.search(
        r"\b(?:frameworks?|companies|libraries?|vendors?|tools?|platforms?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if has_factual_news_request and (not has_local_news_maintenance_context or has_external_news_subject_context):
        return False
    has_local_fixture_context = re.search(
        r"\b(?:fixtures?|test\s+fixtures?|mock(?:ed)?|sample)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_explicit_external_source_request = re.search(
        r"\b(?:from|using|with|based\s+on|against|via|fetch(?:ed)?|pull(?:ed)?|retrieve|refresh|look\s+up|lookup|search|find|check)\b[^\n]{0,80}\b(?:online|web|internet|external|sources?|market\s+data|public\s+(?:data|datasets?)|current\s+(?:market|public|external)\s+(?:data|datasets?))\b|"
        r"\b(?:from|using|with|based\s+on|based\s+upon)\b[^\n]{0,80}\b(?:today(?:'s)?|latest|current|newest|recent|most\s+recent|up[- ]to[- ]date)\s+headlines?\b|"
        r"\b(?:from|using|with|based\s+on|based\s+upon)\b[^\n]{0,80}\btoday(?:'s)?\b[^\n]{0,40}\bheadlines?\b|"
        r"\b(?:online|web|internet|external)\s+sources?\b|"
        r"\bfrom\s+(?:online|web|internet|external)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_explicit_external_current_request = re.search(
        r"\b(?:current|latest|newest|recent|up[- ]to[- ]date|today(?:'s)?)\b[^\n]{0,80}\b(?:online|web|internet|external|official|unofficial|market\s+(?:price|data)|public\s+(?:data|datasets?)|news|headlines?|schedule)\b|"
        r"\b(?:online|web|internet|external)\b[^\n]{0,80}\b(?:current|latest|newest|recent|up[- ]to[- ]date|today(?:'s)?|market\s+(?:price|data)|public\s+(?:data|datasets?)|news|headlines?|schedule)\b|"
        r"\b(?:using|from|with|based\s+on|against|via|fetch(?:ed)?|pull(?:ed)?|retrieve|look\s+up|lookup|search|find|check)\b[^\n]{0,80}\b(?:online|web|internet|external|official|unofficial|market\s+(?:price|data)|latest\s+news|official\s+schedule|current\s+market\s+price)\b|"
        r"\btoday(?:'s)?\s+headlines?\b|"
        r"\bcurrent\s+(?:market|public|external)\s+(?:data|datasets?)\b|"
        r"\b(?:find|search|look\s+up|lookup|check|get|fetch|retrieve)\b[^\n]{0,80}\b(?:latest|newest|current|recent|most\s+recent|up[- ]to[- ]date)\b[^\n]{0,80}\b(?:package\s+version|changelogs?|migration\s+guides?|release\s+notes?)\b|"
        r"\b(?:latest|newest|current|recent|most\s+recent|up[- ]to[- ]date)\b[^\n]{0,80}\b(?:changelogs?|migration\s+guides?|release\s+notes?)\b[^\n]{0,80}\bfor\b|"
        r"\b(?:update|set|bump)\b[^\n]{0,80}\b(?:version|version\s+file|version\s+number|package\.json|pyproject\.toml|cargo\.toml|go\.mod)\b[^\n]{0,80}\bto\b[^\n]{0,40}\b(?:the\s+)?latest\s+package\s+version\b|"
        r"\bcurrent\s+market\s+price\b",
        text,
        flags=re.IGNORECASE,
    )
    has_local_version_lookup_target = re.search(
        r"\b(?:find|check|review|open)\b[^\n]{0,80}\b(?:current|latest|newest|up[- ]to[- ]date)?\s*version\b[^\n]{0,80}\b(?:in|from|inside)\s+(?:package\.json|pyproject\.toml|cargo\.toml|go\.mod|version)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_explicit_local_lookup_source_context = re.search(
        r"\b(?:in|from|inside|within)\s+(?:the\s+)?(?:this\s+)?(?:local\s+)?(?:repo|repository|workspace|project|project\s+files?|docs?|documentation|docs?\s+folder|documentation\s+folder|package\.json|pyproject\.toml|cargo\.toml|go\.mod)\b|"
        r"\b(?:this\s+(?:repo|repository|workspace|project)|local\s+(?:repo|repository|workspace|project|docs?|documentation|package\.json|pyproject\.toml|cargo\.toml|go\.mod)|docs?\s+folder|documentation\s+folder)\b|"
        r"\bfor\s+(?:this\s+)?local\s+project\b",
        text,
        flags=re.IGNORECASE,
    )
    has_external_override_for_local_lookup = has_explicit_external_source_request or re.search(
        r"\b(?:online|web|internet|external|research|registry|npm|pypi|crates\.io|maven|rubygems|nuget|market\s+(?:price|data)|public\s+(?:data|datasets?))\b",
        text,
        flags=re.IGNORECASE,
    )
    if has_explicit_local_lookup_source_context and not has_external_override_for_local_lookup:
        return True
    if (
        has_explicit_external_current_request
        and not has_local_version_lookup_target
        and (not has_local_fixture_context or has_explicit_external_source_request)
    ):
        return False
    has_explicit_local_context = re.search(
        r"\b(?:local|locally|repo|repository|workspace|project\s+files?|changed\s+files?|fixtures?|tests?)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_local_source_word_context = re.search(
        r"\bsources?\b[^\n]{0,80}\b(?:files?|folders?|directories|directory|generated|build|todo|comments?|local|repo|repository|manifest|cleanup|layout|structure|map|branch|material|notes?)\b|"
        r"\b(?:files?|folders?|directories|directory|generated|build|todo|comments?|local|repo|repository|manifest|cleanup|layout|structure|map|branch|material|notes?)\b[^\n]{0,80}\bsources?\b",
        text,
        flags=re.IGNORECASE,
    )
    has_research_source_context = re.search(
        r"\b(?:research|evidence|citation|cite|internet)\b",
        text,
        flags=re.IGNORECASE,
    )
    has_external_source_word_context = re.search(
        r"\b(?:sources?|references?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if has_research_source_context or (has_external_source_word_context and not has_local_source_word_context):
        return False
    has_external_current_context = re.search(
        r"\b(?:external|online|web|official|unofficial|market)\b|"
        r"\bpublic\s+(?:sources?|websites?|sites?|apis?|market\s+data|records?|datasets?)\b",
        text,
        flags=re.IGNORECASE,
    )
    return not (bool(has_external_current_context) and not bool(has_explicit_local_context))


def _local_current_path_context(paths: list[str] | None) -> str:
    hints: list[str] = []
    for path in paths or []:
        rel = str(path).replace("\\", "/").lower()
        base = os.path.basename(rel)
        if base in {"version", "package.json", "pyproject.toml", "cargo.toml", "go.mod"} or "version" in base:
            hints.append("local version file package version")
        if "changelog" in rel:
            hints.append("local changelog recent changes version")
        if "release" in rel and "note" in rel:
            hints.append("local release notes version")
        if base == "readme.md" or base == "readme":
            hints.append("local readme version notes")
        if "today" in base:
            hints.append("local today file report")
        if "schedule" in rel:
            hints.append("local schedule file configuration")
        if base in {"cron", "crontab"} or "crontab" in rel:
            hints.append("local cron schedule configuration")
        if "git.log" in rel:
            hints.append("local recent commits log")
        if "price" in rel or "pricing" in rel or base in {"products.csv", "product.csv"} or "product" in base:
            hints.append("local product price field pricing list file formatting card component")
        if "news" in rel:
            hints.append("local news page component card fixture file")
    return " ".join(hints)


def _research_signal_matches(query: str, output_files: list[str] | None, local_paths: list[str] | None = None) -> tuple[bool, list[str]]:
    query_text = query or ""
    strong = _regex_signal_matches(query_text, RESEARCH_STRONG_SIGNAL_PATTERNS)
    weak = _regex_signal_matches(query_text, RESEARCH_WEAK_SIGNAL_PATTERNS)
    ignored_strong: list[str] = []
    if "github_source" in strong and _is_local_github_maintenance_query(query_text):
        strong = [name for name in strong if name != "github_source"]
        ignored_strong.append("github_source:local_github_maintenance")
    if "current_info" in strong and _is_local_installed_tool_version_query(query_text):
        strong = [name for name in strong if name != "current_info"]
        ignored_strong.append("current_info:local_installed_tool_version")
    if _is_creative_news_no_current_source_query(query_text):
        removed = {
            name for name in strong
            if name in {"current_info", "source_evidence", "online_sources", "external_evidence"}
        }
        if removed:
            strong = [name for name in strong if name not in removed]
            ignored_strong.extend(f"{name}:creative_news_no_current_source" for name in sorted(removed))
    negated = _negated_research_strong_signals(query_text, strong)
    if negated:
        strong = [name for name in strong if name not in negated]
        ignored_strong.extend(f"{name}:negated_research_request" for name in sorted(negated))
    if "current_info" in strong and _is_creative_news_writing_query(query_text):
        strong = [name for name in strong if name != "current_info"]
        ignored_strong.append("current_info:creative_news_writing")
    local_query_text = f"{query_text} {_local_current_path_context(local_paths)}"
    if _is_local_current_source_task(local_query_text):
        removed = {name for name in strong if name in {"current_info", "source_evidence"}}
        if removed:
            strong = [name for name in strong if name not in removed]
            ignored_strong.extend(f"{name}:local_current_source_task" for name in sorted(removed))
    for output in output_files or []:
        base = os.path.basename(str(output).replace("\\", "/")).lower()
        if base in RESEARCH_WEAK_OUTPUT_NAMES:
            weak.append(f"output_name:{base}")

    signals: list[str] = []
    signals.extend(f"research_strong_signal_ignored:{name}" for name in ignored_strong)
    signals.extend(f"research_signal:{name}" for name in strong)
    selected = bool(strong)
    if weak and strong:
        signals.extend(f"research_signal:weak_{name}_with_co_signal" for name in sorted(set(weak)))
    elif weak:
        signals.extend(f"research_weak_signal_ignored:{name}" for name in sorted(set(weak)))
    return selected, sorted(set(signals))


def _classify_congress_task(
    user_query: str,
    output_files: list[str] | None,
    source_manifest: list[dict] | None = None,
    working_dir: str | None = None,
    preflight_requirements: list[dict] | None = None,
    preflight_state: dict | None = None,
    changed_files: list[str] | None = None,
    user_updates: list[dict] | None = None,
) -> TaskClassification:
    """Classify the task from Congress-owned facts only."""
    working_dir = working_dir or os.getcwd()
    query = user_query or ""
    query_l = query.lower()
    outputs = [str(item).replace("\\", "/") for item in (output_files or [])]
    manifest_rels = _manifest_relpaths(source_manifest)
    changed = [str(item).replace("\\", "/") for item in (changed_files or [])]
    all_paths = outputs + manifest_rels + changed
    exts = _path_extensions(all_paths)
    output_exts = _path_extensions(outputs)
    classes: set[str] = set()
    signals: list[str] = []

    for ext in sorted(output_exts):
        signals.append(f"output_extension:{ext}")
    for rel in manifest_rels[:20]:
        base = os.path.basename(rel)
        if base in ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod", "Cargo.toml"):
            signals.append(f"source_manifest:{base}")
    for rel in changed[:20]:
        signals.append(f"changed_file:{rel}")

    doc_exts = {".md", ".txt", ".rst", ".adoc"}
    code_exts = {
        ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java",
        ".kt", ".kts", ".go", ".rs", ".rb", ".cs", ".fs", ".vb",
        ".html", ".css", ".scss", ".sh", ".ps1", ".bat", ".cmd",
        ".json", ".toml", ".yaml", ".yml", ".sln", ".csproj",
        ".fsproj", ".vbproj", ".gradle",
    }
    ui_exts = {".html", ".css", ".scss", ".jsx", ".tsx"}
    if outputs and output_exts and output_exts <= doc_exts:
        classes.add(TASK_CLASS_DOCUMENTATION_ONLY)
        signals.append("requested_outputs_are_documentation")
    manifest_signals = _verification_manifest_signals(working_dir, source_manifest)
    if manifest_signals:
        signals.append("project_manifest_signal:" + ",".join(sorted(manifest_signals)))
    if exts & code_exts or manifest_signals or re.search(r"\b(code|script|implement|fix|bug|refactor|module|function|class|cli|build)\b", query_l):
        classes.add(TASK_CLASS_CODE_STATIC)
        signals.append("code_or_config_signal")
    if _project_has_tests(working_dir, manifest_rels + changed) or re.search(r"\b(test|pytest|unit test|integration test)\b", query_l):
        classes.add(TASK_CLASS_CODE_TESTABLE)
        signals.append("test_signal")
    package_scripts = _package_scripts(working_dir)
    if package_scripts:
        signals.append("package_json_scripts:" + ",".join(sorted(package_scripts)[:8]))
        if any(name in package_scripts for name in ("test", "lint", "build", "typecheck")):
            classes.add(TASK_CLASS_CODE_TESTABLE)
    if (
        exts & ui_exts
        or any(os.path.basename(rel).lower() in ("vite.config.js", "vite.config.ts", "next.config.js") for rel in all_paths)
        or re.search(r"\b(ui|browser|frontend|front-end|react|vue|svelte|canvas|screenshot|visual|dom|web app|playwright)\b", query_l)
    ):
        classes.add(TASK_CLASS_UI_RUNTIME)
        signals.append("ui_runtime_signal")
    req_types = {req.get("type") for req in (preflight_requirements or [])}
    if (
        req_types & {"local_url", "env_var", "service", "mcp_connector", "app_connector", "package_registry"}
        or re.search(r"\b(api|database|credential|oauth|token|endpoint|service|connector|server|redis|postgres|mysql|mcp)\b", query_l)
    ):
        classes.add(TASK_CLASS_SERVICE_INTEGRATION)
        signals.append("service_or_dependency_signal")
    research_selected, research_signals = _research_signal_matches(query, outputs, changed + outputs)
    signals.extend(research_signals)
    if research_selected:
        classes.add(TASK_CLASS_RESEARCH_CURRENT_INFO)
    if re.search(r"\b(security|secret|credential|password|token|auth|sandbox|subprocess|orchestrator|codex|agent|congress|lock|redact|approval gate|verification)\b", query_l):
        classes.add(TASK_CLASS_SECURITY_SENSITIVE)
        signals.append("security_sensitive_signal")
    if any("congress2.py" in rel.lower() or "congress.py" in rel.lower() for rel in outputs + changed):
        classes.add(TASK_CLASS_SECURITY_SENSITIVE)
        signals.append("security_sensitive_file:congress")
    if preflight_state and preflight_state.get("blocked"):
        classes.add(TASK_CLASS_SERVICE_INTEGRATION)
        signals.append("preflight_blocked_signal")
    if user_updates:
        signals.append(f"user_updates:{len(user_updates)}")

    if not classes:
        classes.add(TASK_CLASS_DOCUMENTATION_ONLY if output_exts <= doc_exts else TASK_CLASS_CODE_STATIC)
        signals.append("fallback_classification")

    base_classes = sorted(c for c in classes if c != TASK_CLASS_MIXED)
    if len(base_classes) > 1:
        base_classes.append(TASK_CLASS_MIXED)
    confidence = "high" if len(signals) >= 3 else "medium" if signals else "low"

    evidence_map = {
        TASK_CLASS_DOCUMENTATION_ONLY: ["requested_output_exists", "requested_structure_or_headings", "inspector_review"],
        TASK_CLASS_CODE_STATIC: ["requested_output_exists", "syntax_or_static_check", "output_hashes"],
        TASK_CLASS_CODE_TESTABLE: ["baseline_and_post_change_tests", "test_command_results"],
        TASK_CLASS_UI_RUNTIME: ["browser_or_runtime_evidence"],
        TASK_CLASS_SERVICE_INTEGRATION: ["dependency_preflight_evidence"],
        TASK_CLASS_RESEARCH_CURRENT_INFO: RESEARCH_REQUIRED_EVIDENCE,
        TASK_CLASS_SECURITY_SENSITIVE: ["evidence_based_review", "redaction_checks", "managed_file_safety"],
    }
    required: list[str] = []
    for cls in base_classes:
        for item in evidence_map.get(cls, []):
            if item not in required:
                required.append(item)
    skipped = []
    for cls, evidence in evidence_map.items():
        if cls not in base_classes:
            skipped.append(f"{cls}: not selected; skipped {', '.join(evidence)}")

    rationale = (
        "Classified deterministically from the original request, requested output paths, "
        "source manifest, preflight requirements/results, user updates, and changed files. "
        "Researcher prose is not used as proof."
    )
    return TaskClassification(
        classes=base_classes,
        confidence=confidence,
        signals=sorted(set(signals)),
        required_evidence=required,
        skipped_evidence=skipped,
        rationale=rationale,
    )


def _discover_verification_command_checks(
    working_dir: str,
    classification: TaskClassification | dict,
    phase: str,
    output_files: list[str] | None = None,
    source_manifest: list[dict] | None = None,
    changed_files: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> list[VerificationCheck]:
    classification_data = _task_classification_to_dict(classification)
    classes = list(classification_data.get("classes") or [])
    output_files = [str(item).replace("\\", "/") for item in (output_files or [])]
    changed_files = [str(item).replace("\\", "/") for item in (changed_files or [])]
    manifest_rels = _manifest_relpaths(source_manifest)
    candidate_paths: list[str] = []
    if phase == VERIFICATION_PHASE_BASELINE:
        candidate_paths.extend(_existing_relpaths(working_dir, output_files))
        candidate_paths.extend(rel for rel in manifest_rels if rel.lower().endswith(".py"))
    else:
        candidate_paths.extend(_existing_relpaths(working_dir, output_files + changed_files))
    seen_paths = set()
    python_paths = []
    for rel in candidate_paths:
        norm = rel.replace("\\", "/")
        if norm.lower().endswith(".py") and norm not in seen_paths:
            seen_paths.add(norm)
            python_paths.append(norm)
    python_paths = python_paths[:20]
    js_paths = []
    ts_paths = []
    for rel in candidate_paths:
        norm = rel.replace("\\", "/")
        ext = os.path.splitext(norm)[1].lower()
        if ext in (".js", ".mjs", ".cjs") and norm not in js_paths:
            js_paths.append(norm)
        if ext in (".ts", ".tsx") and norm not in ts_paths:
            ts_paths.append(norm)
    js_paths = js_paths[:20]
    ts_paths = ts_paths[:20]
    manifest_signals = _verification_manifest_signals(working_dir, source_manifest)

    checks: list[VerificationCheck] = []
    required_code = phase != VERIFICATION_PHASE_BASELINE and (
        TASK_CLASS_CODE_STATIC in classes or TASK_CLASS_CODE_TESTABLE in classes
    )
    if python_paths:
        command = [sys.executable, "-m", "py_compile", *python_paths]
        checks.append(VerificationCheck(
            id=f"{phase}:python_syntax",
            title="Python syntax compilation",
            phase=phase,
            check_type="command",
            required=required_code,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            target_paths=python_paths,
            reason="Selected because Python files are part of the source, changed files, or requested outputs.",
        ))
    elif TASK_CLASS_CODE_STATIC in classes or TASK_CLASS_CODE_TESTABLE in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:python_syntax_not_applicable",
            title="Python syntax compilation",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_NOT_APPLICABLE,
            reason="No Python files were available for syntax compilation in this phase.",
        ))

    for rel in js_paths:
        command = ["node", "--check", rel]
        check_id_path = re.sub(r"[^A-Za-z0-9_.-]+", "_", rel)
        checks.append(VerificationCheck(
            id=f"{phase}:node_check:{check_id_path}",
            title=f"Node JavaScript syntax check: {rel}",
            phase=phase,
            check_type="command",
            required=required_code,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            target_paths=[rel],
            reason="Selected because a JavaScript requested or changed output can be parsed by `node --check`.",
        ))

    if ts_paths or _workspace_has_any(working_dir, ("tsconfig.json",)):
        command = ["tsc", "--noEmit"]
        checks.append(VerificationCheck(
            id=f"{phase}:tsc_no_emit",
            title="TypeScript no-emit check",
            phase=phase,
            check_type="command",
            required=required_code,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            target_paths=ts_paths,
            reason="Selected because TypeScript files or tsconfig.json were discovered.",
        ))

    has_tests = _project_has_tests(working_dir, manifest_rels + changed_files)
    if TASK_CLASS_CODE_TESTABLE in classes and has_tests:
        command = [sys.executable, "-m", "pytest"]
        checks.append(VerificationCheck(
            id=f"{phase}:pytest",
            title="Python test suite",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because Congress found tests or pytest configuration.",
        ))
    elif TASK_CLASS_CODE_TESTABLE in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:pytest_not_applicable",
            title="Python test suite",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_NOT_APPLICABLE,
            reason="No tests or pytest configuration were discovered.",
        ))

    scripts = _package_scripts(working_dir)
    for script_name in ("test", "lint", "typecheck", "build"):
        if script_name not in scripts:
            continue
        command = ["npm", "run", script_name]
        checks.append(VerificationCheck(
            id=f"{phase}:npm_{script_name}",
            title=f"npm script: {script_name}",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE and script_name in ("test", "build"),
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason=f"Selected because package.json defines a `{script_name}` script.",
        ))
    if TASK_CLASS_UI_RUNTIME in classes and not scripts:
        checks.append(VerificationCheck(
            id=f"{phase}:ui_script_not_applicable",
            title="UI runtime script discovery",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_NOT_APPLICABLE,
            reason="No package.json scripts were discovered for a browser/runtime command.",
        ))

    if "rust_project" in manifest_signals:
        command = ["cargo", "check", "--quiet"]
        checks.append(VerificationCheck(
            id=f"{phase}:cargo_check",
            title="Rust cargo check",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because Cargo.toml was discovered.",
        ))
    if "go_project" in manifest_signals:
        command = ["go", "test", "./..."]
        checks.append(VerificationCheck(
            id=f"{phase}:go_test",
            title="Go test suite",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because go.mod was discovered.",
        ))
    if "maven_project" in manifest_signals:
        command = ["mvn", "test", "-q"]
        checks.append(VerificationCheck(
            id=f"{phase}:maven_test",
            title="Maven test",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because pom.xml was discovered.",
        ))
    if "gradle_project" in manifest_signals:
        wrapper = None
        for candidate in ("gradlew.bat", "gradlew"):
            path = os.path.join(working_dir, candidate)
            if os.path.exists(path):
                wrapper = path
                break
        command = [wrapper or "gradle", "test"]
        checks.append(VerificationCheck(
            id=f"{phase}:gradle_test",
            title="Gradle test",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because Gradle project files were discovered.",
        ))
    if "dotnet_project" in manifest_signals:
        command = ["dotnet", "build"]
        checks.append(VerificationCheck(
            id=f"{phase}:dotnet_build",
            title=".NET build",
            phase=phase,
            check_type="command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            timeout_seconds=timeout_seconds,
            working_dir=working_dir,
            task_classes=classes,
            command=command,
            command_display=_verification_command_display(command),
            reason="Selected because a .NET solution or project file was discovered.",
        ))
    return checks


def _verification_has_command(checks: list[VerificationCheck], fragments: tuple[str, ...]) -> bool:
    for check in checks:
        if check.check_type != "command":
            continue
        check_id = check.id.lower()
        if any(fragment in check_id for fragment in fragments):
            return True
    return False


def _append_required_missing_verification_gates(
    checks: list[VerificationCheck],
    *,
    working_dir: str,
    classes: list[str],
    phase: str,
    verification_mode: str,
    output_files: list[str] | None,
    source_manifest: list[dict] | None,
    changed_files: list[str] | None,
) -> None:
    if verification_mode != "required" or phase == VERIFICATION_PHASE_BASELINE:
        return
    code_artifacts = _verification_code_artifact_paths(output_files, changed_files)
    manifest_signals = _verification_manifest_signals(working_dir, source_manifest)
    has_project_manifest = bool(manifest_signals)
    documentation_only_target = TASK_CLASS_DOCUMENTATION_ONLY in classes and not code_artifacts
    has_code_target = bool(code_artifacts or (has_project_manifest and not documentation_only_target))
    has_static_command = _verification_has_command(checks, (
        "python_syntax", "node_check", "tsc_no_emit", "cargo_check",
        "go_test", "maven_test", "gradle_test", "dotnet_build", "npm_build",
        "npm_lint", "npm_typecheck",
    ))
    has_test_command = _verification_has_command(checks, (
        "pytest", "npm_test", "go_test", "maven_test", "gradle_test",
    ))

    if TASK_CLASS_CODE_STATIC in classes and has_code_target and not has_static_command:
        checks.append(VerificationCheck(
            id=f"{phase}:missing_code_static_verification",
            title="Missing required code/static verification",
            phase=phase,
            check_type="non_command",
            required=True,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_FAILED,
            reason=(
                "--verification=required requires a relevant syntax, static, build, "
                "or package-script check for code artifacts. Congress found code/project "
                "signals but no supported runnable check. Use --verification=off only for "
                "an explicit waiver."
            ),
            target_paths=code_artifacts,
        ))

    if TASK_CLASS_CODE_TESTABLE in classes and has_code_target and not has_test_command:
        checks.append(VerificationCheck(
            id=f"{phase}:missing_code_test_verification",
            title="Missing required test verification",
            phase=phase,
            check_type="non_command",
            required=True,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_FAILED,
            reason=(
                "--verification=required classified this as testable code, but no real "
                "test command was discovered. Approval requires test evidence, a blocked "
                "dependency, or an explicit verification waiver."
            ),
            target_paths=code_artifacts,
        ))

    if TASK_CLASS_SERVICE_INTEGRATION in classes:
        has_preflight_evidence = any(
            check.id.endswith(":preflight_requirements") for check in checks
        )
        has_service_command = _verification_has_command(checks, (
            "pytest", "npm_test", "go_test", "maven_test", "gradle_test", "dotnet_build",
        ))
        if not has_preflight_evidence and not has_service_command:
            checks.append(VerificationCheck(
                id=f"{phase}:missing_service_integration_verification",
                title="Missing required service/dependency verification",
                phase=phase,
                check_type="non_command",
                required=True,
                working_dir=working_dir,
                task_classes=classes,
                status=VERIFICATION_STATUS_FAILED,
                reason=(
                    "--verification=required classified this as service integration, "
                    "but Congress found no deterministic preflight evidence or relevant "
                    "real command to verify the dependency."
                ),
            ))


def _research_evidence_candidate_paths(working_dir: str, output_files: list[str] | None) -> list[str]:
    candidates = list(output_files or []) + [RESEARCHER_UPDATED_FILE]
    rels = []
    seen = set()
    for rel in _existing_relpaths(working_dir, candidates):
        key = _relpath_key(rel)
        if key not in seen:
            seen.add(key)
            rels.append(rel)
    return rels


def _line_has_concrete_source_identifier(line: str) -> bool:
    text = line.strip()
    lower = text.lower()
    if re.search(r"https?://[^\s)>\]]+", text, flags=re.IGNORECASE):
        return True
    if re.search(
        r"(?<!@)\b(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|ai|dev|info|co|uk|cn|ru|de|fr|jp|us|markets)\b",
        lower,
    ):
        return True
    if re.search(r"\bdoi\s*:?\s*10\.\d{4,9}/\S+", lower):
        return True
    if re.search(r"\barxiv\s*:?\s*\d{4}\.\d{4,5}(?:v\d+)?\b", lower):
        return True
    if re.search(r"\bssrn\s*:?\s*(?:paper\s*)?(?:id\s*)?\d{5,}\b", lower):
        return True
    if ("github" in lower or "repository" in lower or "repo" in lower) and re.search(
        r"\b[a-z0-9_.-]+/[a-z0-9_.-]+\b",
        lower,
    ):
        return True
    if re.search(r"\b(?:official document|named official document)\s*[:=-]\s*\S.{8,}", text, flags=re.IGNORECASE):
        return True
    if re.search(r"\b[A-Z][A-Za-z0-9 .-]+\s+(?:standard|specification|manual|guide|documentation)\s+(?:v?\d|\(\d{4}\))", text):
        return True
    return False


def _source_candidate_lines(text: str) -> list[str]:
    lines = []
    in_sources = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        if not line:
            if in_sources:
                in_sources = False
            continue
        lower = line.lower()
        if re.match(r"^#{1,6}\s+.+", line):
            in_sources = bool(re.search(r"\b(?:sources?|references?|bibliography|research evidence)\b", lower))
            continue
        if re.match(
            r"^[-*]?\s*(?:confidence|primary/secondary|checked online|not checked|not-checked|stale(?:/unverified)?(?: source)? warnings?|source warnings?)\s*:",
            lower,
        ):
            in_sources = False
            continue
        if re.search(r"\b(?:sources?|references?|bibliography)\s*[:=-]", lower):
            in_sources = True
            lines.append(line)
            continue
        if in_sources or re.search(r"https?://|\bdoi\b|\barxiv\b|\bssrn\b|\bgithub\b|\brepository\b|\bofficial document\b", lower):
            lines.append(line)
    return lines


def _research_evidence_from_text(text: str) -> dict:
    text = text or ""
    lower = text.lower()
    source_lines = _source_candidate_lines(text)
    concrete_sources = [
        line for line in source_lines
        if _line_has_concrete_source_identifier(line)
    ]
    vague_source_lines = [
        line for line in source_lines
        if not _line_has_concrete_source_identifier(line)
        and re.search(r"\b(?:official docs?|github|forums?|websites?|sources?)\b", line, flags=re.IGNORECASE)
    ]

    found = []
    missing = []
    field_checks = [
        (
            "retrieval_timestamp",
            re.search(
                r"\b(?:retrieval timestamp|retrieved(?: at| on)?|last checked|accessed)\b[^\n]*(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})",
                text,
                flags=re.IGNORECASE,
            ),
        ),
        ("concrete_source_list", bool(concrete_sources)),
        (
            "source_confidence",
            re.search(
                r"(?:\b(?:confidence|trust|reliability)\b[^\n]*(?:high|medium|low|\d+\s*%)|\b(?:high|medium|low)[-\s]+(?:confidence|trust|reliability)\b|\|\s*[^|\n]*confidence[^|\n]*\|[\s\S]{0,400}\|\s*(?:high|medium|low)(?:[-\s]+(?:confidence|trust|reliability))?\s*\|)",
                text,
                flags=re.IGNORECASE,
            ),
        ),
        ("primary_secondary_distinction", "primary" in lower and "secondary" in lower),
        (
            "checked_online_scope",
            re.search(
                r"\b(?:checked online|online scope|checked sources?|what was checked online|checked[^\n]*(?:website|websites|docs|documentation|source|sources|github|forum|domain))\b",
                text,
                flags=re.IGNORECASE,
            ),
        ),
        (
            "not_checked_reasons",
            re.search(r"\b(?:not checked|not-checked|unchecked|blocked by|could not check|not used as evidence)\b", text, flags=re.IGNORECASE),
        ),
        (
            "stale_unverified_warnings",
            re.search(r"\b(?:stale|unverified|staleness|source warnings?|warnings?)\b", text, flags=re.IGNORECASE),
        ),
    ]
    for name, present in field_checks:
        if present:
            found.append(name)
        else:
            missing.append(name)
    if vague_source_lines and "concrete_source_list" not in missing and not concrete_sources:
        missing.append("concrete_source_list")
    return {
        "found": found,
        "missing": missing,
        "concrete_sources": concrete_sources[:10],
        "vague_source_lines": vague_source_lines[:10],
        "source_lines": source_lines[:20],
    }


def _detect_research_evidence(working_dir: str, output_files: list[str] | None) -> dict:
    relpaths = _research_evidence_candidate_paths(working_dir, output_files)
    combined_parts = []
    read_errors = []
    for rel in relpaths:
        try:
            text = Path(_workspace_abs_path(working_dir, rel)).read_text(encoding="utf-8", errors="replace")
            combined_parts.append(text)
        except OSError as exc:
            read_errors.append(f"{rel}: {type(exc).__name__}: {exc}")
    evidence = _research_evidence_from_text("\n".join(combined_parts))
    evidence["paths"] = relpaths
    evidence["read_errors"] = read_errors
    return evidence


def _latest_current_info_evidence_check(checks: list[VerificationCheck | dict], phase: str | None = None) -> dict | None:
    matches = []
    for check in checks or []:
        item = _verification_check_to_dict(check)
        if item.get("id", "").endswith(":current_info_evidence") and (phase is None or item.get("phase") == phase):
            matches.append(item)
    return matches[-1] if matches else None


def _research_evidence_final_gate_blocker(
    classification: TaskClassification | dict | None,
    checks: list[VerificationCheck | dict],
    verification_mode: str,
) -> str | None:
    if verification_mode == "off":
        return None
    classes = _task_classification_to_dict(classification).get("classes") or []
    if TASK_CLASS_RESEARCH_CURRENT_INFO not in classes:
        return None
    check = _latest_current_info_evidence_check(checks, VERIFICATION_PHASE_FINAL)
    if not check:
        return "Research/current-info evidence is required but no final current_info_evidence check was recorded."
    status = check.get("status")
    if status not in (VERIFICATION_STATUS_PASSED, VERIFICATION_STATUS_WAIVED):
        reason = check.get("reason") or "No reason recorded."
        return f"Research/current-info evidence is required but final current_info_evidence is `{status}`: {reason}"
    return None


def _select_verification_checks(
    working_dir: str,
    classification: TaskClassification | dict,
    phase: str,
    output_files: list[str] | None = None,
    source_manifest: list[dict] | None = None,
    preflight_state: dict | None = None,
    capability_inventory: dict | None = None,
    verification_mode: str = DEFAULT_VERIFICATION_MODE,
    max_timeout_seconds: int = DEFAULT_MAX_VERIFICATION_TIMEOUT,
    changed_files: list[str] | None = None,
) -> list[VerificationCheck]:
    classification_data = _task_classification_to_dict(classification)
    classes = list(classification_data.get("classes") or [])
    if verification_mode == "off":
        return [VerificationCheck(
            id=f"{phase}:verification_off",
            title="Verification mode off",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_WAIVED,
            waiver_reason="User selected --verification=off; Congress records this explicit waiver.",
            reason="Verification is disabled by CLI mode.",
        )]

    output_files = output_files or []
    source_manifest = source_manifest or []
    timeout_per_check = max(
        MIN_MAX_VERIFICATION_TIMEOUT,
        int(max_timeout_seconds or DEFAULT_MAX_VERIFICATION_TIMEOUT),
    )
    checks: list[VerificationCheck] = [
        VerificationCheck(
            id=f"{phase}:requested_outputs",
            title="Requested output existence and hashes",
            phase=phase,
            check_type="non_command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            working_dir=working_dir,
            task_classes=classes,
            timeout_seconds=None,
            target_paths=list(output_files),
            reason=(
                "Baseline records output state before Researcher."
                if phase == VERIFICATION_PHASE_BASELINE
                else "Requested outputs must exist as regular files for approval."
            ),
        ),
        VerificationCheck(
            id=f"{phase}:source_manifest",
            title="Source manifest availability",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_PENDING,
            reason="Records whether Congress discovered source/context files for agents.",
        ),
    ]

    if preflight_state and (preflight_state.get("requirements") or preflight_state.get("results")):
        checks.append(VerificationCheck(
            id=f"{phase}:preflight_requirements",
            title="Preflight dependency evidence",
            phase=phase,
            check_type="non_command",
            required=TASK_CLASS_SERVICE_INTEGRATION in classes or TASK_CLASS_UI_RUNTIME in classes,
            working_dir=working_dir,
            task_classes=classes,
            reason="Selected because Congress recorded preflight requirements or probe results.",
        ))
    elif TASK_CLASS_SERVICE_INTEGRATION in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:preflight_not_applicable",
            title="Preflight dependency evidence",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_NOT_APPLICABLE,
            reason="No deterministic preflight dependency requirement was discovered.",
        ))

    if TASK_CLASS_UI_RUNTIME in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:browser_preflight",
            title="Browser/runtime tooling availability",
            phase=phase,
            check_type="non_command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            working_dir=working_dir,
            task_classes=classes,
            reason="Selected because task classification includes UI/runtime behavior.",
        ))

    checks.extend(_discover_verification_command_checks(
        working_dir,
        classification,
        phase,
        output_files=output_files,
        source_manifest=source_manifest,
        changed_files=changed_files,
        timeout_seconds=timeout_per_check,
    ))

    _append_required_missing_verification_gates(
        checks,
        working_dir=working_dir,
        classes=classes,
        phase=phase,
        verification_mode=verification_mode,
        output_files=output_files,
        source_manifest=source_manifest,
        changed_files=changed_files,
    )

    if TASK_CLASS_RESEARCH_CURRENT_INFO in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:current_info_evidence",
            title="Current/research evidence",
            phase=phase,
            check_type="non_command",
            required=phase != VERIFICATION_PHASE_BASELINE,
            working_dir=working_dir,
            task_classes=classes,
            status=VERIFICATION_STATUS_PENDING,
            reason=(
                "Selected because task classification includes research/current-info. "
                + (
                    "Baseline records the requirement before Researcher creates evidence."
                    if phase == VERIFICATION_PHASE_BASELINE
                    else RESEARCH_EVIDENCE_REQUIREMENT_TEXT
                )
            ),
        ))
    if TASK_CLASS_SECURITY_SENSITIVE in classes:
        checks.append(VerificationCheck(
            id=f"{phase}:redaction_sanity",
            title="Verification redaction sanity",
            phase=phase,
            check_type="non_command",
            required=False,
            working_dir=working_dir,
            task_classes=classes,
            reason="Selected because security-sensitive tasks require verification metadata to redact secret-like values.",
        ))
    return checks


def _run_non_command_verification_check(
    check: VerificationCheck,
    *,
    working_dir: str,
    output_files: list[str] | None = None,
    source_manifest: list[dict] | None = None,
    preflight_state: dict | None = None,
    capability_inventory: dict | None = None,
) -> VerificationCheck:
    if check.status in {
        VERIFICATION_STATUS_WAIVED,
        VERIFICATION_STATUS_NOT_APPLICABLE,
        VERIFICATION_STATUS_BLOCKED,
        VERIFICATION_STATUS_FAILED,
    }:
        check.duration_seconds = check.duration_seconds if check.duration_seconds is not None else 0.0
        check.timestamp = _utc_timestamp()
        return check
    start = time.monotonic()
    output_files = output_files or []
    source_manifest = source_manifest or []
    capability_inventory = capability_inventory or {}
    preflight_state = preflight_state or {}
    if check.id.endswith(":requested_outputs"):
        snapshots = _collect_result_output_snapshots(working_dir, output_files)
        check.output_hashes = snapshots
        invalid = [
            item.get("path") for item in snapshots
            if item.get("invalid_type") or not item.get("exists")
        ]
        if check.phase == VERIFICATION_PHASE_BASELINE and invalid:
            check.status = VERIFICATION_STATUS_NOT_APPLICABLE
            check.reason += " Requested outputs are not required to exist before Researcher."
        elif invalid:
            check.status = VERIFICATION_STATUS_FAILED
            check.reason += " Missing or invalid requested outputs: " + ", ".join(str(item) for item in invalid)
        else:
            check.status = VERIFICATION_STATUS_PASSED
            check.reason += " All requested outputs exist as regular files."
    elif check.id.endswith(":source_manifest"):
        if source_manifest:
            check.status = VERIFICATION_STATUS_PASSED
            check.reason += f" Source manifest has {len(source_manifest)} file(s)."
        else:
            check.status = VERIFICATION_STATUS_NOT_APPLICABLE
            check.reason += " No source files were discovered."
    elif check.id.endswith(":preflight_requirements"):
        blocked = preflight_state.get("blocked")
        if blocked:
            check.status = VERIFICATION_STATUS_BLOCKED
            check.blocked_reason = blocked.get("reason") or "Preflight dependency is blocked."
        else:
            results = preflight_state.get("results") or []
            failed = [item for item in results if not item.get("ok")]
            if failed:
                check.status = VERIFICATION_STATUS_FAILED
                check.reason += " One or more optional preflight checks failed."
            elif results:
                check.status = VERIFICATION_STATUS_PASSED
                check.reason += " Preflight checks passed."
            else:
                check.status = VERIFICATION_STATUS_NOT_APPLICABLE
                check.reason += " No preflight results were recorded."
    elif check.id.endswith(":browser_preflight"):
        tools = (capability_inventory.get("tools") or {})
        browser_names = _tool_names_for_browser()
        found = [name for name in browser_names if tools.get(name) or shutil.which(name)]
        if found:
            check.status = VERIFICATION_STATUS_PASSED
            check.reason += " Browser tooling present: " + ", ".join(found[:5])
        else:
            check.status = VERIFICATION_STATUS_BLOCKED if check.required else VERIFICATION_STATUS_NOT_APPLICABLE
            check.blocked_reason = "Browser/runtime tooling was not found."
    elif check.id.endswith(":current_info_evidence"):
        evidence = _detect_research_evidence(working_dir, output_files)
        check.target_paths = evidence.get("paths") or []
        found = evidence.get("found") or []
        missing = evidence.get("missing") or []
        concrete_sources = evidence.get("concrete_sources") or []
        vague_source_lines = evidence.get("vague_source_lines") or []
        if check.phase == VERIFICATION_PHASE_BASELINE and missing:
            check.status = VERIFICATION_STATUS_NOT_APPLICABLE
            check.reason += " Research evidence is not required before Researcher creates or updates requested outputs."
        elif missing:
            check.status = VERIFICATION_STATUS_FAILED
            check.reason += " Missing required research evidence: " + ", ".join(missing) + "."
            if "concrete_source_list" in missing:
                if vague_source_lines:
                    check.reason += (
                        " Generic source labels are not concrete identifiers; examples found: "
                        + "; ".join(vague_source_lines[:3])
                        + "."
                    )
                else:
                    check.reason += (
                        " Source entries need concrete identifiers such as URLs, domains, "
                        "repository names, paper identifiers, citation details, or named official documents."
                    )
        else:
            check.status = VERIFICATION_STATUS_PASSED
            check.reason += (
                " Research evidence includes retrieval timestamp, concrete source list, "
                "confidence, primary/secondary distinction, checked-online scope, "
                "not-checked reasons, and stale/unverified warnings."
            )
        summary_lines = [
            "found=" + ", ".join(found or ["none"]),
            "missing=" + ", ".join(missing or ["none"]),
            "concrete_sources=" + " | ".join(concrete_sources[:5] or ["none"]),
        ]
        if vague_source_lines:
            summary_lines.append("vague_source_lines=" + " | ".join(vague_source_lines[:5]))
        if evidence.get("read_errors"):
            summary_lines.append("read_errors=" + " | ".join(evidence.get("read_errors")[:5]))
        check.stdout_excerpt = _excerpt_output("\n".join(summary_lines))
    elif check.id.endswith(":redaction_sanity"):
        # Construct a deliberately fictional value; never copy a real key here.
        synthetic_key = "sk-" + "fictional-redaction-test-" + "0" * 20
        sample = f"OPENAI_API_KEY={synthetic_key} password=plain-secret"
        redacted = _redact_sensitive_text(sample)
        if synthetic_key in redacted or "plain-secret" in redacted:
            check.status = VERIFICATION_STATUS_FAILED
            check.reason += " Redaction helper did not mask the synthetic secret sample."
        else:
            check.status = VERIFICATION_STATUS_PASSED
            check.reason += " Redaction helper masked a synthetic secret sample."
    else:
        if check.status == VERIFICATION_STATUS_PENDING:
            check.status = VERIFICATION_STATUS_NOT_APPLICABLE
            check.reason += " Non-command check has no runner in this phase."
    check.duration_seconds = round(time.monotonic() - start, 3)
    check.timestamp = _utc_timestamp()
    return check


def _run_command_verification_check(
    check: VerificationCheck,
    *,
    remaining_timeout: float | None = None,
) -> VerificationCheck:
    start = time.monotonic()
    timeout = check.timeout_seconds or DEFAULT_MAX_VERIFICATION_TIMEOUT
    if remaining_timeout is not None:
        timeout = max(0.1, min(float(timeout), float(remaining_timeout)))
    command = check.command or []
    if not command:
        check.status = VERIFICATION_STATUS_NOT_APPLICABLE
        check.reason += " No command was available to run."
        check.duration_seconds = round(time.monotonic() - start, 3)
        check.timestamp = _utc_timestamp()
        return check
    try:
        proc = subprocess.run(
            command,
            cwd=check.working_dir or None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        check.exit_code = proc.returncode
        check.stdout_excerpt = _excerpt_output(proc.stdout or "")
        check.stderr_excerpt = _excerpt_output(proc.stderr or "")
        check.status = VERIFICATION_STATUS_PASSED if proc.returncode == 0 else VERIFICATION_STATUS_FAILED
        if proc.returncode == 0:
            check.reason += " Command exited 0."
        else:
            check.reason += f" Command exited {proc.returncode}."
    except subprocess.TimeoutExpired as exc:
        check.status = VERIFICATION_STATUS_BLOCKED
        check.exit_code = None
        check.stdout_excerpt = _excerpt_output(exc.stdout or "")
        check.stderr_excerpt = _excerpt_output(exc.stderr or "")
        check.blocked_reason = f"Command timed out after {timeout:.1f}s."
        check.reason += " Command timed out."
    except FileNotFoundError as exc:
        check.status = VERIFICATION_STATUS_BLOCKED
        check.blocked_reason = f"Command executable not found: {command[0]}"
        check.stderr_excerpt = _excerpt_output(str(exc))
        check.reason += " Command executable was not found."
    except OSError as exc:
        check.status = VERIFICATION_STATUS_BLOCKED
        check.blocked_reason = f"Command could not run: {type(exc).__name__}: {exc}"
        check.stderr_excerpt = _excerpt_output(str(exc))
        check.reason += " Command failed before execution completed."
    check.duration_seconds = round(time.monotonic() - start, 3)
    check.timestamp = _utc_timestamp()
    return check


def _verification_summary_from_checks(
    checks: list[VerificationCheck | dict],
    *,
    phase: str | None = None,
    verification_mode: str = DEFAULT_VERIFICATION_MODE,
) -> dict:
    check_dicts = [_verification_check_to_dict(check) for check in checks]
    relevant = [item for item in check_dicts if phase is None or item.get("phase") == phase]
    statuses = [item.get("status") for item in relevant]
    required_failed = [item for item in relevant if item.get("required") and item.get("status") == VERIFICATION_STATUS_FAILED]
    required_blocked = [item for item in relevant if item.get("required") and item.get("status") == VERIFICATION_STATUS_BLOCKED]
    if verification_mode == "off":
        status = VERIFICATION_STATUS_WAIVED
    elif required_blocked:
        status = VERIFICATION_STATUS_BLOCKED
    elif required_failed:
        status = VERIFICATION_STATUS_FAILED
    elif not relevant:
        status = VERIFICATION_STATUS_NOT_APPLICABLE
    elif all(item == VERIFICATION_STATUS_NOT_APPLICABLE for item in statuses):
        status = VERIFICATION_STATUS_NOT_APPLICABLE
    elif all(item in (VERIFICATION_STATUS_PASSED, VERIFICATION_STATUS_WAIVED, VERIFICATION_STATUS_NOT_APPLICABLE) for item in statuses):
        status = VERIFICATION_STATUS_PASSED if any(item == VERIFICATION_STATUS_PASSED for item in statuses) else statuses[0]
    else:
        status = VERIFICATION_STATUS_MIXED
    return {
        "status": status,
        "mode": verification_mode,
        "phase": phase,
        "required": bool(any(item.get("required") for item in relevant)),
        "total_checks": len(relevant),
        "passed": sum(1 for item in relevant if item.get("status") == VERIFICATION_STATUS_PASSED),
        "failed": sum(1 for item in relevant if item.get("status") == VERIFICATION_STATUS_FAILED),
        "blocked": sum(1 for item in relevant if item.get("status") == VERIFICATION_STATUS_BLOCKED),
        "waived": sum(1 for item in relevant if item.get("status") == VERIFICATION_STATUS_WAIVED),
        "not_applicable": sum(1 for item in relevant if item.get("status") == VERIFICATION_STATUS_NOT_APPLICABLE),
        "required_failed_ids": [item.get("id") for item in required_failed],
        "required_blocked_ids": [item.get("id") for item in required_blocked],
        "note": "Verification summary is computed by Congress from check records.",
        "timestamp": _utc_timestamp(),
    }


def _verification_summary_for_current_context(summary: dict | None) -> dict:
    """Return the latest phase summary agents should treat as current."""
    if not isinstance(summary, dict):
        return {}
    latest = summary.get("latest_phase_summary")
    if not isinstance(latest, dict):
        return dict(summary)
    current = dict(latest)
    current.setdefault("mode", summary.get("mode"))
    current["latest_phase"] = summary.get("latest_phase")
    current["latest_phase_status"] = latest.get("status")
    current["cumulative_status"] = summary.get("status")
    current["cumulative_total_checks"] = summary.get("total_checks", 0)
    current["cumulative_failed"] = summary.get("failed", 0)
    if summary.get("status") != latest.get("status"):
        current["note"] = (
            "Latest phase summary is current for this output; cumulative "
            f"historical status is {summary.get('status', 'not_started')}."
        )
    else:
        current.setdefault("note", summary.get("note"))
    return current


def _render_verification_markdown(
    *,
    classification: TaskClassification | dict | None,
    checks: list[VerificationCheck | dict],
    summary: dict,
    verification_mode: str,
    max_timeout_seconds: int,
) -> str:
    classification_data = _redact_sensitive_data(_task_classification_to_dict(classification))
    check_dicts = [_redact_sensitive_data(_verification_check_to_dict(check)) for check in checks]
    current_summary = _redact_sensitive_data(_verification_summary_for_current_context(summary))
    lines = [
        "# Congress Verification",
        "",
        f"- Timestamp: `{_utc_timestamp()}`",
        f"- Verification mode: `{verification_mode}`",
        f"- Max verification timeout: `{max_timeout_seconds}` seconds",
        f"- Overall status: `{current_summary.get('status', 'not_started')}`",
    ]
    if current_summary.get("cumulative_status") and current_summary.get("cumulative_status") != current_summary.get("status"):
        lines.append(
            f"- Cumulative historical status: `{current_summary.get('cumulative_status')}` "
            f"across `{current_summary.get('cumulative_total_checks', 0)}` retained check records"
        )
    if current_summary.get("latest_phase"):
        lines.append(f"- Latest phase: `{current_summary.get('latest_phase')}`")
    lines.extend([
        "",
        "## Task Classification",
        "",
        "- Classes: " + (", ".join(classification_data.get("classes") or []) or "(none)"),
        f"- Confidence: `{classification_data.get('confidence', 'unknown')}`",
        "",
        "### Signals",
    ])
    signals = classification_data.get("signals") or []
    lines.extend(f"- {item}" for item in signals) if signals else lines.append("- (none)")
    lines.extend(["", "### Required Evidence"])
    required = classification_data.get("required_evidence") or []
    lines.extend(f"- {item}" for item in required) if required else lines.append("- (none)")
    lines.extend(["", "### Skipped Evidence"])
    skipped = classification_data.get("skipped_evidence") or []
    lines.extend(f"- {item}" for item in skipped) if skipped else lines.append("- (none)")
    lines.extend([
        "",
        "### Rationale",
        classification_data.get("rationale", "No rationale recorded."),
        "",
        "## Verification Summary",
        "",
        f"- Status: `{current_summary.get('status', 'not_started')}`",
        f"- Checks: `{current_summary.get('total_checks', 0)}`",
        f"- Passed: `{current_summary.get('passed', 0)}`",
        f"- Failed: `{current_summary.get('failed', 0)}`",
        f"- Blocked: `{current_summary.get('blocked', 0)}`",
        f"- Waived: `{current_summary.get('waived', 0)}`",
        f"- Not applicable: `{current_summary.get('not_applicable', 0)}`",
        "",
        "## Check Records",
    ])
    if not check_dicts:
        lines.append("- (no verification checks recorded)")
    for item in check_dicts:
        lines.extend([
            "",
            f"### {item.get('id', 'check')}: {item.get('title', 'Untitled check')}",
            "",
            f"- Phase: `{item.get('phase')}`",
            f"- Status: `{item.get('status')}`",
            f"- Required: `{str(bool(item.get('required'))).lower()}`",
            f"- Type: `{item.get('check_type')}`",
            f"- Reason: {item.get('reason') or 'No reason recorded.'}",
        ])
        if item.get("command_display"):
            lines.append(f"- Command: `{item.get('command_display')}`")
        if item.get("exit_code") is not None:
            lines.append(f"- Exit code: `{item.get('exit_code')}`")
        if item.get("duration_seconds") is not None:
            lines.append(f"- Duration seconds: `{item.get('duration_seconds')}`")
        if item.get("blocked_reason"):
            lines.append(f"- Blocked reason: {item.get('blocked_reason')}")
        if item.get("waiver_reason"):
            lines.append(f"- Waiver reason: {item.get('waiver_reason')}")
        hashes = item.get("output_hashes") or []
        if hashes:
            lines.append("- Output hashes:")
            for snapshot in hashes:
                state = "exists" if snapshot.get("exists") else "missing"
                if snapshot.get("invalid_type"):
                    state = "invalid"
                lines.append(
                    f"  - `{snapshot.get('path')}`: {state}, "
                    f"sha256=`{snapshot.get('sha256') or 'unavailable'}`"
                )
        if item.get("stdout_excerpt"):
            lines.extend(["- Stdout excerpt:", "```text", item.get("stdout_excerpt", ""), "```"])
        if item.get("stderr_excerpt"):
            lines.extend(["- Stderr excerpt:", "```text", item.get("stderr_excerpt", ""), "```"])
    lines.extend([
        "",
        "## Redaction Note",
        "",
        "Command excerpts and verification metadata are redacted for common secret-like values. "
        "Credential names may remain visible; raw credential values should not be written here.",
        "",
    ])
    return "\n".join(lines)


def _write_verification_markdown(
    working_dir: str,
    *,
    classification: TaskClassification | dict | None,
    checks: list[VerificationCheck | dict],
    summary: dict,
    verification_mode: str,
    max_timeout_seconds: int,
) -> None:
    path = os.path.join(working_dir, CONGRESS_VERIFICATION_FILE)
    _atomic_write_text(
        path,
        _render_verification_markdown(
            classification=classification,
            checks=checks,
            summary=summary,
            verification_mode=verification_mode,
            max_timeout_seconds=max_timeout_seconds,
        ),
    )


def _verification_blocked_state_from_summary(summary: dict, checks: list[VerificationCheck | dict]) -> dict | None:
    if summary.get("status") != VERIFICATION_STATUS_BLOCKED:
        return None
    check_dicts = [_verification_check_to_dict(check) for check in checks]
    blocked = next(
        (item for item in check_dicts if item.get("required") and item.get("status") == VERIFICATION_STATUS_BLOCKED),
        None,
    )
    if not blocked:
        blocked = next((item for item in check_dicts if item.get("status") == VERIFICATION_STATUS_BLOCKED), None)
    if not blocked:
        return None
    reason = blocked.get("blocked_reason") or blocked.get("reason") or "Verification dependency is blocked."
    category = "environment"
    if "credential" in reason.lower() or "user" in reason.lower() or "app connector" in reason.lower():
        category = "user"
    status = _blocked_status_for_category(category)
    return _make_blocked_state(
        status,
        category,
        reason,
        "Resolve the required verification dependency, then resume.",
        "congress_verification",
        evidence=[f"Verification check `{blocked.get('id')}` was blocked."],
        requirement={"type": "verification", "label": blocked.get("id"), "check": blocked.get("title")},
    )


# ============================================================================
# CODEX CLI INTERFACE
# ============================================================================

def _resolve_codex_binary(cli_arg: str | None = None) -> str | None:
    if cli_arg:
        if os.path.exists(cli_arg) or shutil.which(cli_arg):
            return cli_arg
        return None

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


def _is_windows_cmd_launcher(codex_bin: str, platform_name: str | None = None) -> bool:
    platform = platform_name if platform_name is not None else os.name
    return platform == "nt" and codex_bin.lower().endswith((".cmd", ".bat"))


def _build_codex_exec_command(codex_bin: str, session_id: str | None = None,
                              platform_name: str | None = None) -> list[str]:
    """Build the real Codex CLI command without executing it."""
    if _is_windows_cmd_launcher(codex_bin, platform_name):
        base_cmd = ["cmd.exe", "/c", codex_bin]
    else:
        base_cmd = [codex_bin]

    if session_id:
        return base_cmd + ["exec", "resume", session_id,
                           APPROVAL_FLAG, "--skip-git-repo-check", "-"]
    return base_cmd + ["exec", APPROVAL_FLAG, "--skip-git-repo-check", "-"]


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

    cmd = _build_codex_exec_command(codex_bin, session_id=session_id)

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
                if codex_started and inet_down.is_set() and silent_secs >= _silence_timeout:
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
                        _stdout_write_safe(
                            f"\r  {C_YELLOW}|{C_RESET} Waiting for Codex to start... "
                            f"{C_DIM}({silent_secs}s){C_RESET}{pause_hint}    ")
                    else:
                        _stdout_write_safe(
                            f"\r  {C_YELLOW}|{C_RESET} {agent_name} working... "
                            f"{C_DIM}({elapsed_secs}s elapsed, silent {silent_secs}s)"
                            f"{C_RESET}{pause_hint}    ")
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

def _strip_fenced_and_quoted_markdown(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        fence_match = re.match(r"^(```+|~~~+)", stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        if stripped.startswith(">"):
            continue
        lines.append(raw_line)
    return "\n".join(lines)


def _markdown_headings(text: str) -> dict[str, str]:
    cleaned = _strip_fenced_and_quoted_markdown(text)
    headings: dict[str, str] = {}
    current_key = ""
    current_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", raw_line)
        if match:
            if current_key:
                headings[current_key] = "\n".join(current_lines).strip()
            current_key = re.sub(r"[^a-z0-9]+", " ", match.group(1).strip().lower()).strip()
            current_lines = []
        elif current_key:
            current_lines.append(raw_line)
    if current_key:
        headings[current_key] = "\n".join(current_lines).strip()
    return headings


def _review_section_match(
    headings: dict[str, str],
    aliases: tuple[str, ...],
    used_headings: set[str] | None = None,
) -> tuple[str, str]:
    alias_keys = {re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip() for alias in aliases}
    used_headings = used_headings or set()
    for heading, content in headings.items():
        if heading in used_headings:
            continue
        if heading in alias_keys:
            return heading, content.strip()
    return "", ""


def _review_section_content(headings: dict[str, str], aliases: tuple[str, ...]) -> str:
    _heading, content = _review_section_match(headings, aliases)
    return content


def _extract_markdown_review_verdict(review_text: str) -> tuple[str | None, list[str]]:
    cleaned = _strip_fenced_and_quoted_markdown(review_text)
    verdict_lines: list[tuple[int, str]] = []
    invalid_verdict_lines: list[str] = []
    significant: list[tuple[int, str]] = []
    for index, raw_line in enumerate(cleaned.splitlines()):
        stripped = raw_line.strip()
        if not stripped:
            continue
        significant.append((index, stripped))
        match = re.match(r"^(?:[-*]\s*)?VERDICT\s*:\s*([A-Za-z_ -]+)\s*$", stripped, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip().upper().replace(" ", "_").replace("-", "_")
        if value in (REVIEW_VERDICT_APPROVED, REVIEW_VERDICT_NEEDS_REVISION):
            verdict_lines.append((index, value))
        else:
            invalid_verdict_lines.append(stripped)
    reasons: list[str] = []
    if invalid_verdict_lines:
        reasons.append("Unknown verdict line: " + "; ".join(invalid_verdict_lines[:3]))
    if not verdict_lines:
        reasons.append("Missing unquoted terminal `VERDICT:` line.")
        return None, reasons
    unique = {value for _index, value in verdict_lines}
    if len(unique) > 1:
        reasons.append("Conflicting verdict lines were found.")
    last_index, verdict = verdict_lines[-1]
    last_significant_index = significant[-1][0] if significant else -1
    if last_index != last_significant_index:
        reasons.append("Final non-empty unquoted line must be the `VERDICT:` line.")
    return verdict, reasons


def _current_output_hashes(working_dir: str, output_files: list[str]) -> list[dict]:
    return [
        {
            "path": item.get("path"),
            "exists": bool(item.get("exists")),
            "invalid_type": bool(item.get("invalid_type")),
            "sha256": item.get("sha256"),
        }
        for item in _collect_result_output_snapshots(working_dir, output_files)
    ]


def _review_user_update_marker(user_updates: list[dict] | None) -> str:
    payload = json.dumps(_redact_sensitive_data(user_updates or []), sort_keys=True, default=str)
    return f"user_updates={len(user_updates or [])}; user_updates_sha256={_hash_text(payload)}"


def _review_context_marker(user_updates: list[dict] | None,
                           verification_summary: dict | None,
                           round_num: int | None) -> str:
    verification_summary = _verification_summary_for_current_context(verification_summary)
    return (
        f"round={round_num}; "
        f"{_review_user_update_marker(user_updates)}; "
        f"verification_status={verification_summary.get('status', 'not_started')}; "
        f"verification_checks={verification_summary.get('total_checks', 0)}; "
        f"verification_phase={verification_summary.get('latest_phase') or verification_summary.get('phase') or 'unknown'}; "
        f"verification_timestamp={verification_summary.get('timestamp', 'unknown')}"
    )


def _expected_review_hash_strings(output_hashes: list[dict]) -> list[str]:
    return [
        str(item.get("sha256"))
        for item in output_hashes
        if item.get("exists") and not item.get("invalid_type") and item.get("sha256")
    ]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _review_text_has_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in terms)


def _review_quality_line_text(line: str) -> str:
    cleaned = re.sub(r"\s+", " ", line or "").strip()
    cleaned = re.sub(r"^[>\s]*[-*]\s*", "", cleaned).strip()
    cleaned = re.sub(r"^[>\s]*\d+[\.)]\s*", "", cleaned).strip()
    return cleaned


def _review_quality_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in (text or "").splitlines():
        if re.match(r"\s*VERDICT\s*:", line, re.IGNORECASE):
            continue
        cleaned = _review_quality_line_text(line)
        if cleaned:
            lines.append(cleaned)
    return lines


def _review_line_has_negative_rationale(line: str, patterns: tuple[str, ...]) -> bool:
    lowered = _review_quality_line_text(line).lower()
    return any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns)


def _review_rationale_tokens(line: str, terms: tuple[str, ...]) -> list[str]:
    lowered = _review_quality_line_text(line).lower()
    lowered = re.sub(r"\[(?:critical|high|medium|low)\]", " ", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\bseverity\s*:\s*(?:critical|high|medium|low)\b", " ", lowered, flags=re.IGNORECASE)
    for term in terms:
        lowered = lowered.replace(term.lower(), " ")
    term_words: set[str] = set()
    for term in terms:
        term_words.update(re.findall(r"[a-z0-9]+", term.lower()))
    stop_words = QUALITY_RATIONALE_STOP_WORDS | term_words | {
        "accepted",
        "affect",
        "blocker",
        "blocked",
        "cannot",
        "correctness",
        "cosmetic",
        "documented",
        "explicit",
        "fixed",
        "fix",
        "formatting",
        "impact",
        "issue",
        "issues",
        "low",
        "material",
        "medium",
        "none",
        "real",
        "risk",
        "style",
        "typo",
        "waived",
        "waiver",
    }
    return [
        token for token in re.findall(r"[a-z0-9]+", lowered)
        if token not in stop_words and not token.isdigit()
    ]


def _review_line_has_concrete_rationale(
    line: str,
    terms: tuple[str, ...],
    negative_patterns: tuple[str, ...],
    *,
    min_tokens: int,
) -> bool:
    if not _review_text_has_any(line, terms):
        return False
    if _review_line_has_negative_rationale(line, negative_patterns):
        return False
    return len(_review_rationale_tokens(line, terms)) >= min_tokens


def _review_line_has_concrete_non_material_rationale(line: str) -> bool:
    if _review_line_has_negative_rationale(line, QUALITY_NON_MATERIAL_NEGATIVE_PATTERNS):
        return False
    lowered = _review_quality_line_text(line).lower()
    if re.search(
        r"\bno\s+(?:corresponding\s+)?(?:unresolved\s+)?"
        r"(?:critical|high|medium|material|approval[-\s]blocking|blocking)"
        r"[\w\s,/\-]{0,80}\b(?:defect|issue|finding|problem)s?\b",
        lowered,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"\bno\s+corresponding\b[\w\s,/\-]{0,80}\bdefect\b", lowered, re.IGNORECASE):
        return True
    if sum(1 for term in QUALITY_NON_MATERIAL_TERMS if term in lowered) >= 2:
        return True
    return _review_line_has_concrete_rationale(
        line,
        QUALITY_NON_MATERIAL_TERMS,
        QUALITY_NON_MATERIAL_NEGATIVE_PATTERNS,
        min_tokens=2,
    )


def _review_line_has_concrete_accepted_risk_rationale(line: str) -> bool:
    return _review_line_has_concrete_rationale(
        line,
        QUALITY_ACCEPTED_RISK_TERMS,
        QUALITY_ACCEPTED_RISK_NEGATIVE_PATTERNS,
        min_tokens=3,
    )


def _review_quality_keywords(line: str) -> set[str]:
    lowered = _review_quality_line_text(line).lower()
    lowered = re.sub(r"\[(?:critical|high|medium|low)\]", " ", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\bseverity\s*:\s*(?:critical|high|medium|low)\b", " ", lowered, flags=re.IGNORECASE)
    generic = QUALITY_RATIONALE_STOP_WORDS | {
        "add",
        "added",
        "approval",
        "approved",
        "before",
        "critical",
        "fix",
        "fixed",
        "high",
        "issue",
        "issues",
        "low",
        "material",
        "medium",
        "missing",
        "must",
        "needs",
        "remaining",
        "review",
        "should",
        "unresolved",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", lowered)
        if len(token) >= 4 and token not in generic and not token.isdigit()
    }


def _review_lines_share_quality_topic(left: str, right: str) -> bool:
    return bool(_review_quality_keywords(left) & _review_quality_keywords(right))


def _review_line_is_no_issue(line: str) -> bool:
    text = _review_quality_line_text(line).lower()
    text = re.sub(r"[.\s]+$", "", text)
    if re.match(
        r"^(?:none\s+material|no\s+(?:material\s+)?(?:unresolved\s+)?"
        r"(?:issues?|defects?|findings?|problems?))\b[\s.;:,-]+"
        r"(?:residual|remaining)\s+(?:risks?|limitations?)\s+"
        r"(?:are\s+)?(?:correctly\s+)?(?:disclosed|documented|labeled|labelled)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.fullmatch(
        r"no\s+(?:critical,?\s*)?(?:high,?\s*)?(?:or\s+)?medium"
        r"(?:\s+inspector)?\s+(?:issues|defects|findings|problems)"
        r"(?:\s+(?:remain|remained|remaining|found))?",
        text,
    ):
        return True
    return text in {
        "",
        "none",
        "n/a",
        "na",
        "not applicable",
        "no unresolved issues",
        "no unresolved material issues",
        "no open issues",
        "no open material issues",
        "no remaining issues",
        "no remaining material issues",
        "no material issues",
        "no material issues found",
        "no material issues remain",
        "no material unresolved issues",
        "none material",
        "none material remain",
        "none material remaining",
        "none material unresolved",
        "nothing",
    }


def _review_has_no_unresolved_issues(unresolved_text: str) -> bool:
    cleaned_lines = _review_quality_lines(unresolved_text)
    if not cleaned_lines:
        return True
    return all(_review_line_is_no_issue(line) for line in cleaned_lines)


def _review_unresolved_issue_lines(unresolved_text: str) -> list[str]:
    return [
        line for line in _review_quality_lines(unresolved_text)
        if not _review_line_is_no_issue(line)
    ]


def _normalize_issue_status(value: str | None, *, source: str = "inspector") -> str:
    text = re.sub(r"[_\-]+", " ", str(value or "").strip().lower())
    text = re.sub(r"\s+", " ", text)
    status = ISSUE_STATUS_ALIASES.get(text, ISSUE_STATUS_OPEN)
    if source == "researcher":
        if status == ISSUE_STATUS_RESOLVED:
            return ISSUE_STATUS_ADDRESSED
        if status == ISSUE_STATUS_ACCEPTED_RISK:
            return ISSUE_STATUS_CLAIMED_ACCEPTED_RISK
        if status == ISSUE_STATUS_NON_MATERIAL:
            return ISSUE_STATUS_CLAIMED_NON_MATERIAL
    elif source == "inspector" and status == ISSUE_STATUS_ADDRESSED:
        return ISSUE_STATUS_RESOLVED
    return status


def _issue_materiality_from_severity(severity: str | None) -> str:
    return "material" if str(severity or "").upper() in QUALITY_MATERIAL_SEVERITIES else "non_material"


def _issue_title_tokens(text: str) -> set[str]:
    generic = QUALITY_RATIONALE_STOP_WORDS | {
        "accepted",
        "addressed",
        "canonical",
        "evidence",
        "fixed",
        "issue",
        "missing",
        "partially",
        "present",
        "resolved",
        "review",
        "reviewed",
        "severity",
        "source",
        "still",
        "status",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 4 and token not in generic and not token.isdigit()
    }


def _issue_normalized_title_text(text: str | None) -> str:
    cleaned = _clean_issue_title(str(text or ""))
    cleaned = re.sub(r"\bissue-[a-f0-9]{8,32}\b", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()


def _issue_normalized_location(value: str | None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return re.sub(r"\s+", " ", text).lower()


def _issue_severity_band(severity: str | None) -> str:
    sev = str(severity or "").upper()
    if sev in ("CRITICAL", "HIGH"):
        return "high"
    if sev == "MEDIUM":
        return "medium"
    return "low"


def _issue_fingerprint(title: str, severity: str | None, location: str | None) -> str:
    tokens = sorted(_issue_title_tokens(title))[:8]
    payload = "|".join([
        _issue_normalized_location(location),
        _issue_severity_band(severity),
        " ".join(tokens),
    ])
    return _hash_text(payload)[:16]


def _extract_issue_location(text: str) -> str:
    match = re.search(r"\b(?:location|file|path)\s*:\s*`?([^`\n;]+)`?", text, re.IGNORECASE)
    if match:
        return match.group(1).strip(" .")
    match = re.search(r"`([^`]+\.[A-Za-z0-9]{1,8})`", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"\b([A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]{1,8})\b\s*:", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"\b([A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]{1,8})\b", text)
    return match.group(1).strip() if match else ""


def _clean_issue_title(line: str) -> str:
    text = _review_quality_line_text(line)
    text = re.sub(r"\bissue-[a-f0-9]{8,32}\b", " ", text, flags=re.IGNORECASE)
    text = text.replace("`", " ")
    text = re.sub(r"\[(?:CRITICAL|HIGH|MEDIUM|LOW)\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSeverity\s*:\s*(?:CRITICAL|HIGH|MEDIUM|LOW)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:CRITICAL|HIGH|MEDIUM|LOW)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(?:Issue|Finding)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*[A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]{1,8}\s*:\s*", "", text)
    text = re.sub(r"\b(?:must|should)\s+be\s+(?:fixed|addressed|resolved)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:needs|need)\s+(?:to\s+)?be\s+(?:fixed|addressed|resolved)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -.;")
    return text[:180] or "Unspecified review issue"


def _make_issue_record(
    *,
    title: str,
    severity: str,
    reviewer: str,
    location: str = "",
    round_num: int | None = None,
    status: str = ISSUE_STATUS_OPEN,
    rationale: str = "",
    source_artifact: str = "",
) -> IssueRecord:
    normalized_severity = str(severity or "MEDIUM").upper()
    fingerprint = _issue_fingerprint(title, normalized_severity, location)
    issue_id = f"issue-{fingerprint}"
    return IssueRecord(
        id=issue_id,
        title=title,
        severity=normalized_severity,
        reviewers=[reviewer] if reviewer else [],
        location=location,
        round=round_num,
        status=status,
        materiality=_issue_materiality_from_severity(normalized_severity),
        rationale=rationale,
        source_artifact=source_artifact,
        fingerprint=fingerprint,
        first_seen_round=round_num,
        last_seen_round=round_num,
        origins=[{
            "reviewer": reviewer,
            "round": round_num,
            "source_artifact": source_artifact,
        }] if reviewer else [],
    )


def _issue_record_to_dict(issue: IssueRecord | dict | None) -> dict:
    if issue is None:
        return {}
    if isinstance(issue, dict):
        return dict(issue)
    return asdict(issue)


def _issue_record_from_dict(data: IssueRecord | dict | None) -> IssueRecord | None:
    if data is None:
        return None
    if isinstance(data, IssueRecord):
        return data
    if not isinstance(data, dict):
        return None
    return IssueRecord(
        id=str(data.get("id") or data.get("fingerprint") or uuid.uuid4().hex[:12]),
        title=str(data.get("title") or "Unspecified review issue"),
        severity=str(data.get("severity") or "MEDIUM").upper(),
        reviewers=list(data.get("reviewers") or ([] if not data.get("reviewer") else [data.get("reviewer")])),
        location=str(data.get("location") or ""),
        round=data.get("round"),
        status=str(data.get("status") or ISSUE_STATUS_OPEN),
        materiality=str(data.get("materiality") or _issue_materiality_from_severity(data.get("severity"))),
        rationale=str(data.get("rationale") or ""),
        source_artifact=str(data.get("source_artifact") or ""),
        fingerprint=str(data.get("fingerprint") or _issue_fingerprint(
            str(data.get("title") or ""),
            str(data.get("severity") or "MEDIUM"),
            str(data.get("location") or ""),
        )),
        first_seen_round=data.get("first_seen_round"),
        last_seen_round=data.get("last_seen_round"),
        resolution_round=data.get("resolution_round"),
        origins=list(data.get("origins") or []),
        timestamp=str(data.get("timestamp") or _utc_timestamp()),
    )


def _issue_records_to_state(issues: list[IssueRecord | dict] | None) -> dict:
    records = [_issue_record_to_dict(item) for item in (issues or [])]
    open_material = [
        item for item in records
        if item.get("materiality") == "material"
        and item.get("status") not in ISSUE_CLOSED_STATUSES
    ]
    return {
        "issues": records,
        "open_material_count": len(open_material),
        "open_count": len([item for item in records if item.get("status") not in ISSUE_CLOSED_STATUSES]),
        "total_count": len(records),
    }


def _issue_records_from_state(state: dict | list | None) -> list[IssueRecord]:
    raw_items = state if isinstance(state, list) else (state or {}).get("issues") or []
    result: list[IssueRecord] = []
    for item in raw_items:
        record = _issue_record_from_dict(item)
        if record is not None:
            result.append(record)
    return result


def _extract_issue_records_from_review(
    review_text: str,
    *,
    reviewer: str,
    artifact_path: str,
    round_num: int | None,
) -> list[IssueRecord]:
    headings = _markdown_headings(review_text)
    sections = []
    for aliases in (
        REVIEW_REQUIRED_SECTION_ALIASES.get("findings", ()),
        REVIEW_REQUIRED_SECTION_ALIASES.get("unresolved", ()),
    ):
        content = _review_section_content(headings, aliases)
        if content:
            sections.append(content)
    text = "\n".join(sections) or review_text
    records: list[IssueRecord] = []
    for item in _extract_review_severity_records(text):
        line = str(item.get("line") or "")
        severity = str(item.get("severity") or "MEDIUM").upper()
        title = _clean_issue_title(line)
        if _review_line_is_no_issue(title):
            continue
        location = _extract_issue_location(line)
        records.append(_make_issue_record(
            title=title,
            severity=severity,
            reviewer=reviewer,
            location=location,
            round_num=round_num,
            source_artifact=artifact_path,
        ))
    return records


def _extract_structured_blocks(section_text: str) -> list[dict]:
    blocks: list[dict] = []
    current: dict | None = None
    for raw in (section_text or "").splitlines():
        line = raw.strip()
        if not line or _review_line_is_no_issue(line):
            continue
        inline_status = _split_inline_issue_status(line)
        issue_match = re.match(r"^(?:[-*]\s*)?(?:Issue|Finding)\s*:\s*(.+)$", line, re.IGNORECASE)
        if issue_match:
            if current:
                blocks.append(current)
            issue_text = issue_match.group(1).strip()
            nested_inline_status = _split_inline_issue_status(issue_text)
            if nested_inline_status:
                issue_text, status_text, rationale_text = nested_inline_status
                current = {"issue": issue_text, "status": status_text, "lines": [line]}
                if rationale_text:
                    current["rationale"] = rationale_text
            else:
                current = {"issue": issue_text, "lines": [line]}
            continue
        if inline_status:
            if current:
                blocks.append(current)
            issue_text, status_text, rationale_text = inline_status
            current = {"issue": issue_text, "status": status_text, "lines": [line]}
            if rationale_text:
                current["rationale"] = rationale_text
            continue
        if current is None:
            current = {"issue": line.lstrip("-* ").strip(), "lines": [line]}
            continue
        current["lines"].append(line)
        status_match = re.match(r"^(?:[-*]\s*)?Status\s*:\s*(.+)$", line, re.IGNORECASE)
        if status_match:
            current["status"] = status_match.group(1).strip()
        loc_match = re.match(r"^(?:[-*]\s*)?(?:Location changed|Location|File|Path)\s*:\s*(.+)$", line, re.IGNORECASE)
        if loc_match:
            current["location"] = loc_match.group(1).strip()
        reason_match = re.match(r"^(?:[-*]\s*)?(?:Reason|Evidence|Rationale|Blocker)\s*:\s*(.+)$", line, re.IGNORECASE)
        if reason_match:
            current["rationale"] = reason_match.group(1).strip()
    if current:
        blocks.append(current)
    for block in blocks:
        if "status" not in block:
            joined = " ".join(block.get("lines") or [])
            match = re.search(r"\bStatus\s*:\s*([A-Za-z_ -]+)", joined, re.IGNORECASE)
            if match:
                block["status"] = match.group(1).strip()
        if "rationale" not in block:
            rationale_parts = []
            for line in block.get("lines") or []:
                if re.match(r"^(?:[-*]\s*)?(?:Issue|Finding|Status|Location changed|Location|File|Path)\s*:", line, re.IGNORECASE):
                    continue
                rationale_parts.append(line.lstrip("-* ").strip())
            block["rationale"] = " ".join(rationale_parts).strip()
    return blocks


def _extract_issue_status_records(
    markdown_text: str,
    *,
    section_aliases: tuple[str, ...],
    source: str,
    reviewer: str = "",
    round_num: int | None = None,
    source_artifact: str = "",
) -> list[dict]:
    headings = _markdown_headings(markdown_text)
    content = _review_section_content(headings, section_aliases)
    if not content:
        return []
    records: list[dict] = []
    for block in _extract_structured_blocks(content):
        joined_lines = " ".join(str(line) for line in (block.get("lines") or []))
        issue_id_match = re.search(r"\bissue-[a-f0-9]{8,32}\b", joined_lines, re.IGNORECASE)
        title = _clean_issue_title(str(block.get("issue") or ""))
        if _review_line_is_no_issue(title):
            continue
        raw_status = str(block.get("status") or "")
        status = _normalize_issue_status(raw_status, source=source)
        rationale = str(block.get("rationale") or "")
        location = str(
            block.get("location")
            or _extract_issue_location(title)
            or _extract_issue_location(rationale)
            or ""
        )
        records.append({
            "id": issue_id_match.group(0).lower() if issue_id_match else "",
            "title": title,
            "status": status,
            "raw_status": raw_status,
            "location": location,
            "rationale": rationale,
            "reviewer": reviewer,
            "round": round_num,
            "source": source,
            "source_artifact": source_artifact,
            "fingerprint": _issue_fingerprint(title, "MEDIUM", location),
        })
    return records


def _split_inline_issue_status(line: str) -> tuple[str, str, str] | None:
    status_pattern = (
        r"RESOLVED|FIXED|ADDRESSED|STILL\s+PRESENT|PARTIALLY\s+RESOLVED|"
        r"PARTIALLY\s+FIXED|PARTIALLY\s+ADDRESSED|ACCEPTED[_\s-]+RISK|"
        r"NON[_\s-]+MATERIAL|NOT\s+FIXED|NOT\s+ADDRESSED|OPEN"
    )
    text = str(line or "").strip()
    match = re.match(
        rf"^(?:[-*]\s*)?(?P<issue>.+?)\s*(?:[:\-]\s*)"
        rf"(?P<status>{status_pattern})\b(?P<tail>.*)$",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.match(
            rf"^(?:[-*]\s*)?(?P<issue>.+?)\s+"
            rf"(?P<status>{status_pattern})\b(?P<tail>.*)$",
            text,
            re.IGNORECASE,
        )
    if not match:
        return None
    issue = match.group("issue").strip()
    if re.fullmatch(r"(?:Status|Evidence|Rationale|Reason|Blocker|Location|File|Path)", issue, re.IGNORECASE):
        return None
    status = re.sub(r"[_\-]+", " ", match.group("status")).strip()
    tail = match.group("tail").strip(" .;-")
    resolved_tail = re.match(
        r"^/\s*(RESOLVED|FIXED|ADDRESSED)\b(?P<tail>.*)$",
        tail,
        re.IGNORECASE,
    )
    if resolved_tail and re.fullmatch(r"NON\s+MATERIAL", status, re.IGNORECASE):
        status = resolved_tail.group(1).strip()
        tail = resolved_tail.group("tail").strip(" .;-")
    non_material_tail = re.match(
        r"^/\s*NON[_\s-]+MATERIAL\b(?P<tail>.*)$",
        tail,
        re.IGNORECASE,
    )
    if non_material_tail and re.fullmatch(r"(RESOLVED|FIXED|ADDRESSED)", status, re.IGNORECASE):
        tail = non_material_tail.group("tail").strip(" .;-")
    return issue, status, tail


def _excellence_key_for_label(label: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()
    for key, canonical, aliases in EXCELLENCE_CHECKLIST_ITEMS:
        candidates = (canonical.lower(),) + tuple(alias.lower() for alias in aliases)
        if any(candidate in normalized or normalized in candidate for candidate in candidates):
            return key
    return None


def _excellence_label_for_key(key: str) -> str:
    for item_key, label, _aliases in EXCELLENCE_CHECKLIST_ITEMS:
        if item_key == key:
            return label
    return key.replace("_", " ").title()


def _extract_evidence_text(line: str) -> str:
    match = re.search(r"\b(?:Evidence|Reason|Rationale|Blocker)\s*:\s*(.+)$", line, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    parts = re.split(r"\s+-\s+", line, maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()
    return re.sub(r"^[\s:;.\-]+", "", line).strip()


def _extract_excellence_checklist(
    review_text: str,
    *,
    reviewer: str,
    round_num: int | None,
    source_artifact: str,
) -> tuple[list[ExcellenceChecklistItem], list[str], list[str]]:
    headings = _markdown_headings(review_text)
    content = _review_section_content(
        headings,
        ("excellence checklist", "excellence", "quality checklist", "final excellence checklist"),
    )
    support_sections = []
    for aliases in (
        REVIEW_REQUIRED_SECTION_ALIASES.get("findings", ()),
        REVIEW_REQUIRED_SECTION_ALIASES.get("evidence", ()),
        REVIEW_REQUIRED_SECTION_ALIASES.get("commands", ()),
        REVIEW_REQUIRED_SECTION_ALIASES.get("verification", ()),
    ):
        section = _review_section_content(headings, aliases)
        if section and len(re.findall(r"[A-Za-z0-9]+", section)) >= 12:
            support_sections.append(section)
    has_substantive_support = len(support_sections) >= 2
    items: dict[str, ExcellenceChecklistItem] = {}
    blockers: list[str] = []
    if content:
        for raw in content.splitlines():
            line = raw.strip()
            if not line or _review_line_is_no_issue(line):
                continue
            line = re.sub(r"^[-*]\s*", "", line).strip()
            match = re.match(r"(.+?)\s*:\s*(PASS|FAIL|WAIVED|UNKNOWN)\b(.*)$", line, re.IGNORECASE)
            if match:
                key = _excellence_key_for_label(match.group(1))
                if not key:
                    continue
                status = match.group(2).upper()
                evidence = _extract_evidence_text(match.group(3).strip())
            else:
                status_first = re.match(r"(PASS|FAIL|WAIVED|UNKNOWN)\s*:\s*(.+)$", line, re.IGNORECASE)
                if not status_first:
                    continue
                status = status_first.group(1).upper()
                remainder = status_first.group(2).strip()
                key = _excellence_key_for_label(remainder)
                if not key:
                    continue
                label_text = _excellence_label_for_key(key)
                evidence = re.sub(
                    rf"^{re.escape(label_text)}\b[\s:;.\-]*",
                    "",
                    remainder,
                    flags=re.IGNORECASE,
                ).strip()
                if not evidence and "." in remainder:
                    evidence = remainder.split(".", 1)[1].strip()
            label = _excellence_label_for_key(key)
            pass_period_ack = bool(re.search(r"\bPASS\s*\.", line, re.IGNORECASE))
            if (
                status == EXCELLENCE_STATUS_PASS
                and not evidence
                and has_substantive_support
                and pass_period_ack
            ):
                evidence = "Supported by the review's findings, evidence, command, and verification sections."
            blocker = ""
            if status in (EXCELLENCE_STATUS_FAIL, EXCELLENCE_STATUS_UNKNOWN):
                blocker = evidence or f"{label} is {status}."
            elif status == EXCELLENCE_STATUS_PASS and not evidence:
                blocker = f"{label} is PASS without supporting evidence."
            elif status == EXCELLENCE_STATUS_WAIVED and not evidence:
                blocker = f"{label} is WAIVED without explicit waiver evidence."
            item = ExcellenceChecklistItem(
                key=key,
                label=label,
                status=status,
                evidence=evidence,
                reviewer=reviewer,
                round=round_num,
                blocker=blocker,
                source_artifact=source_artifact,
            )
            items[key] = item
            if blocker:
                blockers.append(f"{reviewer} {label}: {blocker}")
    missing = [
        key for key in EXCELLENCE_CHECKLIST_KEYS
        if key not in items
    ]
    for key in missing:
        blockers.append(f"{reviewer} Excellence checklist missing item: {_excellence_label_for_key(key)}.")
    return list(items.values()), missing, blockers


def _issue_records_match(left: IssueRecord | dict, right: IssueRecord | dict) -> bool:
    ldata = _issue_record_to_dict(left)
    rdata = _issue_record_to_dict(right)
    if ldata.get("id") and rdata.get("id") and str(ldata.get("id")) == str(rdata.get("id")):
        return True
    if ldata.get("fingerprint") and ldata.get("fingerprint") == rdata.get("fingerprint"):
        return True
    lloc = _issue_normalized_location(ldata.get("location"))
    rloc = _issue_normalized_location(rdata.get("location"))
    ltitle = _issue_normalized_title_text(str(ldata.get("title") or ""))
    rtitle = _issue_normalized_title_text(str(rdata.get("title") or ""))
    if ltitle and ltitle == rtitle and lloc and rloc and lloc == rloc:
        return True
    if (
        ltitle
        and rtitle
        and (ltitle in rtitle or rtitle in ltitle)
        and min(len(ltitle.split()), len(rtitle.split())) >= 2
        and not (lloc and rloc and lloc != rloc)
    ):
        return True
    ltokens = _issue_title_tokens(str(ldata.get("title") or ""))
    rtokens = _issue_title_tokens(str(rdata.get("title") or ""))
    overlap = ltokens & rtokens
    if not ltokens or not rtokens:
        return False
    union = ltokens | rtokens
    jaccard = len(overlap) / max(1, len(union))
    coverage = len(overlap) / max(1, min(len(ltokens), len(rtokens)))
    same_severity_band = _issue_severity_band(ldata.get("severity")) == _issue_severity_band(rdata.get("severity"))
    if lloc and rloc and lloc == rloc and len(overlap) >= 2 and (coverage >= 0.75 or jaccard >= 0.6):
        return True
    if len(overlap) >= 2 and coverage >= 0.9 and jaccard >= 0.75:
        return True
    return len(overlap) >= 3 and same_severity_band and jaccard >= 0.6


def _find_matching_issue_index(issues: list[IssueRecord], incoming: IssueRecord | dict) -> int | None:
    incoming_data = _issue_record_to_dict(incoming)
    incoming_id = str(incoming_data.get("id") or "")
    if incoming_id:
        for index, issue in enumerate(issues):
            if issue.id == incoming_id:
                return index
    for index, issue in enumerate(issues):
        if _issue_records_match(issue, incoming):
            return index
    incoming_title = _issue_normalized_title_text(str(incoming_data.get("title") or ""))
    incoming_location = _issue_normalized_location(incoming_data.get("location"))
    exact_title_matches: list[int] = []
    if incoming_title:
        for index, issue in enumerate(issues):
            issue_location = _issue_normalized_location(issue.location)
            if _issue_normalized_title_text(issue.title) != incoming_title:
                continue
            if incoming_location and issue_location and incoming_location != issue_location:
                continue
            exact_title_matches.append(index)
    if len(exact_title_matches) == 1:
        return exact_title_matches[0]
    return None


def _merge_issue_record(existing: IssueRecord, incoming: IssueRecord) -> IssueRecord:
    if incoming.severity in ("CRITICAL", "HIGH") and existing.severity not in ("CRITICAL", "HIGH"):
        existing.severity = incoming.severity
    elif incoming.severity == "MEDIUM" and existing.severity == "LOW":
        existing.severity = incoming.severity
    existing.materiality = _issue_materiality_from_severity(existing.severity)
    for reviewer in incoming.reviewers:
        if reviewer and reviewer not in existing.reviewers:
            existing.reviewers.append(reviewer)
    if incoming.location and not existing.location:
        existing.location = incoming.location
    existing.last_seen_round = incoming.last_seen_round or incoming.round or existing.last_seen_round
    if incoming.source_artifact and not existing.source_artifact:
        existing.source_artifact = incoming.source_artifact
    for origin in incoming.origins:
        if origin not in existing.origins:
            existing.origins.append(origin)
    if existing.status in ISSUE_CLOSED_STATUSES and incoming.status == ISSUE_STATUS_OPEN:
        existing.status = ISSUE_STATUS_OPEN
        existing.resolution_round = None
    if incoming.rationale:
        existing.rationale = incoming.rationale
    return existing


def _issue_status_has_valid_rationale(status: str, rationale: str, title: str) -> bool:
    if status not in {
        ISSUE_STATUS_CLAIMED_ACCEPTED_RISK,
        ISSUE_STATUS_CLAIMED_NON_MATERIAL,
        ISSUE_STATUS_ACCEPTED_RISK,
        ISSUE_STATUS_NON_MATERIAL,
    }:
        return True
    text = str(rationale or "").strip()
    if not text:
        return False
    if status in (ISSUE_STATUS_CLAIMED_ACCEPTED_RISK, ISSUE_STATUS_ACCEPTED_RISK):
        return _review_line_has_concrete_accepted_risk_rationale(text)
    return _review_line_has_concrete_non_material_rationale(text)


def _issue_lifecycle_blockers_from_records(issues: list[IssueRecord | dict]) -> list[str]:
    blockers: list[str] = []
    for raw in issues or []:
        issue = _issue_record_from_dict(raw)
        if issue is None or issue.materiality != "material":
            continue
        title = issue.title or issue.id
        if issue.status in ISSUE_OPEN_STATUSES:
            blockers.append(
                f"Unresolved material issue `{issue.id}` remains {issue.status}: {title}"
            )
        elif issue.status in (ISSUE_STATUS_ACCEPTED_RISK, ISSUE_STATUS_NON_MATERIAL):
            if not _issue_status_has_valid_rationale(issue.status, issue.rationale, issue.title):
                blockers.append(
                    f"Issue `{issue.id}` has invalid {issue.status} rationale: {title}"
                )
    return blockers


def _issue_public_summary_lines(issues: list[IssueRecord | dict], *, limit: int = 8) -> list[str]:
    lines: list[str] = []
    for raw in issues[:limit]:
        item = _issue_record_to_dict(raw)
        reviewers = ", ".join(str(r) for r in item.get("reviewers") or []) or "unknown"
        location = f" location=`{item.get('location')}`" if item.get("location") else ""
        lines.append(
            f"- `{item.get('id')}` [{item.get('severity')}] status=`{item.get('status')}` "
            f"reviewers=`{reviewers}`{location}: {item.get('title')}"
        )
    if len(issues) > limit:
        lines.append(f"- ... {len(issues) - limit} additional issue(s) tracked.")
    return lines


def _issue_prompt_summary(
    issues: list[IssueRecord | dict],
    *,
    audience: str = "default",
) -> str:
    records = [_issue_record_from_dict(item) for item in (issues or [])]
    records = [item for item in records if item is not None and item.status not in ISSUE_CLOSED_STATUSES]
    if not records:
        return ""
    if audience == "inspector_2_pre_review":
        own = [item for item in records if item.reviewers and set(item.reviewers) <= {"inspector_2"}]
        if not own:
            return (
                "\n============================================================\n"
                "CONGRESS ISSUE LIFECYCLE CONTEXT\n"
                "============================================================\n"
                "Inspector 2 pre-review independence mode is active. Inspector 1-derived issue titles, excerpts, paths, source artifacts, and reviewer metadata are withheld until Inspector 2 produces independent findings.\n"
            )
        records = own
    lines = [
        "",
        "=" * 60,
        "CONGRESS ISSUE LIFECYCLE CONTEXT",
        "=" * 60,
        "Congress tracks material review issues across rounds. Open material issues must be fixed or independently verified as resolved, accepted-risk, or non-material before approval.",
        *_issue_public_summary_lines(records),
    ]
    return "\n".join(lines) + "\n"


def _review_issue_section_text(review_text: str) -> str:
    headings = _markdown_headings(review_text)
    if not headings:
        return review_text or ""
    issue_aliases = (
        REVIEW_REQUIRED_SECTION_ALIASES.get("findings", ())
        + REVIEW_REQUIRED_SECTION_ALIASES.get("unresolved", ())
        + ("issue list", "review issues", "material issues")
    )
    issue_heading_keys = {
        re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()
        for alias in issue_aliases
    }
    sections = [
        content.strip()
        for heading, content in headings.items()
        if heading in issue_heading_keys and content.strip()
    ]
    return "\n".join(sections) if sections else review_text or ""


def _review_line_is_issue_candidate(line: str) -> bool:
    raw = str(line or "").strip()
    cleaned = _review_quality_line_text(raw)
    if not cleaned or _review_line_is_no_issue(cleaned):
        return False
    if (
        _review_line_has_concrete_non_material_rationale(cleaned)
        or _review_line_has_concrete_accepted_risk_rationale(cleaned)
    ):
        return False
    if re.match(r"^(?:Issue|Finding)\s*:", cleaned, re.IGNORECASE):
        return True
    if re.search(r"\b(?:location|file|path)\s*:", cleaned, re.IGNORECASE):
        return _review_text_has_any(cleaned, QUALITY_ACTIONABLE_TERMS)
    if re.match(r"^[A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]{1,8}\s*:", cleaned):
        return _review_text_has_any(cleaned, QUALITY_ACTIONABLE_TERMS)
    if re.match(r"^\s*(?:[-*]\s*)?\d+[\.)]\s+", raw):
        return _review_text_has_any(cleaned, QUALITY_ACTIONABLE_TERMS)
    if re.match(r"^\s*[-*]\s+", raw) and _review_text_has_any(cleaned, QUALITY_ACTIONABLE_TERMS):
        return bool(
            re.search(
                r"\b(missing|incomplete|incorrect|unsupported|unverified|ambiguous|bug|risk|"
                r"should|must|needs|required|materially\s+improve)\b",
                cleaned,
                re.IGNORECASE,
            )
        )
    return False


def _review_line_is_issue_detail_continuation(line: str) -> bool:
    cleaned = _review_quality_line_text(line)
    return bool(
        re.match(
            r"^(?:Problem|Fix|Location|File|Path|Evidence|Rationale|Reason|Impact|"
            r"Recommendation|Suggested\s+(?:fix|change)|Status)\s*:",
            cleaned,
            re.IGNORECASE,
        )
    )


def _extract_review_severity_records(review_text: str) -> list[dict]:
    records: list[dict] = []
    severity_patterns = (
        re.compile(r"\[(CRITICAL|HIGH|MEDIUM|LOW)\]", re.IGNORECASE),
        re.compile(r"\bSeverity\s*:\s*(CRITICAL|HIGH|MEDIUM|LOW)\b", re.IGNORECASE),
        re.compile(r"^\s*[-*]\s*(CRITICAL|HIGH|MEDIUM|LOW)\s*[:\-]", re.IGNORECASE),
    )
    active_explicit_severity: str | None = None
    for line in (review_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matched_explicit = False
        for pattern in severity_patterns:
            for match in pattern.finditer(stripped):
                severity = match.group(1).upper()
                records.append({
                    "severity": severity,
                    "line": stripped[:220],
                })
                active_explicit_severity = severity
                matched_explicit = True
        if (
            not matched_explicit
            and active_explicit_severity
            and _review_line_is_issue_detail_continuation(stripped)
        ):
            continue
        if not matched_explicit and _review_line_is_issue_candidate(stripped):
            records.append({
                "severity": "MEDIUM",
                "line": stripped[:220],
                "inferred": True,
            })
    return records


def _review_record_has_concrete_accepted_risk(record: dict, rationale_lines: list[str]) -> bool:
    issue_line = str(record.get("line") or "")
    if _review_line_has_concrete_accepted_risk_rationale(issue_line):
        return True
    for line in rationale_lines:
        if (
            _review_line_has_concrete_accepted_risk_rationale(line)
            and _review_lines_share_quality_topic(issue_line, line)
        ):
            return True
    return False


def _review_record_has_concrete_low_rationale(record: dict, unresolved_lines: list[str]) -> bool:
    issue_line = str(record.get("line") or "")
    if (
        _review_line_has_concrete_non_material_rationale(issue_line)
        or _review_line_has_concrete_accepted_risk_rationale(issue_line)
    ):
        return True
    for line in unresolved_lines:
        if not _review_lines_share_quality_topic(issue_line, line):
            continue
        if (
            _review_line_has_concrete_non_material_rationale(line)
            or _review_line_has_concrete_accepted_risk_rationale(line)
        ):
            return True
    return False


def _review_quality_signals(
    review_text: str,
    unresolved_text: str,
    raw_verdict: str | None,
    quality_mode: str,
) -> dict:
    mode = _normalize_quality_mode(quality_mode, strict=False)
    unresolved_for_quality = "\n".join(
        line for line in (unresolved_text or "").splitlines()
        if not re.match(r"\s*VERDICT\s*:", line, re.IGNORECASE)
    )
    issue_analysis_text = _review_issue_section_text(review_text)
    severity_records = _extract_review_severity_records(issue_analysis_text)
    detected = _dedupe_preserve_order([item["severity"] for item in severity_records])
    material_records = [
        item for item in severity_records
        if item.get("severity") in QUALITY_MATERIAL_SEVERITIES
    ]
    material_severities = _dedupe_preserve_order([item["severity"] for item in material_records])
    review_lines = _review_quality_lines(review_text)
    unresolved_lines = _review_unresolved_issue_lines(unresolved_for_quality)
    non_material = any(_review_line_has_concrete_non_material_rationale(line) for line in review_lines)
    accepted_risk = any(_review_line_has_concrete_accepted_risk_rationale(line) for line in review_lines)
    unresolved_none = not unresolved_lines
    actionable_unresolved = any(
        _review_text_has_any(line, QUALITY_ACTIONABLE_TERMS)
        for line in unresolved_lines
    )
    unresolved_blockers = [
        line for line in unresolved_lines
        if not (
            _review_line_has_concrete_non_material_rationale(line)
            or _review_line_has_concrete_accepted_risk_rationale(line)
        )
    ]
    blockers: list[str] = []

    effective_verdict = raw_verdict
    if mode == QUALITY_MODE_BEST and raw_verdict == REVIEW_VERDICT_APPROVED:
        material_blockers = [
            item for item in material_records
            if not _review_record_has_concrete_accepted_risk(item, review_lines)
        ]
        if material_blockers:
            for item in material_blockers[:5]:
                blockers.append(
                    "Material issue remains despite APPROVED verdict: "
                    f"{item.get('line')}"
                )
            if len(material_blockers) > 5:
                blockers.append(
                    f"{len(material_blockers) - 5} additional material severity issue(s) remain."
                )
        low_records = [item for item in severity_records if item.get("severity") == "LOW"]
        low_blockers = [
            item for item in low_records
            if not _review_record_has_concrete_low_rationale(item, review_lines)
        ]
        if low_blockers and not material_records:
            blockers.append(
                "LOW issue remains without concrete non-material or accepted-risk rationale: "
                f"{low_blockers[0].get('line')}"
            )
        if unresolved_blockers:
            excerpt = " ".join(" ".join(unresolved_blockers).split())[:220]
            blockers.append(
                "Approved review lists unresolved issues without concrete non-material "
                f"or accepted-risk rationale: {excerpt}"
            )
        if blockers:
            effective_verdict = REVIEW_VERDICT_NEEDS_REVISION

    return {
        "quality_mode": mode,
        "raw_verdict": raw_verdict,
        "effective_verdict": effective_verdict,
        "quality_status": (
            REVIEW_STATUS_NEEDS_REVISION
            if effective_verdict == REVIEW_VERDICT_NEEDS_REVISION
            else REVIEW_STATUS_APPROVED
            if effective_verdict == REVIEW_VERDICT_APPROVED
            else REVIEW_STATUS_MALFORMED
        ),
        "quality_blocking_reasons": blockers,
        "detected_severities": detected,
        "material_issue_severities": material_severities,
        "material_issue_count": len(material_records),
        "non_material_rationale_present": non_material,
        "accepted_risk_rationale_present": accepted_risk,
        "actionable_unresolved_present": actionable_unresolved,
    }


def assess_markdown_review(
    review_text: str,
    *,
    reviewer: str,
    artifact_path: str,
    round_num: int | None,
    working_dir: str,
    output_files: list[str],
    verification_summary: dict | None,
    user_updates: list[dict] | None,
    partial_output_required: bool = False,
    quality_mode: str = DEFAULT_QUALITY_MODE,
) -> ReviewAssessment:
    verdict, verdict_reasons = _extract_markdown_review_verdict(review_text)
    headings = _markdown_headings(review_text)
    missing_sections: list[str] = []
    section_contents: dict[str, str] = {}
    used_headings: set[str] = set()
    for key, aliases in REVIEW_REQUIRED_SECTION_ALIASES.items():
        heading, content = _review_section_match(headings, aliases, used_headings)
        if content:
            used_headings.add(heading)
            section_contents[key] = content
        else:
            missing_sections.append(REVIEW_SECTION_LABELS.get(key, key))

    output_hashes = _current_output_hashes(working_dir, output_files)
    expected_hashes = _expected_review_hash_strings(output_hashes)
    cleaned = _strip_fenced_and_quoted_markdown(review_text)
    cleaned_for_hash_match = cleaned.lower()
    referenced_hashes = [sha for sha in expected_hashes if sha.lower() in cleaned_for_hash_match]
    stale_reasons: list[str] = []
    if expected_hashes and len(referenced_hashes) != len(expected_hashes):
        missing = [sha for sha in expected_hashes if sha.lower() not in cleaned_for_hash_match]
        stale_reasons.append(
            "Review does not reference current requested-output sha256 hash(es): "
            + ", ".join(missing[:3])
        )

    verification_referenced = CONGRESS_VERIFICATION_FILE in cleaned
    if not verification_referenced:
        missing_sections.append("verification evidence file reference")

    session_marker = _review_context_marker(user_updates, verification_summary, round_num)
    marker_present = session_marker in cleaned
    if not marker_present:
        stale_reasons.append("Review does not reference the current review freshness marker.")

    partial_ack = (
        "partial_output_recovered" in cleaned
        or "partial output" in cleaned.lower()
        or "partial-output" in cleaned.lower()
    )
    malformed_reasons = list(verdict_reasons)
    if missing_sections:
        malformed_reasons.append("Missing required Markdown review section(s): " + ", ".join(missing_sections))
    if partial_output_required and not partial_ack:
        malformed_reasons.append("Review does not acknowledge partial output recovery metadata.")

    unresolved_content = section_contents.get("unresolved", "")
    unresolved_record = "\n".join(
        line for line in unresolved_content.splitlines()
        if not re.match(r"\s*VERDICT\s*:", line, re.IGNORECASE)
    ).strip()
    quality_signals = _review_quality_signals(
        cleaned,
        unresolved_record,
        verdict,
        _normalize_quality_mode(quality_mode, strict=False),
    )
    parsed_issues = _extract_issue_records_from_review(
        cleaned,
        reviewer=reviewer,
        artifact_path=artifact_path,
        round_num=round_num,
    )
    previous_issue_statuses = _extract_issue_status_records(
        cleaned,
        section_aliases=("previous issues", "previous issue verification", "issue resolution"),
        source="inspector",
        reviewer=reviewer,
        round_num=round_num,
        source_artifact=artifact_path,
    )
    excellence_items, excellence_missing, excellence_blockers = _extract_excellence_checklist(
        cleaned,
        reviewer=reviewer,
        round_num=round_num,
        source_artifact=artifact_path,
    )

    if malformed_reasons:
        status = REVIEW_STATUS_MALFORMED
    elif stale_reasons:
        status = REVIEW_STATUS_STALE
    elif quality_signals.get("effective_verdict") == REVIEW_VERDICT_APPROVED:
        status = REVIEW_STATUS_APPROVED
    else:
        status = REVIEW_STATUS_NEEDS_REVISION

    return ReviewAssessment(
        reviewer=reviewer,
        artifact_path=artifact_path,
        round=round_num,
        verdict=quality_signals.get("effective_verdict") if status in (
            REVIEW_STATUS_APPROVED,
            REVIEW_STATUS_NEEDS_REVISION,
        ) else verdict,
        raw_verdict=verdict,
        effective_verdict=quality_signals.get("effective_verdict"),
        status=status,
        valid=status in (REVIEW_STATUS_APPROVED, REVIEW_STATUS_NEEDS_REVISION),
        missing_sections=missing_sections,
        malformed_reasons=malformed_reasons,
        stale_reasons=stale_reasons,
        referenced_output_hashes=referenced_hashes,
        expected_output_hashes=output_hashes,
        verification_evidence_referenced=verification_referenced,
        session_marker=session_marker,
        session_marker_present=marker_present,
        partial_output_required=partial_output_required,
        partial_output_acknowledged=partial_ack,
        unresolved_issues=unresolved_record[:500],
        quality_mode=quality_signals.get("quality_mode", DEFAULT_QUALITY_MODE),
        quality_status=(
            quality_signals.get("quality_status", status)
            if status in (REVIEW_STATUS_APPROVED, REVIEW_STATUS_NEEDS_REVISION)
            else status
        ),
        quality_blocking_reasons=list(quality_signals.get("quality_blocking_reasons") or []),
        detected_severities=list(quality_signals.get("detected_severities") or []),
        material_issue_severities=list(quality_signals.get("material_issue_severities") or []),
        material_issue_count=int(quality_signals.get("material_issue_count") or 0),
        non_material_rationale_present=bool(quality_signals.get("non_material_rationale_present")),
        accepted_risk_rationale_present=bool(quality_signals.get("accepted_risk_rationale_present")),
        actionable_unresolved_present=bool(quality_signals.get("actionable_unresolved_present")),
        parsed_issues=parsed_issues,
        previous_issue_statuses=previous_issue_statuses,
        excellence_checklist=excellence_items,
        excellence_missing_items=excellence_missing,
        excellence_blocking_reasons=excellence_blockers,
    )


def parse_verdict(inspector_output: str) -> str:
    tail  = inspector_output[-500:] if len(inspector_output) > 500 else inspector_output
    match = re.search(r'VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)', tail, re.IGNORECASE)
    return match.group(1).upper() if match else "NEEDS_REVISION"


def has_explicit_verdict(inspector_output: str) -> bool:
    tail = inspector_output[-500:] if len(inspector_output) > 500 else inspector_output
    return bool(re.search(r'VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)', tail, re.IGNORECASE))


def _looks_like_complete_agent_output(agent: str, stdout: str) -> bool:
    """Return True for complete review output that should enter review assessment."""
    if agent not in {"inspector", "inspector_2"}:
        return False
    return bool((stdout or "").strip() and has_explicit_verdict(stdout))


def _is_only_nonfatal_codex_stderr(stderr: str, rc: int = 1) -> bool:
    """Return True when stderr contains only known post-output Codex warnings."""
    if not (stderr or "").strip():
        return True
    if _matches_context_limit_error(stderr) or _matches_rate_limit_error(stderr) or _is_network_error(stderr, 1):
        return False
    error_lines = _stderr_error_lines(stderr)
    if not error_lines.strip():
        return rc == 0
    for line in error_lines.splitlines():
        lower = line.lower()
        model_refresh = (
            "failed to refresh available models" in lower
            and "timeout waiting for child process to exit" in lower
        )
        rollout_bookkeeping = (
            "failed to record rollout items" in lower
            and "thread" in lower
            and "not found" in lower
        )
        if not (model_refresh or rollout_bookkeeping):
            return False
    return True


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
                 codex_bin_resolved: str | None = None, working_dir: str | None = None,
                 verification_mode: str = DEFAULT_VERIFICATION_MODE,
                 max_verification_timeout: int = DEFAULT_MAX_VERIFICATION_TIMEOUT,
                 second_inspector_mode: str = DEFAULT_SECOND_INSPECTOR_MODE,
                 log_prompts_mode: str = DEFAULT_LOG_PROMPTS_MODE,
                 quality_mode: str = DEFAULT_QUALITY_MODE,
                 result_file: str = CONGRESS_RESULT_FILE,
                 strict_exit_codes: bool = True,
                 ci_mode: bool = True,
                 ci_mode_explicit: bool = False,
                 interactive_requested: bool | None = None):
        self.max_rounds  = max_rounds
        self.codex_bin   = codex_bin_resolved or _resolve_codex_binary(codex_bin)
        self.working_dir = working_dir or os.getcwd()
        if verification_mode not in VERIFICATION_MODES:
            raise ValueError(f"Invalid verification mode: {verification_mode}")
        if second_inspector_mode not in SECOND_INSPECTOR_MODES:
            raise ValueError(f"Invalid second inspector mode: {second_inspector_mode}")
        if log_prompts_mode not in LOG_PROMPTS_MODES:
            raise ValueError(f"Invalid prompt log mode: {log_prompts_mode}")
        self.quality_mode = _normalize_quality_mode(quality_mode)
        self.verification_mode = verification_mode
        self.max_verification_timeout = max(
            MIN_MAX_VERIFICATION_TIMEOUT,
            int(max_verification_timeout or DEFAULT_MAX_VERIFICATION_TIMEOUT),
        )
        self.second_inspector_mode = second_inspector_mode
        self.log_prompts_mode = log_prompts_mode
        self.result_file = _normalize_result_file_path(self.working_dir, result_file)
        self.strict_exit_codes = bool(strict_exit_codes)
        self.ci_mode = bool(ci_mode)
        self.ci_mode_explicit = bool(ci_mode_explicit)
        self.interactive_requested = (
            bool(interactive_requested)
            if interactive_requested is not None
            else bool(self.ci_mode_explicit and not self.ci_mode)
        )
        self.ui          = TerminalUI()
        self.session_id  = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.logger      = CongressLogger(self.session_id, prompt_log_mode=self.log_prompts_mode)

        self.round_history: list[dict] = []
        self.source_files: list[str]   = []
        self.required_source_files: list[str] = []
        self.output_files: list[str]   = []
        self._interactive              = False if self.ci_mode else sys.stdin.isatty()
        self._interrupt_requested      = False
        self.researcher_session_id: str | None = None
        self.inspector_session_id:  str | None = None
        self.inspector_2_session_id: str | None = None
        self.last_result: CongressResult | None = None
        self._workspace_lock_metadata: dict | None = None
        self._workspace_lock_token: str | None = None
        self._recovery_events: list[dict] = []
        self.retry_state: dict | None = None
        self._active_user_query: str = ""
        self._active_round: int = 1
        self.user_updates: list[dict] = []
        self.source_manifest: list[dict] = []
        self.capability_inventory: dict = {}
        self.preflight_requirements: list[dict] = []
        self.preflight_state: dict = {}
        self.blocked_state: dict | None = None
        self.task_classification: TaskClassification | None = None
        self.verification_checks: list[VerificationCheck] = []
        self.issue_records: list[IssueRecord] = []
        self.verification_summary: dict = {
            "status": "not_started",
            "mode": self.verification_mode,
            "required": False,
            "total_checks": 0,
            "note": "Verification has not run yet.",
        }
        self.review_assessments: list[ReviewAssessment] = []
        self.review_summary: dict = _review_assessments_to_summary(
            self.review_assessments,
            quality_mode=self.quality_mode,
            issue_records=self.issue_records,
        )
        self.final_approval_assessment: FinalApprovalAssessment | None = None
        self._last_post_change_outputs: list[dict] = []
        self._final_verification_output_signature: tuple[tuple[str, bool, bool, str], ...] | None = None

        if not self.codex_bin:
            self.ui.error("Codex CLI not found! Install it or pass --codex-bin=<path>")
            sys.exit(1)

    # ──────────────────────────────────────────────────────────────────────
    # State management
    # ──────────────────────────────────────────────────────────────────────

    def _save_state_now(self, user_query: str, current_round: int, substep: str):
        """Persist current progress to congress_state.json."""
        state = _build_state_v3(
            working_dir=self.working_dir,
            session_id=self.session_id,
            user_query=user_query,
            output_files=self.output_files,
            source_files=self.source_files,
            required_source_files=self.required_source_files,
            source_manifest=self.source_manifest,
            max_rounds=self.max_rounds,
            current_round=current_round,
            current_substep=substep,
            status=STATUS_RUNNING,
            researcher_session_id=self.researcher_session_id,
            inspector_session_id=self.inspector_session_id,
            inspector_2_session_id=self.inspector_2_session_id,
            user_updates=self.user_updates,
            blocked_state=self.blocked_state,
            preflight_state=self.preflight_state,
            verification_mode=self.verification_mode,
            max_verification_timeout=self.max_verification_timeout,
            second_inspector_mode=self.second_inspector_mode,
            log_prompts_mode=self.log_prompts_mode,
            quality_mode=self.quality_mode,
            result_file=self.result_file,
            strict_exit_codes=self.strict_exit_codes,
            ci_mode=self.ci_mode,
            ci_mode_explicit=self.ci_mode_explicit,
            interactive_requested=self.interactive_requested,
            retry_state=_redact_sensitive_data(self.retry_state),
            task_classification=_task_classification_to_dict(self.task_classification),
            verification_state=_redact_sensitive_data(self.verification_summary),
            verification_checks=[
                _redact_sensitive_data(_verification_check_to_dict(check))
                for check in self.verification_checks
            ],
            review_state=_redact_sensitive_data(self.review_summary),
            issue_state=_redact_sensitive_data(_issue_records_to_state(self.issue_records)),
        )
        _save_state_file(self.working_dir, state)

    def _restore_resume_context_from_state(
        self,
        saved_state: dict | None,
        user_query: str,
        start_round: int,
        initial_substep: str,
    ) -> None:
        if not saved_state or not initial_substep:
            return
        if saved_state.get("original_request_hash") and saved_state.get("original_request_hash") != _hash_text(user_query):
            self.logger.log_master("SYSTEM", "Resume context restore skipped: original request hash changed.")
            return
        if _safe_int(saved_state.get("current_round"), start_round) != start_round:
            self.logger.log_master("SYSTEM", "Resume context restore skipped: saved round does not match.")
            return
        if str(saved_state.get("current_substep") or "") != initial_substep:
            self.logger.log_master("SYSTEM", "Resume context restore skipped: saved substep does not match.")
            return

        output_keys = {_relpath_key(item) for item in self.output_files}
        saved_output_keys = {_relpath_key(item) for item in (saved_state.get("output_files") or [])}
        if saved_output_keys and saved_output_keys != output_keys:
            self.logger.log_master("SYSTEM", "Resume context restore skipped: output contract changed.")
            return

        verification_state = saved_state.get("verification_state")
        if isinstance(verification_state, dict) and verification_state:
            self.verification_summary = dict(verification_state)

        saved_checks = []
        for item in saved_state.get("verification_checks") or []:
            check = _verification_check_from_dict(item)
            if check is not None:
                saved_checks.append(check)
        if saved_checks:
            self.verification_checks = saved_checks

        review_state = saved_state.get("review_state")
        if isinstance(review_state, dict) and review_state:
            assessments = [
                dict(item)
                for item in (review_state.get("assessments") or [])
                if isinstance(item, dict)
            ]
            if assessments:
                self.review_assessments = assessments
                self.review_summary = _review_assessments_to_summary(
                    self.review_assessments,
                    quality_mode=self.quality_mode,
                    issue_records=self.issue_records,
                )
            else:
                self.review_summary = dict(review_state)

        if not self.issue_records:
            issue_state = saved_state.get("issue_state") or (review_state or {}).get("issue_state")
            if issue_state:
                self.issue_records = _issue_records_from_state(issue_state)
                self.review_summary = _review_assessments_to_summary(
                    self.review_assessments,
                    quality_mode=self.quality_mode,
                    issue_records=self.issue_records,
                )

        current_outputs = _collect_result_output_snapshots(self.working_dir, self.output_files)
        latest_inspector_1 = _latest_review_assessment(self.review_assessments, "inspector_1")
        outputs_still_match_review = (
            bool(latest_inspector_1)
            and not _assessment_matches_current_outputs(latest_inspector_1, current_outputs)
        )
        if (
            outputs_still_match_review
            and (self.verification_summary or {}).get("latest_phase") == VERIFICATION_PHASE_FINAL
            and isinstance((self.verification_summary or {}).get("latest_phase_summary"), dict)
        ):
            self._final_verification_output_signature = self._current_output_signature()
            self.logger.log_master(
                "SYSTEM",
                "Restored final verification/review context for unchanged resumed outputs.",
            )

    def _has_current_final_verification(self) -> bool:
        return (
            (self.verification_summary or {}).get("latest_phase") == VERIFICATION_PHASE_FINAL
            and isinstance((self.verification_summary or {}).get("latest_phase_summary"), dict)
            and self._final_verification_output_signature == self._current_output_signature()
        )

    def _partial_artifact_token(self, value) -> str:
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._-")
        return (token or "unknown")[:64]

    def _write_partial_output_artifacts(self, agent: str, round_label,
                                        recovery_type: str, stdout: str = "",
                                        stderr: str = "") -> dict:
        """Persist interrupted stdout/stderr as managed recovery evidence."""
        artifacts: dict[str, str | int] = {}
        stdout_text = stdout or ""
        stderr_text = stderr or ""
        if not stdout_text and not stderr_text:
            return artifacts

        rdir = _rounds_dir(self.working_dir)
        os.makedirs(rdir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        base = (
            f"round_{self._partial_artifact_token(round_label)}"
            f"_partial_{self._partial_artifact_token(agent or 'codex')}"
            f"_{self._partial_artifact_token(recovery_type)}_{stamp}"
        )

        for stream_name, text in (("stdout", stdout_text), ("stderr", stderr_text)):
            if not text:
                continue
            filename = f"{base}.{stream_name}.txt"
            abs_path = os.path.join(rdir, filename)
            rel_path = f"{CONGRESS_ROUNDS_DIR}/{filename}"
            try:
                _atomic_write_text(abs_path, text)
            except OSError as exc:
                self.logger.log_master(
                    "SYSTEM",
                    f"Could not write partial {stream_name} artifact {rel_path}: {exc}",
                )
                continue
            artifacts[f"partial_{stream_name}_path"] = rel_path.replace("\\", "/")
            artifacts[f"partial_{stream_name}_chars"] = len(text)
        return artifacts

    def _save_retry_state(self, user_query: str | None, current_round,
                          substep: str | None, agent: str, reason: str,
                          retry_after_seconds: int | None = None,
                          session_id: str | None = None,
                          stdout: str = "", stderr: str = "",
                          output_status: list[dict] | None = None,
                          partial_paths: dict | None = None) -> None:
        """Persist inspectable retry metadata before a long wait or retry."""
        try:
            round_value = int(current_round or self._active_round or 1)
        except (TypeError, ValueError):
            round_value = self._active_round or 1
        substep_value = substep or (f"{agent}_running" if agent else "")
        artifact_paths = {
            key: value
            for key, value in (partial_paths or {}).items()
            if key in {
                "partial_stdout_path",
                "partial_stderr_path",
                "partial_stdout_chars",
                "partial_stderr_chars",
            } and value
        }
        missing_stdout_artifact = bool(stdout) and not artifact_paths.get("partial_stdout_path")
        missing_stderr_artifact = bool(stderr) and not artifact_paths.get("partial_stderr_path")
        if missing_stdout_artifact or missing_stderr_artifact:
            artifact_paths.update(
                self._write_partial_output_artifacts(
                    agent or "codex",
                    round_value,
                    reason,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        retry_state = {
            "timestamp": _utc_timestamp(),
            "agent": agent,
            "round": round_value,
            "substep": substep_value,
            "session_id": session_id,
            "reason": reason,
            "retry_after_seconds": retry_after_seconds,
            "stdout_chars": len(stdout or ""),
            "stderr_chars": len(stderr or ""),
            "stderr_excerpt": _redact_sensitive_data((stderr or "")[:500]),
            "output_status": _redact_sensitive_data(output_status or []),
        }
        retry_state.update(artifact_paths)
        if retry_after_seconds is not None:
            retry_state["next_retry_at"] = datetime.utcfromtimestamp(
                time.time() + int(retry_after_seconds)
            ).replace(microsecond=0).isoformat() + "Z"
        self.retry_state = retry_state
        self.logger.log_master(
            "SYSTEM",
            "Saved retry state "
            f"agent={agent} round={round_value} substep={substep_value} "
            f"reason={reason} retry_after={retry_after_seconds}",
        )
        try:
            self._save_state_now(
                user_query if user_query is not None else self._active_user_query,
                round_value,
                substep_value,
            )
        except Exception as exc:
            self.logger.log_master("SYSTEM", f"Could not save retry state: {exc}")

    def _write_current_session_request(self, user_query: str,
                                       audience: str = "default") -> bool:
        return _write_session_request(
            self.working_dir,
            user_query,
            self.output_files,
            user_updates=self.user_updates,
            source_manifest=self.source_manifest,
            capability_inventory=self.capability_inventory,
            task_classification=_task_classification_to_dict(self.task_classification),
            verification_summary=self.verification_summary,
            review_summary=self.review_summary,
            audience=audience,
        )

    def _record_user_update(self, text: str, source: str, user_query: str | None = None,
                            current_round: int = 1, persist: bool = True) -> dict:
        update = _sanitize_user_update(text, source)
        self.user_updates.append(update)
        self.logger.log_master(
            "SYSTEM",
            "User update recorded "
            f"source={source} redacted={str(update.get('redacted')).lower()}",
        )
        if persist and user_query is not None:
            self._write_current_session_request(user_query)
            try:
                _append_history_note(
                    self.working_dir,
                    "user_update",
                    [_format_user_update(update)],
                )
            except OSError as exc:
                self.logger.log_master("SYSTEM", f"Could not append user update history: {exc}")
            self._save_state_now(user_query, current_round, "blocked" if self.blocked_state else "")
        return update

    def _prompt_for_resume_note(self) -> str:
        if self.ci_mode or not self._interactive:
            return ""
        try:
            answer = input("Add a note for the agents before resuming? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                return ""
            return input("Note for agents: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _acquire_workspace_lock(self) -> None:
        metadata = _acquire_workspace_lock_file(
            self.working_dir,
            self.session_id,
            STATUS_RUNNING,
        )
        self._workspace_lock_metadata = metadata
        self._workspace_lock_token = metadata.get("owner_token")
        self.logger.log_master(
            "SYSTEM",
            f"Workspace lock acquired: {CONGRESS_LOCK_FILE} ({_format_lock_owner(metadata)})",
        )
        for reason in metadata.get("replaced_stale_lock_reasons") or []:
            self.logger.log_master("SYSTEM", f"Replaced stale workspace lock: {reason}")

    def _release_workspace_lock(self) -> None:
        if not self._workspace_lock_metadata:
            return
        released = _release_workspace_lock_file(
            self.working_dir,
            self.session_id,
            self._workspace_lock_token,
        )
        if released:
            self.logger.log_master("SYSTEM", f"Workspace lock released: {CONGRESS_LOCK_FILE}")
        else:
            self.logger.log_master(
                "SYSTEM",
                f"Workspace lock not released because it is no longer owned by this session: {CONGRESS_LOCK_FILE}",
            )
        self._workspace_lock_metadata = None
        self._workspace_lock_token = None

    # ──────────────────────────────────────────────────────────────────────
    # Prompt building
    # ──────────────────────────────────────────────────────────────────────

    def _source_file_section(self) -> str:
        required_files = list(self.required_source_files or self.source_files)
        if not required_files:
            return ""
        file_list = "\n".join(
            f"  - {_relpath_from_workspace(self.working_dir, f)}"
            for f in required_files
        )
        return (
            f"\n{'=' * 60}\n"
            f"REQUIRED SOURCE/INPUT FILES\n"
            f"{'=' * 60}\n"
            f"Read these Tier 1 files from disk because the user explicitly named them or they were provided as direct task inputs.\n"
            f"They are required context; broad discovered files are listed separately as optional source context.\n"
            f"{file_list}\n"
        )

    def _optional_source_context_prompt_section(self) -> str:
        if not self.source_manifest:
            return ""
        grouped = _source_manifest_items_by_tier(self.source_manifest)
        tier2 = len(grouped.get(SOURCE_CONTEXT_TIER_PROJECT, []))
        tier3 = len(grouped.get(SOURCE_CONTEXT_TIER_OPTIONAL, []))
        tier4 = len(grouped.get(SOURCE_CONTEXT_TIER_EXCLUDED, []))
        return (
            f"\n{'=' * 60}\n"
            f"OPTIONAL SOURCE CONTEXT MANIFEST\n"
            f"{'=' * 60}\n"
            f"Congress wrote tiered source context to {SESSION_REQUEST_FILE}: "
            f"Tier 2 optional project context={tier2}, Tier 3 optional discoverable files={tier3}, "
            f"Tier 4 excluded-by-default summaries={tier4}.\n"
            f"Use optional source manifest entries to inspect files needed for a complete and trustworthy answer. "
            f"Do not mechanically read unrelated optional files.\n"
        )

    def _original_request_prompt_section(self, user_query: str) -> str:
        return (
            f"\n{'=' * 60}\n"
            f"ORIGINAL USER REQUEST / SOURCE OF TRUTH\n"
            f"{'=' * 60}\n"
            f"The original user prompt is the highest-priority task definition after system/developer safety rules. "
            f"Source manifests, verification summaries, previous notes, and optional context must not narrow or override the original user request. "
            f"If any context conflicts with the original user request, follow the original user request and explain the conflict.\n\n"
            f"{user_query}\n"
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

    def _output_status_section(self) -> str:
        if not self.output_files:
            return ""
        lines = []
        for item in _collect_result_output_snapshots(self.working_dir, self.output_files):
            path = item.get("path", "")
            if item.get("invalid_type"):
                status = f"INVALID ({item.get('invalid_reason') or 'not a regular file'})"
            elif item.get("exists"):
                sha = item.get("sha256") or "unavailable"
                status = f"exists, size={item.get('size')} bytes, sha256={sha}"
            else:
                status = "missing"
            lines.append(f"  - {path}: {status}")
        return (
            f"\n{'=' * 60}\n"
            f"REQUESTED OUTPUT STATUS SNAPSHOT\n"
            f"{'=' * 60}\n"
            f"Congress-owned informational snapshot before this agent runs. "
            f"Use it as context, but inspect the files from disk yourself.\n"
            + "\n".join(lines) + "\n"
        )

    def _recovery_context_section(self) -> str:
        if not self._recovery_events:
            return ""
        lines = []
        for event in self._recovery_events[-5:]:
            path_bits = []
            if event.get("partial_stdout_path"):
                path_bits.append(f"stdout_path={event.get('partial_stdout_path')}")
            if event.get("partial_stderr_path"):
                path_bits.append(f"stderr_path={event.get('partial_stderr_path')}")
            path_suffix = (" " + " ".join(path_bits)) if path_bits else ""
            lines.append(
                "  - "
                f"round={event.get('round')} agent={event.get('agent')} "
                f"type={event.get('type')} stdout_chars={event.get('stdout_chars')} "
                f"reason={event.get('reason')}{path_suffix}"
            )
        return (
            f"\n{'=' * 60}\n"
            f"RECOVERY NOTES FROM PRIOR AGENT RUNS\n"
            f"{'=' * 60}\n"
            f"partial_output_recovered=true\n"
            f"Congress saved partial output during a recovery path. "
            f"Treat it as evidence only, not completion, and verify from disk.\n"
            + "\n".join(lines) + "\n"
        )

    def _record_partial_recovery(self, agent: str, round_label, recovery_type: str,
                                 reason: str, stdout_chars: int,
                                 stdout: str = "", stderr: str = "",
                                 partial_paths: dict | None = None) -> dict:
        artifact_paths = {
            key: value
            for key, value in (partial_paths or {}).items()
            if key in {
                "partial_stdout_path",
                "partial_stderr_path",
                "partial_stdout_chars",
                "partial_stderr_chars",
            } and value
        }
        if (stdout or stderr) and not artifact_paths:
            artifact_paths.update(
                self._write_partial_output_artifacts(
                    agent,
                    round_label,
                    recovery_type,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        event = {
            "agent": agent,
            "round": round_label,
            "type": recovery_type,
            "reason": reason,
            "stdout_chars": stdout_chars,
            "stderr_chars": len(stderr or ""),
            "timestamp": _utc_timestamp(),
        }
        event.update(artifact_paths)
        self._recovery_events.append(event)
        path_summary = ""
        if event.get("partial_stdout_path") or event.get("partial_stderr_path"):
            path_summary = (
                f" stdout_path={event.get('partial_stdout_path', '')}"
                f" stderr_path={event.get('partial_stderr_path', '')}"
            )
        self.logger.log_master(
            "SYSTEM",
            "partial_output_recovered "
            f"agent={agent} round={round_label} type={recovery_type} "
            f"stdout_chars={stdout_chars} reason={reason}{path_summary}",
        )
        return event

    def _session_request_section(self) -> str:
        return (
            f"\n{'=' * 60}\n"
            f"SESSION REQUEST FILE\n"
            f"{'=' * 60}\n"
            f"Read this file every round before doing work:\n"
            f"  {os.path.join(self.working_dir, SESSION_REQUEST_FILE)}\n"
        )

    def _user_updates_prompt_section(self) -> str:
        if not self.user_updates:
            return ""
        return (
            f"\n{'=' * 60}\n"
            f"USER UPDATES - READ BEFORE ACTING\n"
            f"{'=' * 60}\n"
            f"The managed session request file contains a `## User Updates` section. "
            f"Read it before acting; it contains sanitized resume or pause comments "
            f"that supersede earlier assumptions when they conflict.\n"
        )

    def _capability_prompt_section(self) -> str:
        if not self.capability_inventory:
            return ""
        return (
            f"\n{'=' * 60}\n"
            f"CAPABILITY AND PREFLIGHT CONTEXT\n"
            f"{'=' * 60}\n"
            f"Congress recorded a redacted capability/preflight inventory in "
            f"{SESSION_REQUEST_FILE}. Treat it as informational context, not as a "
            f"restriction on using real commands or tools needed for the task.\n"
        )

    def _verification_prompt_section(self) -> str:
        if not self.task_classification and not self.verification_checks:
            return ""
        classification = _task_classification_to_dict(self.task_classification)
        classes = ", ".join(classification.get("classes") or []) or "not_classified"
        class_list = classification.get("classes") or []
        required_evidence = ", ".join(classification.get("required_evidence") or []) or "none"
        research_requirement = ""
        if TASK_CLASS_RESEARCH_CURRENT_INFO in class_list:
            research_requirement = f"Research evidence requirement: {RESEARCH_EVIDENCE_REQUIREMENT_TEXT}\n"
        current_summary = _verification_summary_for_current_context(self.verification_summary)
        status = current_summary.get("status", "not_started")
        total = current_summary.get("total_checks", 0)
        cumulative_note = ""
        if current_summary.get("cumulative_status") and current_summary.get("cumulative_status") != status:
            cumulative_note = (
                f"Cumulative historical verification status: {current_summary.get('cumulative_status')} "
                f"({current_summary.get('cumulative_total_checks', 0)} retained records). "
                "Use the latest phase status for the current requested outputs.\n"
            )
        return (
            f"\n{'=' * 60}\n"
            f"CONGRESS-OWNED VERIFICATION CONTEXT\n"
            f"{'=' * 60}\n"
            f"Task classes: {classes}\n"
            f"Required evidence: {required_evidence}\n"
            f"{research_requirement}"
            f"Verification mode: {self.verification_mode}\n"
            f"Latest verification status: {status} ({total} check records)\n"
            f"{cumulative_note}"
            f"Read {os.path.join(self.working_dir, CONGRESS_VERIFICATION_FILE)} if it exists. "
            f"This evidence does not replace your responsibility to inspect files and run real commands when needed. "
            f"Mechanical verification passing does not mean the deliverable is excellent; it is not quality approval. "
            f"You must still judge content quality, research depth, correctness, completeness, usefulness, source trust, "
            f"and fit to the original request.\n"
        )

    def _issue_lifecycle_prompt_section(self, *, audience: str = "default") -> str:
        return _issue_prompt_summary(self.issue_records, audience=audience)

    def _review_context_marker(self, round_num: int) -> str:
        return _review_context_marker(self.user_updates, self.verification_summary, round_num)

    def _review_prompt_section(self, reviewer_label: str, round_num: int) -> str:
        hashes = _current_output_hashes(self.working_dir, self.output_files)
        hash_lines = []
        for item in hashes:
            state = "exists" if item.get("exists") else "missing"
            if item.get("invalid_type"):
                state = "invalid"
            hash_lines.append(
                f"  - {item.get('path')}: {state}, sha256={item.get('sha256') or 'unavailable'}"
            )
        marker = self._review_context_marker(round_num)
        section_names = [
            "Findings",
            "Evidence Reviewed",
            "Commands/Tests Run",
            "Output Hashes",
            "Verification Evidence",
            "Waived Checks",
        ]
        if self.issue_records:
            section_names.append("Previous Issues")
        section_names.extend([
            "Excellence Checklist",
            "Unresolved Issues",
            "VERDICT: APPROVED or VERDICT: NEEDS_REVISION",
        ])
        partial_note = ""
        if self._recovery_events:
            partial_note = (
                "Because partial_output_recovered=true is present, your review must explicitly "
                "acknowledge partial output recovery before approving.\n"
            )
        return (
            f"\n{'=' * 60}\n"
            f"REQUIRED MARKDOWN REVIEW CONTRACT ({reviewer_label})\n"
            f"{'=' * 60}\n"
            "Your stdout is a review artifact. Write readable Markdown, not JSON or JSONL.\n"
            "Congress will validate your review before accepting the verdict.\n"
            f"Congress quality mode: {self.quality_mode}\n"
            "In best-output mode, APPROVED is only acceptable when no material unresolved issue remains.\n"
            "Use NEEDS_REVISION for CRITICAL, HIGH, or MEDIUM material issues and for any actionable issue that would materially improve the requested output.\n"
            "LOW issues may remain only with concrete non-material rationale. Accepted-risk approval requires concrete blocker/risk rationale.\n"
            "Mechanical verification passing does not mean the deliverable is excellent, and it is not quality approval. You must still judge content quality, research depth, correctness, completeness, usefulness, source trust, and fit to the original request.\n"
            "If Congress lists previous tracked issues, include `## Previous Issues` and mark each as RESOLVED, STILL PRESENT, PARTIALLY RESOLVED, ACCEPTED_RISK, or NON_MATERIAL with concrete evidence.\n"
            "Include `## Excellence Checklist` with each item marked PASS, FAIL, WAIVED, or UNKNOWN plus evidence or blocker rationale. Use this exact form for every item: `- Original request fully answered: PASS - Evidence: ...`. Missing, unsupported PASS, FAIL, or UNKNOWN material checklist items block final approval.\n"
            "Excellence checklist items: Original request fully answered; Deliverable directly usable; Material issues resolved; Claims sourced or assumptions labeled; Implementation details present; Edge cases and failure modes covered; Verification/test strategy specific; Internally consistent; No raw secrets included; Best practical version.\n"
            "Required sections/headings:\n"
            + "\n".join(f"  - {name}" for name in section_names) + "\n"
            "The final non-empty unquoted line must be exactly one verdict line.\n"
            "Do not place the real verdict inside a code block, quote, example, or paragraph.\n"
            f"Review freshness marker: {marker}\n"
            "Copy the freshness marker into your Evidence Reviewed or Verification Evidence section.\n"
            f"Verification evidence file: {CONGRESS_VERIFICATION_FILE}\n"
            "Current requested-output hashes you must reference:\n"
            + ("\n".join(hash_lines) if hash_lines else "  - (none)") + "\n"
            f"{partial_note}"
        )

    def _review_summary_for_state(self) -> dict:
        self.review_summary = _review_assessments_to_summary(
            self.review_assessments,
            quality_mode=self.quality_mode,
            issue_records=self.issue_records,
        )
        if self.final_approval_assessment is not None:
            self.review_summary = dict(self.review_summary)
            gate = _final_approval_assessment_to_dict(self.final_approval_assessment)
            self.review_summary["final_gate"] = gate
            self.review_summary["final_approval_status"] = (
                self.final_approval_assessment.status
                if self.final_approval_assessment.approved
                else None
            )
            if gate.get("waivers"):
                self.review_summary["waivers"] = gate.get("waivers")
        return self.review_summary

    def _merge_tracked_issue(self, issue: IssueRecord) -> IssueRecord:
        index = _find_matching_issue_index(self.issue_records, issue)
        if index is None:
            self.issue_records.append(issue)
            return issue
        merged = _merge_issue_record(self.issue_records[index], issue)
        self.issue_records[index] = merged
        return merged

    def _apply_issue_status_record(self, record: dict, *, source: str) -> None:
        title = str(record.get("title") or "")
        location = str(record.get("location") or "")
        status = str(record.get("status") or ISSUE_STATUS_OPEN)
        rationale = str(record.get("rationale") or "")
        reviewer = str(record.get("reviewer") or source or "")
        probe_issue = _make_issue_record(
            title=title,
            severity="MEDIUM",
            reviewer=reviewer,
            location=location,
            round_num=record.get("round"),
            status=status,
            rationale=rationale,
            source_artifact=str(record.get("source_artifact") or ""),
        )
        record_id = str(record.get("id") or "").strip().lower()
        if re.fullmatch(r"issue-[a-f0-9]{8,32}", record_id):
            probe_issue.id = record_id
        index = _find_matching_issue_index(self.issue_records, probe_issue)
        if index is None:
            if source == "researcher":
                self.issue_records.append(probe_issue)
            return
        issue = self.issue_records[index]
        previous_status = issue.status
        previous_rationale = issue.rationale
        if reviewer and reviewer not in issue.reviewers and source != "researcher":
            issue.reviewers.append(reviewer)
        if location and not issue.location:
            issue.location = location
        if rationale:
            issue.rationale = rationale
        issue.last_seen_round = record.get("round") or issue.last_seen_round
        if source == "researcher":
            if status in {
                ISSUE_STATUS_ADDRESSED,
                ISSUE_STATUS_PARTIALLY_ADDRESSED,
                ISSUE_STATUS_CLAIMED_ACCEPTED_RISK,
                ISSUE_STATUS_CLAIMED_NON_MATERIAL,
                ISSUE_STATUS_OPEN,
            }:
                issue.status = status
            return
        if status == ISSUE_STATUS_PARTIALLY_ADDRESSED:
            issue.status = ISSUE_STATUS_PARTIALLY_ADDRESSED
            issue.resolution_round = None
        elif status == ISSUE_STATUS_OPEN:
            issue.status = ISSUE_STATUS_OPEN
            issue.resolution_round = None
        elif status in ISSUE_CLOSED_STATUSES:
            if issue.materiality == "material" and status in (ISSUE_STATUS_ACCEPTED_RISK, ISSUE_STATUS_NON_MATERIAL):
                if not _issue_status_has_valid_rationale(status, rationale, issue.title):
                    if previous_status in ISSUE_CLOSED_STATUSES:
                        issue.status = previous_status
                        issue.rationale = previous_rationale
                    else:
                        issue.status = ISSUE_STATUS_OPEN
                    return
            issue.status = status
            issue.resolution_round = record.get("round")

    def _record_researcher_issue_claims(self, researcher_output: str, round_num: int) -> list[dict]:
        claims = _extract_issue_status_records(
            researcher_output,
            section_aliases=("inspector issues addressed", "issues addressed", "review issues addressed"),
            source="researcher",
            reviewer="researcher",
            round_num=round_num,
            source_artifact=RESEARCHER_UPDATED_FILE,
        )
        for claim in claims:
            self._apply_issue_status_record(claim, source="researcher")
        if claims:
            self.review_summary = self._review_summary_for_state()
            self.logger.log_master("SYSTEM", f"Recorded {len(claims)} researcher issue-resolution claim(s).")
        return claims

    def _issue_lifecycle_blockers(self) -> list[str]:
        return _issue_lifecycle_blockers_from_records(self.issue_records)

    def _record_review_assessment(self, assessment: ReviewAssessment) -> ReviewAssessment:
        for status_record in assessment.previous_issue_statuses:
            self._apply_issue_status_record(status_record, source="inspector")
        for issue in assessment.parsed_issues:
            self._merge_tracked_issue(issue)
        self.review_assessments.append(assessment)
        self.review_summary = self._review_summary_for_state()
        self.logger.log_master(
            "SYSTEM",
            f"Review assessment {assessment.reviewer}: status={assessment.status} "
            f"raw_verdict={assessment.raw_verdict} effective_verdict={assessment.effective_verdict} "
            f"quality_blockers={len(assessment.quality_blocking_reasons)} "
            f"issues={len(assessment.parsed_issues)} excellence_blockers={len(assessment.excellence_blocking_reasons)}",
        )
        return assessment

    def _assess_review_output(self, reviewer: str, review_text: str,
                              artifact_path: str, round_num: int) -> ReviewAssessment:
        return assess_markdown_review(
            review_text,
            reviewer=reviewer,
            artifact_path=artifact_path,
            round_num=round_num,
            working_dir=self.working_dir,
            output_files=self.output_files,
            verification_summary=self.verification_summary,
            user_updates=self.user_updates,
            partial_output_required=bool(self._recovery_events),
            quality_mode=self.quality_mode,
        )

    def _classify_task(self, user_query: str, changed_files: list[str] | None = None) -> TaskClassification:
        self.task_classification = _classify_congress_task(
            user_query,
            self.output_files,
            source_manifest=self.source_manifest,
            working_dir=self.working_dir,
            preflight_requirements=self.preflight_requirements,
            preflight_state=self.preflight_state,
            changed_files=changed_files,
            user_updates=self.user_updates,
        )
        self.logger.log_master(
            "SYSTEM",
            "Task classification: "
            + ", ".join(self.task_classification.classes)
            + f" confidence={self.task_classification.confidence}",
        )
        return self.task_classification

    def _run_verification_phase(self, user_query: str, phase: str,
                                changed_files: list[str] | None = None) -> dict:
        if phase != VERIFICATION_PHASE_FINAL:
            self._final_verification_output_signature = None
        if self.task_classification is None:
            self._classify_task(user_query, changed_files=changed_files)
        selected = _select_verification_checks(
            self.working_dir,
            self.task_classification,
            phase,
            output_files=self.output_files,
            source_manifest=self.source_manifest,
            preflight_state=self.preflight_state,
            capability_inventory=self.capability_inventory,
            verification_mode=self.verification_mode,
            max_timeout_seconds=self.max_verification_timeout,
            changed_files=changed_files,
        )
        phase_start = time.monotonic()
        executed: list[VerificationCheck] = []
        for check in selected:
            elapsed = time.monotonic() - phase_start
            remaining = self.max_verification_timeout - elapsed
            if remaining <= 0 and check.check_type == "command":
                check.status = VERIFICATION_STATUS_BLOCKED
                check.blocked_reason = (
                    f"Verification phase exceeded max timeout of {self.max_verification_timeout}s."
                )
                check.reason += " Skipped because total verification timeout was exhausted."
                check.timestamp = _utc_timestamp()
                executed.append(check)
                continue
            if check.check_type == "command":
                executed.append(_run_command_verification_check(check, remaining_timeout=remaining))
            else:
                executed.append(_run_non_command_verification_check(
                    check,
                    working_dir=self.working_dir,
                    output_files=self.output_files,
                    source_manifest=self.source_manifest,
                    preflight_state=self.preflight_state,
                    capability_inventory=self.capability_inventory,
                ))
        self.verification_checks.extend(executed)
        self.verification_summary = _verification_summary_from_checks(
            self.verification_checks,
            phase=None,
            verification_mode=self.verification_mode,
        )
        phase_summary = _verification_summary_from_checks(
            executed,
            phase=phase,
            verification_mode=self.verification_mode,
        )
        self.verification_summary["latest_phase"] = phase
        self.verification_summary["latest_phase_status"] = phase_summary.get("status")
        self.verification_summary["latest_phase_summary"] = phase_summary
        try:
            _write_verification_markdown(
                self.working_dir,
                classification=self.task_classification,
                checks=self.verification_checks,
                summary=self.verification_summary,
                verification_mode=self.verification_mode,
                max_timeout_seconds=self.max_verification_timeout,
            )
        except OSError as exc:
            self.logger.log_master("SYSTEM", f"Could not write {CONGRESS_VERIFICATION_FILE}: {exc}")
        self.logger.log_master(
            "SYSTEM",
            f"Verification {phase}: status={phase_summary.get('status')} checks={phase_summary.get('total_checks')}",
        )
        if phase == VERIFICATION_PHASE_FINAL:
            self._final_verification_output_signature = self._current_output_signature()
        return phase_summary

    def _current_output_signature(self) -> tuple[tuple[str, bool, bool, str], ...]:
        return tuple(
            (
                str(item.get("path") or ""),
                bool(item.get("exists")),
                bool(item.get("invalid_type")),
                str(item.get("sha256") or ""),
            )
            for item in _collect_result_output_snapshots(self.working_dir, self.output_files)
        )

    def _ensure_final_verification_phase(
        self,
        user_query: str,
        changed_files: list[str] | None = None,
    ) -> dict:
        current_signature = self._current_output_signature()
        if (
            (self.verification_summary or {}).get("latest_phase") == VERIFICATION_PHASE_FINAL
            and self._final_verification_output_signature == current_signature
        ):
            latest = (self.verification_summary or {}).get("latest_phase_summary")
            if isinstance(latest, dict):
                self.logger.log_master(
                    "SYSTEM",
                    "Reusing current final verification summary for unchanged requested outputs.",
                )
                return latest
        return self._run_verification_phase(
            user_query,
            VERIFICATION_PHASE_FINAL,
            changed_files=changed_files,
        )

    def _verification_terminal_blocker(self, phase_summary: dict) -> tuple[str | None, list[str], dict | None]:
        status = phase_summary.get("status")
        if self.verification_mode == "off":
            return None, [], None
        if status == VERIFICATION_STATUS_BLOCKED:
            blocked_checks = [
                check for check in self.verification_checks
                if check.status == VERIFICATION_STATUS_BLOCKED
                and (check.phase == phase_summary.get("phase") or phase_summary.get("phase") is None)
            ]
            blocked = _verification_blocked_state_from_summary(
                {"status": VERIFICATION_STATUS_BLOCKED},
                blocked_checks or self.verification_checks,
            )
            if blocked:
                return blocked.get("status", STATUS_BLOCKED_NEEDS_ENVIRONMENT), [blocked.get("reason", "Verification blocked.")], blocked
            return STATUS_BLOCKED_NEEDS_ENVIRONMENT, ["Verification is blocked by a required dependency."], None
        if status == VERIFICATION_STATUS_FAILED:
            failed_ids = phase_summary.get("required_failed_ids") or []
            reason = (
                "Required verification failed"
                + (": " + ", ".join(str(item) for item in failed_ids) if failed_ids else ".")
            )
            return STATUS_VERIFICATION_FAILED, [reason], None
        if status in (VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_MIXED, "not_started", None):
            return STATUS_VERIFICATION_FAILED, [
                f"Final verification status is not approval-ready: {status or 'not_started'}."
            ], None
        return None, [], None

    def _final_gate_should_continue(self, gate: FinalApprovalAssessment, round_num: int) -> bool:
        if gate.approved or round_num >= self.max_rounds:
            return False
        retryable_terms = (
            "Unresolved material issue",
            "Issue `",
            "Excellence checklist blocker",
            "claimed-but-unverified",
        )
        return any(
            any(term in str(reason) for term in retryable_terms)
            for reason in gate.blocking_reasons
        )

    def _build_final_approval_assessment(
        self,
        intended_status: str = STATUS_APPROVED,
        allow_second_inspector_waiver: bool = False,
        waiver_reason: str | None = None,
    ) -> FinalApprovalAssessment:
        output_snapshots = _collect_result_output_snapshots(self.working_dir, self.output_files)
        blockers: list[str] = []
        status = intended_status if intended_status in APPROVED_STATUSES else STATUS_FAILED_NONRESUMABLE
        waivers: list[str] = []

        for item in output_snapshots:
            if item.get("invalid_type"):
                blockers.append(
                    f"Requested output `{item.get('path')}` is not a regular file: "
                    f"{item.get('invalid_reason') or 'invalid output type'}."
                )
                status = STATUS_FAILED_NONRESUMABLE
            elif not item.get("exists"):
                blockers.append(f"Requested output `{item.get('path')}` is missing.")
                status = STATUS_FAILED_NONRESUMABLE
            elif not item.get("sha256"):
                blockers.append(f"Requested output `{item.get('path')}` has no current hash.")
                status = STATUS_FAILED_NONRESUMABLE

        if self.blocked_state:
            blocked_status = self.blocked_state.get("status") or STATUS_BLOCKED_NEEDS_ENVIRONMENT
            status = blocked_status if blocked_status in CONTROLLED_STATUSES else STATUS_BLOCKED_NEEDS_ENVIRONMENT
            blockers.append(
                "Active blocked state remains: "
                + str(self.blocked_state.get("reason", "No reason recorded."))
            )

        verification_summary_for_gate = self.verification_summary or {"status": "not_started"}
        latest_phase_summary = verification_summary_for_gate.get("latest_phase_summary")
        if (
            verification_summary_for_gate.get("latest_phase") == VERIFICATION_PHASE_FINAL
            and isinstance(latest_phase_summary, dict)
        ):
            verification_summary_for_gate = latest_phase_summary
        verification_status = verification_summary_for_gate.get("status", "not_started")
        if (
            self.verification_mode != "off"
            and (self.verification_summary or {}).get("latest_phase") != VERIFICATION_PHASE_FINAL
        ):
            if status in APPROVED_STATUSES:
                status = STATUS_VERIFICATION_FAILED
            blockers.append("Final verification phase has not run for the current requested outputs.")
        elif self.verification_mode != "off":
            current_signature = self._current_output_signature()
            if self._final_verification_output_signature != current_signature:
                if status in APPROVED_STATUSES:
                    status = STATUS_VERIFICATION_FAILED
                blockers.append("Final verification evidence is stale for the current requested outputs.")
        verification_terminal, verification_reasons, verification_blocked = self._verification_terminal_blocker(
            verification_summary_for_gate
        )
        if verification_terminal:
            if status in APPROVED_STATUSES:
                status = verification_terminal
            blockers.extend(verification_reasons)
            if verification_blocked:
                self.blocked_state = verification_blocked
        elif self.verification_mode != "off" and verification_status not in (
            VERIFICATION_STATUS_PASSED,
            VERIFICATION_STATUS_WAIVED,
            VERIFICATION_STATUS_NOT_APPLICABLE,
        ):
            if status in APPROVED_STATUSES:
                status = STATUS_VERIFICATION_FAILED
            blockers.append(f"Final verification status is not approval-ready: {verification_status}.")

        research_evidence_blocker = _research_evidence_final_gate_blocker(
            self.task_classification,
            self.verification_checks,
            self.verification_mode,
        )
        if research_evidence_blocker:
            if status in APPROVED_STATUSES:
                status = STATUS_VERIFICATION_FAILED
            if research_evidence_blocker not in blockers:
                blockers.append(research_evidence_blocker)

        inspector_1 = _latest_review_assessment(self.review_assessments, "inspector_1")
        inspector_2 = _latest_review_assessment(self.review_assessments, "inspector_2")
        inspector_1_status = inspector_1.get("status") if inspector_1 else None
        inspector_2_status = inspector_2.get("status") if inspector_2 else None

        if not inspector_1:
            if status in APPROVED_STATUSES:
                status = STATUS_REVIEW_FAILED
            blockers.append("Inspector 1 approval assessment is missing.")
        elif inspector_1.get("status") != REVIEW_STATUS_APPROVED or inspector_1.get("verdict") != REVIEW_VERDICT_APPROVED:
            if status in APPROVED_STATUSES:
                status = STATUS_REVIEW_FAILED
            blockers.append("Inspector 1 review is not current and approved.")
            for reason in inspector_1.get("quality_blocking_reasons") or []:
                blockers.append(f"Inspector 1 quality policy blocker: {reason}")
        else:
            stale = _assessment_matches_current_outputs(inspector_1, output_snapshots)
            if stale:
                if status in APPROVED_STATUSES:
                    status = STATUS_REVIEW_FAILED
                blockers.extend(stale)
            current_marker = self._review_context_marker(int(inspector_1.get("round") or 0) or None)
            if not inspector_1.get("session_marker_present") or inspector_1.get("session_marker") != current_marker:
                if status in APPROVED_STATUSES:
                    status = STATUS_REVIEW_FAILED
                blockers.append("Inspector 1 review does not reference the current review freshness marker.")
            if self._recovery_events and not inspector_1.get("partial_output_acknowledged"):
                if status in APPROVED_STATUSES:
                    status = STATUS_REVIEW_FAILED
                blockers.append("Inspector 1 did not acknowledge partial output recovery metadata.")

        second_inspector_required = not allow_second_inspector_waiver
        if second_inspector_required:
            if not inspector_2:
                if status in APPROVED_STATUSES:
                    status = STATUS_REVIEW_FAILED
                blockers.append("Inspector 2 approval assessment is missing.")
            elif inspector_2.get("status") != REVIEW_STATUS_APPROVED or inspector_2.get("verdict") != REVIEW_VERDICT_APPROVED:
                if status in APPROVED_STATUSES:
                    status = STATUS_REVIEW_FAILED
                blockers.append("Inspector 2 review is not current and approved.")
                for reason in inspector_2.get("quality_blocking_reasons") or []:
                    blockers.append(f"Inspector 2 quality policy blocker: {reason}")
            else:
                stale = _assessment_matches_current_outputs(inspector_2, output_snapshots)
                if stale:
                    if status in APPROVED_STATUSES:
                        status = STATUS_REVIEW_FAILED
                    blockers.extend(stale)
                current_marker = self._review_context_marker(int(inspector_2.get("round") or 0) or None)
                if not inspector_2.get("session_marker_present") or inspector_2.get("session_marker") != current_marker:
                    if status in APPROVED_STATUSES:
                        status = STATUS_REVIEW_FAILED
                    blockers.append("Inspector 2 review does not reference the current review freshness marker.")
                if self._recovery_events and not inspector_2.get("partial_output_acknowledged"):
                    if status in APPROVED_STATUSES:
                        status = STATUS_REVIEW_FAILED
                    blockers.append("Inspector 2 did not acknowledge partial output recovery metadata.")
        else:
            status = STATUS_APPROVED_WITH_WAIVER if not blockers else status
            waivers.append(
                waiver_reason
                or "Inspector 2 was explicitly waived by --second-inspector=off."
            )

        issue_blockers = self._issue_lifecycle_blockers()
        if issue_blockers:
            if status in APPROVED_STATUSES:
                status = STATUS_REVIEW_FAILED
            blockers.extend(issue_blockers)

        excellence_records: list[dict] = []
        excellence_blockers: list[str] = []
        latest_reviews_for_excellence = [item for item in (inspector_1, inspector_2) if item]
        if not second_inspector_required:
            latest_reviews_for_excellence = [item for item in (inspector_1,) if item]
        for review in latest_reviews_for_excellence:
            reviewer_name = review.get("reviewer") or "inspector"
            checklist = review.get("excellence_checklist") or []
            missing = review.get("excellence_missing_items") or []
            for item in checklist:
                record = dict(item)
                excellence_records.append(record)
                status_value = str(record.get("status") or EXCELLENCE_STATUS_UNKNOWN).upper()
                blocker = str(record.get("blocker") or "")
                label = str(record.get("label") or record.get("key") or "excellence item")
                if status_value in (EXCELLENCE_STATUS_FAIL, EXCELLENCE_STATUS_UNKNOWN):
                    excellence_blockers.append(
                        f"Excellence checklist blocker from {reviewer_name}: {label} is {status_value}"
                        + (f" - {blocker}" if blocker else ".")
                    )
                elif status_value == EXCELLENCE_STATUS_PASS and not str(record.get("evidence") or "").strip():
                    excellence_blockers.append(
                        f"Excellence checklist blocker from {reviewer_name}: {label} is PASS without evidence."
                    )
                elif status_value == EXCELLENCE_STATUS_WAIVED and not str(record.get("evidence") or "").strip():
                    excellence_blockers.append(
                        f"Excellence checklist blocker from {reviewer_name}: {label} is WAIVED without waiver evidence."
                    )
            for key in missing:
                excellence_blockers.append(
                    f"Excellence checklist blocker from {reviewer_name}: missing {_excellence_label_for_key(str(key))}."
                )
        if issue_blockers and not any(item.get("key") == "material_issues_resolved" for item in excellence_records):
            excellence_blockers.append(
                "Excellence checklist blocker: Material issues resolved cannot pass while Congress issue lifecycle has open material issues."
            )
        if excellence_blockers:
            if status in APPROVED_STATUSES:
                status = STATUS_REVIEW_FAILED
            blockers.extend(excellence_blockers)

        approved = not blockers and status in APPROVED_STATUSES
        if approved and allow_second_inspector_waiver:
            status = STATUS_APPROVED_WITH_WAIVER
        elif approved:
            status = STATUS_APPROVED

        assessment = FinalApprovalAssessment(
            approved=approved,
            status=status,
            blocking_reasons=blockers,
            waivers=waivers,
            output_files=output_snapshots,
            verification_status=str(verification_status),
            inspector_1_status=inspector_1_status,
            inspector_2_status=inspector_2_status,
            second_inspector_required=second_inspector_required,
            result_status=intended_status,
            issue_state=_issue_records_to_state(self.issue_records),
            unresolved_material_issues=[
                _issue_record_to_dict(item) for item in self.issue_records
                if item.materiality == "material" and item.status not in ISSUE_CLOSED_STATUSES
            ],
            excellence_checklist=excellence_records,
            excellence_blocking_reasons=excellence_blockers,
        )
        self.final_approval_assessment = assessment
        self.review_summary = self._review_summary_for_state()
        self.logger.log_master(
            "SYSTEM",
            f"Final approval gate: approved={assessment.approved} status={assessment.status} "
            f"blockers={len(assessment.blocking_reasons)} waivers={len(assessment.waivers)}",
        )
        return assessment

    def _build_researcher_prompt(self, user_query: str, round_num: int) -> str:
        original_request_section = self._original_request_prompt_section(user_query)
        source_section = self._source_file_section()
        optional_source_section = self._optional_source_context_prompt_section()
        output_section = self._output_file_section()
        output_status_section = self._output_status_section()
        session_request_section = self._session_request_section()
        user_updates_section = self._user_updates_prompt_section()
        capability_section = self._capability_prompt_section()
        verification_section = self._verification_prompt_section()
        issue_section = self._issue_lifecycle_prompt_section()
        recovery_context_section = self._recovery_context_section()
        res_doc_path = os.path.join(self.working_dir, RESEARCHER_UPDATED_FILE)
        ins_doc_path = os.path.join(self.working_dir, INSPECTOR_COMMENTS_FILE)
        ins2_doc_path = os.path.join(self.working_dir, INSPECTOR_2_COMMENTS_FILE)
        has_res_doc  = round_num > 1 and os.path.exists(res_doc_path)
        has_ins_doc  = round_num > 1 and os.path.exists(ins_doc_path)
        has_ins2_doc = round_num > 1 and os.path.exists(ins2_doc_path)

        # Round 2+: point agent at living documents on disk
        living_doc_lines = []
        if has_res_doc:
            living_doc_lines.append(
                f"  Your previous notes/process report (latest): {res_doc_path}")
        if has_ins_doc:
            living_doc_lines.append(
                f"  Inspector feedback/comments (latest):      {ins_doc_path}")
        if has_ins2_doc:
            living_doc_lines.append(
                f"  Inspector 2 feedback/comments (latest):    {ins2_doc_path}")

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
            "Read all Tier 1 required source/input files listed above or in the session request before deciding what to change."
        )
        steps.append(
            "Use the optional Tier 2/Tier 3 source manifest to inspect files needed for a complete and trustworthy answer. "
            "Do not skip files that are clearly relevant, but do not mechanically read unrelated optional files."
        )
        steps.append(
            "Read every requested output file listed above from disk. If any requested output file is missing, create it."
        )
        if round_num > 1 and has_res_doc:
            steps.append(f"Read your previous researcher notes from: {res_doc_path}")
        if round_num > 1 and has_ins_doc:
            steps.append(f"Read the latest inspector review from: {ins_doc_path}")
        if self.issue_records:
            steps.append(
                "Because Congress is running in best-output mode, address every material Inspector issue before seeking approval. "
                "Include a `## Inspector Issues Addressed` section in your notes with Fixed, Partially fixed, Not fixed, Accepted risk, or Non-material rationale, changed location, and reason for each tracked material issue. "
                "Researcher claims do not close material issues until an Inspector verifies them."
            )
        if round_num > 1 and has_ins2_doc:
            steps.append(
                f"Read the latest independent Inspector 2 review from: {ins2_doc_path}. "
                "Treat its findings as required revision context for this round."
            )
        steps.append(
            "Create or update ALL requested output files on disk, plus any necessary support files required to make those deliverables complete, runnable, and verifiable.\n"
            "   - If a requested output file already exists, improve it instead of ignoring it.\n"
            "   - Support files may include modules, assets, config, tests, scripts, or helper files needed by the requested deliverables.\n"
            "   - Do not modify Congress-managed files unless explicitly instructed.\n"
            "   - List every support file you created or modified in your notes and explain why it was required.\n"
            "   - If you disagree with a previous inspector point, explain that clearly in your notes.\n"
            "   - The requested output files on disk are the actual deliverables."
        )
        steps.append(
            "Use real project commands, tools, searches, builds, or tests when they are needed to produce trustworthy deliverables. "
            "Report the exact checks you ran and their outcomes in stdout."
        )
        steps.append(
            "If you are blocked by a missing credential, unavailable local server, UI action, paid API, or required user decision, "
            "include a Markdown section titled BLOCKED with the specific missing item and what you verified."
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

        inspector_2_feedback_section = ""
        if has_ins2_doc:
            try:
                with open(ins2_doc_path, "r", encoding="utf-8", errors="replace") as f:
                    inspector_2_feedback = f.read(12000)
            except OSError:
                inspector_2_feedback = ""
            if inspector_2_feedback:
                inspector_2_feedback_section = (
                    f"\n{'=' * 60}\n"
                    f"INSPECTOR 2 FEEDBACK EXCERPT - ADDRESS THIS ROUND\n"
                    f"{'=' * 60}\n"
                    f"{_truncate_for_prompt(inspector_2_feedback, 8000)}\n"
                )

        return (
            f"{RESEARCHER_SYSTEM_PROMPT}\n"
            f"{original_request_section}"
            f"{source_section}\n"
            f"{optional_source_section}"
            f"{output_section}"
            f"{output_status_section}"
            f"{session_request_section}"
            f"{user_updates_section}"
            f"{capability_section}"
            f"{verification_section}"
            f"{issue_section}"
            f"{recovery_context_section}"
            f"{inspector_2_feedback_section}"
            f"{living_docs_section}"
            f"{task_section}"
        )

    def _build_inspector_prompt(self, user_query: str, researcher_output: str,
                                round_num: int) -> str:
        original_request_section = self._original_request_prompt_section(user_query)
        source_section = self._source_file_section()
        optional_source_section = self._optional_source_context_prompt_section()
        output_section = self._output_file_section()
        output_status_section = self._output_status_section()
        session_request_section = self._session_request_section()
        user_updates_section = self._user_updates_prompt_section()
        capability_section = self._capability_prompt_section()
        verification_section = self._verification_prompt_section()
        issue_section = self._issue_lifecycle_prompt_section()
        review_contract_section = self._review_prompt_section("Inspector 1", round_num)
        recovery_context_section = self._recovery_context_section()

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
            "   VERDICT: NEEDS_REVISION   (if any CRITICAL, HIGH, or MEDIUM material issue remains)\n"
            "   VERDICT: NEEDS_REVISION   (if any actionable issue would materially improve the requested output)\n"
            "   VERDICT: APPROVED         (only if no material unresolved issue remains)\n"
            "LOW issues may be approved only when they are cosmetic or truly non-material, with concrete rationale."
        )

        steps = []
        steps.append(f"Read the managed session request file: {os.path.join(self.working_dir, SESSION_REQUEST_FILE)}")
        steps.append("Re-read the ORIGINAL USER REQUEST included inline below. It remains the source of truth every round.")
        steps.append("Read all Tier 1 required source/input files listed above or in the session request.")
        steps.append(
            "Inspect optional Tier 2/Tier 3 source context as needed for review confidence, especially when the deliverable makes claims about those files. "
            "Do not treat optional manifest entries as a mandatory full-read list."
        )
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
            f"   - Run real commands, tests, builds, scripts, or environment checks when they are relevant and available.\n"
            f"   - Cite concrete file paths, observed behavior, command outputs, and any checks you could not run.\n"
            f"   - Mark previous issues as RESOLVED or STILL PRESENT when applicable.\n"
            f"   - Congress will automatically save your stdout as {INSPECTOR_COMMENTS_FILE} v{round_num}."
        )

        steps.append(
            "Output your COMPLETE review to stdout. "
            "Do not edit the requested deliverables or Congress-managed files; stdout is your review document."
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
            f"{original_request_section}"
            f"{source_section}\n"
            f"{optional_source_section}"
            f"{output_section}"
            f"{output_status_section}"
            f"{session_request_section}"
            f"{user_updates_section}"
            f"{capability_section}"
            f"{verification_section}"
            f"{issue_section}"
            f"{review_contract_section}"
            f"{recovery_context_section}"
            f"{living_docs_section}"
            f"{task_section}"
            f"{inline_fallback}"
        )

    def _build_inspector_2_prompt(self, user_query: str, researcher_output: str,
                                  inspector_1_output: str, round_num: int) -> str:
        _ = inspector_1_output  # Inspector 2 must not receive Inspector 1 review content pre-review.
        original_request_section = self._original_request_prompt_section(user_query)
        source_section = self._source_file_section()
        optional_source_section = self._optional_source_context_prompt_section()
        output_section = self._output_file_section()
        output_status_section = self._output_status_section()
        session_request_section = self._session_request_section()
        user_updates_section = self._user_updates_prompt_section()
        capability_section = self._capability_prompt_section()
        verification_section = self._verification_prompt_section()
        issue_section = self._issue_lifecycle_prompt_section(audience="inspector_2_pre_review")
        review_contract_section = self._review_prompt_section("Inspector 2", round_num)
        recovery_context_section = self._recovery_context_section()
        res_doc_path = os.path.join(self.working_dir, RESEARCHER_UPDATED_FILE)
        ins2_doc_path = os.path.join(self.working_dir, INSPECTOR_2_COMMENTS_FILE)

        living_docs_section = (
            f"\n{'=' * 60}\n"
            f"LIVING DOCUMENTS - READ THESE COMPLETELY BEFORE STARTING:\n"
            f"{'=' * 60}\n"
            f"  Researcher notes: {res_doc_path}\n"
            f"  Your review will be saved as: {ins2_doc_path}\n"
            "  Do not open or read prior Inspector 1 review artifacts or any Inspector 1 review excerpts before producing your independent findings.\n"
        )

        prompt = (
            "You are INSPECTOR 2 in Congress. You are an independent adversarial reviewer and independent second reviewer.\n"
            "Before producing your independent findings, do not open or read prior Inspector 1 review artifacts or any Inspector 1 review excerpts.\n"
            "Do not rubber-stamp. Do not rely on Inspector 1. Do not summarize Inspector 1. Do not confirm Inspector 1. Do not rubber-stamp Inspector 1. Do not approve because another reviewer approved.\n"
            "Inspect the deliverable from a skeptical opposite point of view. Search for hidden issues, weak assumptions, missing references, logical gaps, contradictions, implementation traps, bad defaults, unjustified strategies, overclaiming, non-English/forum/source weakness, and better algorithm or better path possibilities.\n"
            "Re-read the original request, inspect the real files from disk, "
            "review Congress verification evidence as mechanical context only, and run any real commands/tests/checks needed for confidence.\n"
            "You have full practical Codex capability like Inspector 1. Do not output JSON or JSONL.\n"
            "If your independent review finds any material issue, including any MEDIUM issue in best-output mode, use VERDICT: NEEDS_REVISION.\n"
            "If you approve, your approval must be grounded in current output hashes and verification evidence.\n"
        )
        inline_context = (
            f"\n{'=' * 60}\n"
            f"INLINE CONTEXT FALLBACKS\n"
            f"{'=' * 60}\n"
            f"Researcher notes excerpt:\n{_truncate_for_prompt(researcher_output, 8000)}\n"
        )
        task_section = (
            f"\n{'=' * 60}\n"
            f"YOUR TASK FOR ROUND {round_num}:\n"
            f"{'=' * 60}\n"
            "Pre-review independence guardrail: do not read or open prior Inspector 1 review artifacts before producing your independent findings.\n"
            f"1. Read {SESSION_REQUEST_FILE}, all requested outputs, Tier 1 required source/input files, researcher notes, and {CONGRESS_VERIFICATION_FILE}.\n"
            f"2. Inspect optional Tier 2/Tier 3 source context as needed for independent review confidence. Do not treat optional manifest entries as a mandatory full-read list.\n"
            f"3. Independently verify the deliverables against the original user request.\n"
            f"4. Produce a complete Markdown review using the required review contract.\n"
            f"5. End with exactly one terminal verdict line: VERDICT: APPROVED or VERDICT: NEEDS_REVISION.\n"
        )
        return (
            f"{prompt}\n"
            f"{original_request_section}"
            f"{source_section}"
            f"{optional_source_section}"
            f"{output_section}"
            f"{output_status_section}"
            f"{session_request_section}"
            f"{user_updates_section}"
            f"{capability_section}"
            f"{verification_section}"
            f"{issue_section}"
            f"{review_contract_section}"
            f"{recovery_context_section}"
            f"{living_docs_section}"
            f"{task_section}"
            f"{inline_context}"
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
        if self.ci_mode or not self._interactive:
            self.ui.status(
                f"Internet connection lost. Auto-retrying in {RATE_LIMIT_AUTO_RETRY_SECONDS}s...",
                C_RED,
            )
            self.logger.log_master(
                "SYSTEM",
                f"Internet unavailable; unattended auto-retry in {RATE_LIMIT_AUTO_RETRY_SECONDS}s",
            )
            time.sleep(RATE_LIMIT_AUTO_RETRY_SECONDS)
            return True

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

    def _wait_for_rate_limit(self, retry_info: str, *, agent: str = "",
                             round_label=None, session_id: str | None = None,
                             stdout: str = "", stderr: str = "",
                             partial_paths: dict | None = None) -> str:
        """
        Pause after a rate limit hit. Waits for user to press R (retry) or Q (quit),
        and auto-retries every 3 minutes while waiting.
        Returns: "manual_retry", "auto_retry", or "quit".
        """
        _flush_input()
        msg = f"API rate/usage limit hit.{retry_info}"
        auto_minutes = max(1, RATE_LIMIT_AUTO_RETRY_SECONDS // 60)
        self.ui.error(msg)
        self._save_retry_state(
            self._active_user_query,
            round_label if round_label is not None else self._active_round,
            f"{agent}_running" if agent else "",
            agent or "codex",
            "rate_limit",
            retry_after_seconds=RATE_LIMIT_AUTO_RETRY_SECONDS,
            session_id=session_id,
            stdout=stdout,
            stderr=stderr,
            output_status=_collect_output_status(self.working_dir, self.output_files) if self.output_files else [],
            partial_paths=partial_paths,
        )
        if self.ci_mode or not self._interactive:
            self.ui.status(
                f"Auto-retry in {RATE_LIMIT_AUTO_RETRY_SECONDS}s for rate limit.",
                C_YELLOW,
            )
            self.logger.log_master("SYSTEM", f"Rate limit unattended pause: {msg}")
            time.sleep(RATE_LIMIT_AUTO_RETRY_SECONDS)
            self.logger.log_master(
                "SYSTEM",
                f"Rate limit auto-retry after {RATE_LIMIT_AUTO_RETRY_SECONDS}s",
            )
            return "auto_retry"

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
          rc==-3 or network error → save state, wait, retry
          startup/silence timeout → save state, wait, retry/resume
          context limit with partial output → resume session with "continue"
          rate limit (check regardless of output)
            → save partial output, wait, retry/resume
          other failure / success → return as-is
        """
        stdout, stderr, rc, duration = "", "", -1, 0.0
        total_duration = 0.0
        attempt = 0

        # Session tracking
        sid_key = f"{agent}_session_id"
        current_sid = getattr(self, sid_key, None) if use_session else None

        def retry_substep() -> str:
            return f"{agent}_running"

        def timeout_is_recoverable(err: str, code: int) -> bool:
            if code != -1:
                return False
            lowered = (err or "").lower()
            return (
                "did not start within" in lowered
                or "timed out: zero activity" in lowered
            )

        while True:
            if self._interrupt_requested:
                return "", "interrupted", -2, total_duration, current_sid

            current_prompt = prompt
            if attempt > 0:
                if current_sid:
                    # Resuming existing session — agent has context, just say continue
                    current_prompt = "continue"
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

            # ── Priority 2: Recoverable timeout/network error ──
            if (
                _looks_like_complete_agent_output(agent, stdout)
                and _is_only_nonfatal_codex_stderr(stderr, rc)
            ):
                if (stderr or "").strip():
                    self.logger.log_master(
                        "SYSTEM",
                        f"{agent} produced complete review output; ignoring nonfatal Codex stderr.",
                    )
                clean_review_rc = 0
                return stdout, stderr, clean_review_rc, total_duration, current_sid

            if rc == -3 or _is_network_error(stderr, rc) or timeout_is_recoverable(stderr, rc):
                reason = (
                    "timeout"
                    if timeout_is_recoverable(stderr, rc)
                    else "network"
                )
                self.logger.log_master("SYSTEM",
                    f"Recoverable {reason} error on attempt {attempt + 1}: {stderr[:100]}")
                partial_event = None
                if stdout.strip():
                    partial_event = self._record_partial_recovery(
                        agent, round_label, f"{reason}_partial",
                        f"partial stdout before recoverable {reason} retry",
                        len(stdout),
                        stdout=stdout,
                        stderr=stderr,
                    )
                self._save_retry_state(
                    self._active_user_query,
                    round_label,
                    retry_substep(),
                    agent,
                    reason,
                    retry_after_seconds=RATE_LIMIT_AUTO_RETRY_SECONDS,
                    session_id=current_sid,
                    stdout=stdout,
                    stderr=stderr,
                    output_status=_collect_output_status(self.working_dir, self.output_files) if self.output_files else [],
                    partial_paths=partial_event,
                )
                if reason == "network":
                    if not self._wait_for_internet():
                        return stdout, stderr, -2, total_duration, current_sid
                else:
                    self.ui.status(
                        f"Recoverable timeout. Auto-retrying in {RATE_LIMIT_AUTO_RETRY_SECONDS}s...",
                        C_YELLOW,
                    )
                    time.sleep(RATE_LIMIT_AUTO_RETRY_SECONDS)
                attempt += 1
                continue

            # ── Priority 3: Context window exhaustion — resume session ──
            if _matches_context_limit_error(stderr):
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

                    cont_num = 1
                    while cont_num <= MAX_CONTINUATIONS:
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
                                f"{len(accumulated)} chars saved; waiting to resume.",
                                C_YELLOW)
                            self.logger.log_master("SYSTEM",
                                f"Network error mid-continuation, saving {len(accumulated)} chars")
                            partial_event = self._record_partial_recovery(
                                agent, round_label, "network_mid_continuation",
                                "network error during context-limit continuation",
                                len(accumulated),
                                stdout=accumulated,
                                stderr=cont_err,
                            )
                            self._save_retry_state(
                                self._active_user_query,
                                round_label,
                                retry_substep(),
                                agent,
                                "network_mid_continuation",
                                retry_after_seconds=RATE_LIMIT_AUTO_RETRY_SECONDS,
                                session_id=current_sid,
                                stdout=accumulated,
                                stderr=cont_err,
                                output_status=_collect_output_status(self.working_dir, self.output_files) if self.output_files else [],
                                partial_paths=partial_event,
                            )
                            if not self._wait_for_internet():
                                return accumulated, cont_err, -2, total_duration, current_sid
                            continue

                        # Check for rate limit mid-continuation
                        is_limited, _ = _is_rate_limited(cont_err, cont_rc)
                        if is_limited:
                            self.ui.status(
                                f"Rate limit during continuation — "
                                f"{len(accumulated)} chars saved; waiting to resume.",
                                C_YELLOW)
                            self.logger.log_master("SYSTEM",
                                f"Rate limit mid-continuation, saving {len(accumulated)} chars")
                            partial_event = self._record_partial_recovery(
                                agent, round_label, "rate_limit_mid_continuation",
                                "rate limit during context-limit continuation",
                                len(accumulated),
                                stdout=accumulated,
                                stderr=cont_err,
                            )
                            wait_started = time.monotonic()
                            wait_action = self._wait_for_rate_limit(
                                _is_rate_limited(cont_err, cont_rc)[1],
                                agent=agent,
                                round_label=round_label,
                                session_id=current_sid,
                                stdout=accumulated,
                                stderr=cont_err,
                                partial_paths=partial_event,
                            )
                            total_duration += time.monotonic() - wait_started
                            if wait_action == "quit":
                                return accumulated, cont_err, -2, total_duration, current_sid
                            continue

                        # If NOT context limit again → agent finished cleanly
                        if not _matches_context_limit_error(cont_err):
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
                        cont_num += 1

                    # Exhausted MAX_CONTINUATIONS — mark partial; do not report clean success
                    self.ui.status(
                        f"Max continuations reached — saved {len(accumulated)} partial chars.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Max continuations ({MAX_CONTINUATIONS}) exhausted, "
                        f"partial output chars={len(accumulated)}")
                    partial_event = self._record_partial_recovery(
                        agent, round_label, "max_continuations",
                        "context-limit continuation limit exhausted",
                        len(accumulated),
                        stdout=accumulated,
                        stderr=stderr,
                    )
                    self._save_retry_state(
                        self._active_user_query,
                        round_label,
                        retry_substep(),
                        agent,
                        "max_continuations_partial",
                        session_id=current_sid,
                        stdout=accumulated,
                        stderr=stderr,
                        output_status=_collect_output_status(self.working_dir, self.output_files) if self.output_files else [],
                        partial_paths=partial_event,
                    )
                    return accumulated, stderr, 1, total_duration, current_sid

                elif stdout.strip():
                    # No session ID available — record partial and report non-clean completion
                    self.ui.status(
                        f"Context window exhausted — {len(stdout)} chars captured "
                        f"(no session to resume). Marking partial output incomplete.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Context limit, no session ID, partial chars={len(stdout)}")
                    partial_event = self._record_partial_recovery(
                        agent, round_label, "context_limit_no_session",
                        "context limit with no session id available",
                        len(stdout),
                        stdout=stdout,
                        stderr=stderr,
                    )
                    self._save_retry_state(
                        self._active_user_query,
                        round_label,
                        retry_substep(),
                        agent,
                        "context_limit_no_session_partial",
                        session_id=current_sid,
                        stdout=stdout,
                        stderr=stderr,
                        output_status=_collect_output_status(self.working_dir, self.output_files) if self.output_files else [],
                        partial_paths=partial_event,
                    )
                    return stdout, stderr, 1, total_duration, current_sid

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
                partial_event = None
                if stdout.strip():
                    # Partial output captured before limit hit — save, then resume/retry.
                    self.ui.status(
                        f"Rate limit hit after {len(stdout)} chars — saving partial output and retrying.",
                        C_YELLOW)
                    self.logger.log_master("SYSTEM",
                        f"Rate limit with partial output ({len(stdout)} chars) — retrying")
                    partial_event = self._record_partial_recovery(
                        agent, round_label, "rate_limit_partial",
                        "rate limit after partial stdout",
                        len(stdout),
                        stdout=stdout,
                        stderr=stderr,
                    )
                # Rate limits must pause and retry, even when partial stdout exists.
                wait_started = time.monotonic()
                wait_action = self._wait_for_rate_limit(
                    retry_info,
                    agent=agent,
                    round_label=round_label,
                    session_id=current_sid,
                    stdout=stdout,
                    stderr=stderr,
                    partial_paths=partial_event,
                )
                total_duration += time.monotonic() - wait_started
                if wait_action == "quit":
                    return stdout, stderr, -2, total_duration, current_sid
                attempt += 1
                continue   # retry

            # ── No error condition matched: return (success or other failure) ──
            return stdout, stderr, rc, total_duration, current_sid

        # Should never reach here (loop always returns inside), but safety fallback
        return stdout, stderr, rc, total_duration, current_sid

    # ──────────────────────────────────────────────────────────────────────
    # Transition menu (pause / resume / output / continue)
    # ──────────────────────────────────────────────────────────────────────

    def _transition_menu(self, next_agent: str, round_num: int,
                         user_query: str | None = None) -> str:
        """Show transition menu between agents.
        Returns: 'continue', 'output', or 'quit'.
        Skipped entirely in non-interactive mode.
        """
        if self.ci_mode or not self._interactive:
            return "continue"

        _flush_input()

        print()
        print(f"  {C_CYAN}{'─' * 50}{C_RESET}")
        print(f"  {C_CYAN}{C_BOLD}  What next?{C_RESET}")
        print(f"  {C_GREEN}  [C]{C_RESET} Continue to {next_agent}  "
              f"{C_DIM}(auto in {AUTO_CONTINUE_SECS}s){C_RESET}")
        print(f"  {C_YELLOW}  [P]{C_RESET} Pause (wait for your command)")
        print(f"  {C_CYAN}  [M]{C_RESET} Add comment for agents")
        print(f"  {C_MAGENTA}  [O]{C_RESET} Output now (finalize current result)")
        print(f"  {C_RED}  [Q]{C_RESET} Quit session")
        print(f"  {C_CYAN}{'─' * 50}{C_RESET}")

        old_settings = _enter_cbreak()
        try:
            result = self._menu_countdown()
        except KeyboardInterrupt:
            self.ui.status("Interrupted.", C_RED)
            self.logger.log_master("SYSTEM", "Interrupted during transition menu")
            return "quit"
        finally:
            _exit_cbreak(old_settings)
        if result == "pause":
            return self._menu_pause_loop(user_query=user_query, round_num=round_num)
        if result == "comment":
            self._add_interactive_comment(user_query=user_query, round_num=round_num)
            return "continue"
        return result

    def _menu_countdown(self) -> str:
        for remaining in range(AUTO_CONTINUE_SECS, 0, -1):
            sys.stdout.write(
                f"\r  {C_DIM}  Auto-continue in {remaining}s... "
                f"(press C/P/M/O/Q){C_RESET}    ")
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
                    elif key == "m":
                        return "comment"
                    elif key == "o":
                        return "output"
                    elif key == "q":
                        return "quit"
                time.sleep(0.1)

        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
        self.ui.status("Auto-continuing...", C_DIM)
        return "continue"

    def _add_interactive_comment(self, user_query: str | None = None,
                                 round_num: int = 1) -> None:
        try:
            note = input("Note for agents: ").strip()
        except (EOFError, KeyboardInterrupt):
            note = ""
        if note:
            self._record_user_update(
                note,
                "pause-comment",
                user_query=user_query,
                current_round=round_num,
                persist=bool(user_query),
            )
            self.ui.status("Comment added for subsequent agents.", C_GREEN)

    def _menu_pause_loop(self, user_query: str | None = None,
                         round_num: int = 1) -> str:
        self.ui.status("PAUSED. Press [R] Resume  [M] Add comment  [O] Output now  [Q] Quit", C_YELLOW)
        self.logger.log_master("SYSTEM", "Paused by user")

        wait_count = 0
        old_settings = _enter_cbreak()
        try:
            while True:
                if _kbhit():
                    key = _consume_key()
                    if key == "r":
                        self.ui.status("Resumed!", C_GREEN)
                        self.logger.log_master("SYSTEM", "Resumed by user")
                        return "continue"
                    elif key == "m":
                        _exit_cbreak(old_settings)
                        old_settings = None
                        self._add_interactive_comment(user_query=user_query, round_num=round_num)
                        old_settings = _enter_cbreak()
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
                        f"[R] Resume  [M] Comment  [O] Output  [Q] Quit", C_DIM)
        finally:
            _exit_cbreak(old_settings)

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

    def _build_missing_output_repair_instruction(self, round_num: int,
                                                 missing_outputs: list[str],
                                                 invalid_outputs: list[str]) -> str:
        lines = [
            f"Congress repair instruction after Researcher round {round_num}:",
            "The previous Researcher pass did not satisfy the requested output contract.",
        ]
        if missing_outputs:
            lines.append("Missing requested output files: " + ", ".join(missing_outputs))
        if invalid_outputs:
            lines.append("Requested output paths that are not regular files: " + ", ".join(invalid_outputs))
        lines.extend([
            "Next Researcher must create or repair every listed deliverable before Inspector review.",
            "Read the original request, session_request.md, and the requested output files from disk before continuing.",
        ])
        return " ".join(lines)

    def _build_no_output_review_fallback(self, reviewer_label: str, artifact_name: str,
                                         round_num: int, rc: int, stderr: str) -> str:
        marker = self._review_context_marker(round_num)
        hashes = _current_output_hashes(self.working_dir, self.output_files)
        hash_lines = []
        for item in hashes:
            state = "exists" if item.get("exists") else "missing"
            if item.get("invalid_type"):
                state = "invalid"
            hash_lines.append(
                f"- {item.get('path')}: {state}, sha256={item.get('sha256') or 'unavailable'}"
            )
        reason = _redact_sensitive_text((stderr or "").strip()[:1000]) or "no stderr detail"
        title = "Inspector 2 Review Unavailable" if reviewer_label == "Inspector 2" else "Inspector Review Unavailable"
        return "\n".join([
            f"# {title}",
            "",
            "## Findings",
            f"- {reviewer_label} produced no usable output for round {round_num}.",
            "- Congress records this as a review retry signal, not as deliverable approval.",
            "- The next round receives this artifact so the deliverable can be reviewed again.",
            "",
            "## Evidence Reviewed",
            f"- Review freshness marker: {marker}",
            f"- Runner return code: {rc}",
            f"- Runner reason: {reason}",
            "- partial output recovery metadata was considered if present.",
            "",
            "## Commands/Tests Run",
            "- No reviewer commands were recorded because the review produced no usable output.",
            "",
            "## Output Hashes",
            *(hash_lines or ["- No requested output hashes were available."]),
            "",
            "## Verification Evidence",
            f"- Review fallback references {CONGRESS_VERIFICATION_FILE}; next reviewer must inspect current verification evidence directly.",
            "",
            "## Waived Checks",
            "- None.",
            "",
            "## Excellence Checklist",
            "- Original request fully answered: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Deliverable directly usable: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Material issues resolved: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Claims sourced or assumptions labeled: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Implementation details present: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Edge cases and failure modes covered: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Verification/test strategy specific: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Internally consistent: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- No raw secrets included: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "- Best practical version: UNKNOWN - Blocker: reviewer unavailable in this round.",
            "",
            "## Unresolved Issues",
            "- None for deliverable content; review was unavailable in this round.",
            "",
            "VERDICT: NEEDS_REVISION",
        ])

    def _build_session_summary(self, status: str) -> str:
        output_status = _collect_output_status(self.working_dir, self.output_files)
        lines = [
            f"SESSION STATUS: {status}",
            f"WORKING DIRECTORY: {self.working_dir}",
            f"QUALITY MODE: {self.quality_mode}",
            "",
            "REQUESTED OUTPUT FILES:",
        ]
        if self.output_files:
            for item in output_status:
                if item.get("invalid_type"):
                    marker = "INVALID"
                    size_text = item.get("invalid_reason") or "exists but is not a valid output file"
                else:
                    marker = "OK" if item["exists"] else "MISSING"
                    size_text = f"{item['size']} bytes" if item["size"] is not None else "no size"
                lines.append(f"- [{marker}] {item['path']} ({size_text})")
        else:
            lines.append("- (none)")

        lines.extend([
            "",
            "MANAGED CONGRESS FILES:",
            *[f"- {item}" for item in KNOWN_MANAGED_ARTIFACTS],
            "",
            f"LOGS: {self.logger.session_dir}",
        ])
        return "\n".join(lines)

    def _finalize_output(self, user_query: str, current_output: str) -> str:
        """Build a read-only session summary for output-file mode."""
        self.ui.status("Building read-only session summary...", C_CYAN)
        self.logger.log_master("SYSTEM", "Finalize output requested (summary only)")
        return self._finalize_result(
            user_query,
            STATUS_OUTPUT_EARLY,
            blocking_reasons=["User requested output before approval was reached."],
        )

    # ──────────────────────────────────────────────────────────────────────
    def _result_history_collisions(self) -> list[str]:
        collisions = []
        result_key = _relpath_key(self.result_file)
        history_key = _relpath_key(CONGRESS_HISTORY_FILE)
        for relpath in self.output_files:
            relkey = _relpath_key(relpath)
            if _is_reserved_output_relpath(relpath) or relkey in (result_key, history_key):
                collisions.append(relpath)
        return collisions

    def _write_result_artifacts(self, result: CongressResult) -> None:
        result_path = _workspace_abs_path(self.working_dir, self.result_file)
        history_path = os.path.join(self.working_dir, CONGRESS_HISTORY_FILE)
        if result.blocked:
            _write_blocked_artifact(self.working_dir, result.blocked)
        parent = os.path.dirname(result_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_text(result_path, render_congress_result_markdown(result))
        _append_text(history_path, _render_history_event(result))

    def _artifact_path_errors(self) -> list[str]:
        errors = _managed_artifact_path_errors(self.working_dir)
        result_path = _workspace_abs_path(self.working_dir, self.result_file)
        if self.result_file != CONGRESS_RESULT_FILE:
            if os.path.exists(result_path) and not os.path.isfile(result_path):
                errors.append(f"{self.result_file} exists but is not a regular file")
            tmp_path = result_path + ".tmp"
            if os.path.exists(tmp_path) and not os.path.isfile(tmp_path):
                errors.append(f"{self.result_file}.tmp exists but is not a regular file")
        return errors

    def _save_final_state(self, user_query: str, current_round: int,
                          result: CongressResult,
                          resume_substep: str | None = None) -> None:
        current_substep = (
            resume_substep
            if result.status == STATUS_INTERRUPTED_RESUMABLE and resume_substep
            else "blocked"
            if result.status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT)
            else PHASE_TERMINAL
        )
        if result.status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT):
            self.blocked_state = result.blocked
        elif result.status != STATUS_INTERRUPTED_RESUMABLE:
            self.blocked_state = None
        state = _build_state_v3(
            working_dir=self.working_dir,
            session_id=self.session_id,
            user_query=user_query,
            output_files=self.output_files,
            source_files=self.source_files,
            required_source_files=self.required_source_files,
            source_manifest=self.source_manifest,
            max_rounds=self.max_rounds,
            current_round=current_round,
            current_substep=current_substep,
            status=result.status,
            final_status=result.status,
            researcher_session_id=self.researcher_session_id,
            inspector_session_id=self.inspector_session_id,
            inspector_2_session_id=self.inspector_2_session_id,
            last_result=_redacted_result_to_dict(result),
            user_updates=self.user_updates,
            blocked_state=_redact_sensitive_data(self.blocked_state),
            preflight_state=_redact_sensitive_data(self.preflight_state),
            verification_mode=self.verification_mode,
            max_verification_timeout=self.max_verification_timeout,
            second_inspector_mode=self.second_inspector_mode,
            log_prompts_mode=self.log_prompts_mode,
            quality_mode=self.quality_mode,
            result_file=self.result_file,
            strict_exit_codes=self.strict_exit_codes,
            ci_mode=self.ci_mode,
            ci_mode_explicit=self.ci_mode_explicit,
            interactive_requested=self.interactive_requested,
            retry_state=_redact_sensitive_data(self.retry_state),
            task_classification=_task_classification_to_dict(self.task_classification),
            verification_state=_redact_sensitive_data(self.verification_summary),
            verification_checks=[
                _redact_sensitive_data(_verification_check_to_dict(check))
                for check in self.verification_checks
            ],
            review_state=_redact_sensitive_data(self.review_summary),
            issue_state=_redact_sensitive_data(_issue_records_to_state(self.issue_records)),
        )
        _save_state_file(self.working_dir, state)

    def _finalize_without_workspace_writes(self, user_query: str, status: str,
                                           blocking_reasons: list[str] | None = None) -> str:
        final_status = status if status in CONTROLLED_STATUSES else STATUS_INTERNAL_ERROR
        summary = self._build_session_summary(final_status)
        result = CongressResult(
            status=final_status,
            exit_code=_status_to_exit_code(final_status),
            approved=_status_is_approved(final_status),
            blocking_reasons=list(blocking_reasons or []),
            output_files=_collect_result_output_snapshots(self.working_dir, self.output_files),
            verification=_default_verification_placeholder(final_status),
            reviews=_default_review_placeholder(final_status),
            result_file=self.result_file,
            summary_text=summary,
        )
        self.last_result = result
        self.logger.log_master(
            "SYSTEM",
            "Finalized without workspace writes: "
            f"{final_status}; reasons={'; '.join(result.blocking_reasons) or 'none'}",
        )
        return summary

    def _finalize_result(self, user_query: str, status: str,
                         summary_text: str | None = None,
                         blocking_reasons: list[str] | None = None,
                         verification: dict | None = None,
                         reviews: dict | None = None,
                         blocked: dict | None = None,
                         current_round: int | None = None,
                         resume_substep: str | None = None) -> str:
        final_status = status if status in CONTROLLED_STATUSES else STATUS_INTERNAL_ERROR
        reasons = list(blocking_reasons or [])
        collisions = self._result_history_collisions()
        if collisions:
            final_status = STATUS_FAILED_NONRESUMABLE
            reasons.append(
                "Requested output collides with a Congress-managed artifact: "
                + ", ".join(collisions)
            )
        artifact_path_errors = [] if collisions else self._artifact_path_errors()
        if artifact_path_errors:
            final_status = STATUS_INTERNAL_ERROR
            reasons.extend(artifact_path_errors)

        summary = summary_text or self._build_session_summary(final_status)
        verification_data = verification or _verification_summary_for_current_context(self.verification_summary) or _default_verification_placeholder(final_status)
        if self.capability_inventory:
            verification_data = dict(verification_data)
            verification_data["capability_inventory"] = _redact_sensitive_data(self.capability_inventory)
        review_data = reviews or self.review_summary or _default_review_placeholder(final_status)
        if final_status in APPROVED_STATUSES and isinstance(review_data, dict):
            review_data = dict(review_data)
            review_data["final_approval_status"] = final_status
        result = CongressResult(
            status=final_status,
            exit_code=_status_to_exit_code(final_status),
            approved=_status_is_approved(final_status),
            blocking_reasons=reasons,
            output_files=_collect_result_output_snapshots(self.working_dir, self.output_files),
            verification=verification_data,
            reviews=review_data,
            blocked=blocked,
            result_file=self.result_file,
            summary_text=summary,
        )
        self.last_result = result

        round_for_state = current_round if current_round is not None else (len(self.round_history) or 1)
        try:
            self._save_final_state(user_query, round_for_state, result, resume_substep=resume_substep)
        except Exception as e:
            self.logger.log_master("SYSTEM", f"Could not save final state: {e}")

        if not collisions and not artifact_path_errors:
            try:
                self._write_result_artifacts(result)
            except Exception as e:
                artifact_error = (
                    f"Could not write Congress result/history artifacts: "
                    f"{type(e).__name__}: {e}"
                )
                self.logger.log_master("SYSTEM", artifact_error)
                final_status = STATUS_INTERNAL_ERROR
                reasons.append(artifact_error)
                summary = self._build_session_summary(final_status)
                result = CongressResult(
                    status=final_status,
                    exit_code=_status_to_exit_code(final_status),
                    approved=False,
                    blocking_reasons=reasons,
                    output_files=_collect_result_output_snapshots(self.working_dir, self.output_files),
                    verification=verification or _default_verification_placeholder(final_status),
                    reviews=reviews or _default_review_placeholder(final_status),
                    blocked=blocked,
                    result_file=self.result_file,
                    summary_text=summary,
                )
                self.last_result = result
                try:
                    self._save_final_state(user_query, round_for_state, result, resume_substep=resume_substep)
                except Exception as state_exc:
                    self.logger.log_master("SYSTEM", f"Could not save downgraded final state: {state_exc}")
                try:
                    self._write_result_artifacts(result)
                except Exception as second_exc:
                    self.logger.log_master(
                        "SYSTEM",
                        "Could not write downgraded result/history artifacts: "
                        f"{type(second_exc).__name__}: {second_exc}",
                    )
        elif artifact_path_errors:
            result_path_has_error = any(
                item.startswith(self.result_file) for item in artifact_path_errors
            )
            if not result_path_has_error:
                try:
                    result_path = _workspace_abs_path(self.working_dir, self.result_file)
                    parent = os.path.dirname(result_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    _atomic_write_text(result_path, render_congress_result_markdown(result))
                except Exception as e:
                    self.logger.log_master(
                        "SYSTEM",
                        "Could not write downgraded result artifact after preflight error: "
                        f"{type(e).__name__}: {e}",
                    )
            self.logger.log_master(
                "SYSTEM",
                "Skipped result/history artifact writes because artifact paths are invalid: "
                + "; ".join(artifact_path_errors),
            )
        else:
            self.logger.log_master(
                "SYSTEM",
                "Skipped result/history artifact writes because of requested-output collision: "
                + ", ".join(collisions),
            )
        return summary

    def _finalize_internal_error(self, user_query: str, exc: Exception,
                                 current_round: int | None = None) -> str:
        reason = f"{type(exc).__name__}: {exc}"
        try:
            self.logger.log_master("SYSTEM", f"Internal error finalization: {reason}")
            return self._finalize_result(
                user_query,
                STATUS_INTERNAL_ERROR,
                blocking_reasons=[reason],
                current_round=current_round,
            )
        except Exception as final_exc:
            summary = self._build_session_summary(STATUS_INTERNAL_ERROR)
            self.last_result = CongressResult(
                status=STATUS_INTERNAL_ERROR,
                exit_code=_status_to_exit_code(STATUS_INTERNAL_ERROR),
                approved=False,
                blocking_reasons=[
                    reason,
                    f"Finalizer failed: {type(final_exc).__name__}: {final_exc}",
                ],
                output_files=_collect_result_output_snapshots(self.working_dir, self.output_files),
                verification=_default_verification_placeholder(STATUS_INTERNAL_ERROR),
                reviews=_default_review_placeholder(STATUS_INTERNAL_ERROR),
                result_file=self.result_file,
                summary_text=summary,
            )
            return summary

    # Main debate loop
    # ──────────────────────────────────────────────────────────────────────

    def run(self, user_query: str, start_round: int = 1,
            initial_substep: str = "",
            source_files: list[str] | None = None,
            required_source_files: list[str] | None = None,
            saved_source_manifest: list[dict] | None = None,
            source_context_version: int | None = None,
            saved_issue_state: dict | None = None,
            output_files: list[str] | None = None,
            resume_comment: str | None = None,
            saved_user_updates: list[dict] | None = None) -> str:
        """Run Congress with best-effort terminal result finalization."""
        try:
            return self._run_core(
                user_query,
                start_round=start_round,
                initial_substep=initial_substep,
                source_files=source_files,
                required_source_files=required_source_files,
                saved_source_manifest=saved_source_manifest,
                source_context_version=source_context_version,
                saved_issue_state=saved_issue_state,
                output_files=output_files,
                resume_comment=resume_comment,
                saved_user_updates=saved_user_updates,
            )
        except Exception as exc:
            return self._finalize_internal_error(user_query, exc)
        finally:
            self._release_workspace_lock()

    def _run_inspector_2_stage(self, user_query: str, researcher_output: str,
                               inspector_output: str, round_num: int,
                               pause_event: threading.Event,
                               round_data: dict) -> tuple[str, str, str]:
        if not self._write_current_session_request(user_query, audience="inspector_2_pre_review"):
            final_output = self._finalize_result(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[
                    f"Could not update {SESSION_REQUEST_FILE} with Inspector 2 independent pre-review context."
                ],
                current_round=round_num,
            )
            return "break", final_output, STATUS_FAILED_NONRESUMABLE
        inspector_2_prompt = self._build_inspector_2_prompt(
            user_query,
            researcher_output,
            inspector_output,
            round_num,
        )
        self.logger.log_agent_start("inspector_2", round_num, inspector_2_prompt)
        self._save_state_now(user_query, round_num, "inspector_2_running")
        stdout2, stderr2, rc2, duration2, new_sid2 = self._run_with_recovery(
            "inspector_2", inspector_2_prompt, round_num, pause_event
        )

        self.logger.log_agent_output(
            "inspector_2", round_num, stdout2, stderr2, rc2, duration2)
        if new_sid2:
            self.inspector_2_session_id = new_sid2
        if rc2 == -2:
            round_data["inspector_2_output"] = ""
            round_data["error"] = "interrupted_inspector_2"
            final_output = self._finalize_result(
                user_query,
                STATUS_INTERRUPTED_RESUMABLE,
                blocking_reasons=[
                    "Inspector 2 was interrupted; resume will continue review for this round."
                ],
                current_round=round_num,
                resume_substep="inspector_2_running",
            )
            return "break", final_output, STATUS_INTERRUPTED_RESUMABLE

        inspector_2_output = stdout2.strip()
        if not inspector_2_output:
            inspector_2_output = self._build_no_output_review_fallback(
                "Inspector 2",
                INSPECTOR_2_COMMENTS_FILE,
                round_num,
                rc2,
                stderr2,
            )
            _write_round_output(self.working_dir, round_num, "inspector_2", inspector_2_output)
            _write_living_doc(self.working_dir, INSPECTOR_2_COMMENTS_FILE,
                              inspector_2_output, round_num, "INSPECTOR 2 REVIEW")
            self.logger.log_round_summary(round_num, "INSPECTOR_2_FAILED")
            round_data["inspector_2_output"] = inspector_2_output
            inspector_2_assessment = self._record_review_assessment(
                self._assess_review_output(
                    "inspector_2",
                    inspector_2_output,
                    INSPECTOR_2_COMMENTS_FILE,
                    round_num,
                )
            )
            round_data["inspector_2_review_assessment"] = _review_assessment_to_dict(inspector_2_assessment)
            self._save_state_now(user_query, round_num, "inspector_2_done")
            if round_num >= self.max_rounds:
                final_output = self._finalize_result(
                    user_query,
                    STATUS_MAX_ROUNDS_UNAPPROVED,
                    blocking_reasons=[
                        f"Inspector 2 produced no usable output in final round {round_num}; "
                        f"{INSPECTOR_2_COMMENTS_FILE} records NEEDS_REVISION feedback."
                    ],
                    current_round=round_num,
                )
                return "break", final_output, STATUS_MAX_ROUNDS_UNAPPROVED
            return "continue", "", STATUS_FAILED_NONRESUMABLE

        inspector_2_blocked = _parse_blocked_output(inspector_2_output, "inspector_2", round_num)
        if inspector_2_blocked:
            self.blocked_state = inspector_2_blocked
            _write_round_output(self.working_dir, round_num, "inspector_2", inspector_2_output)
            _write_living_doc(self.working_dir, INSPECTOR_2_COMMENTS_FILE,
                              inspector_2_output, round_num, "INSPECTOR 2 REVIEW")
            round_data["inspector_2_output"] = inspector_2_output
            round_data["blocked"] = inspector_2_blocked
            final_output = self._finalize_result(
                user_query,
                inspector_2_blocked["status"],
                blocking_reasons=[inspector_2_blocked["reason"]],
                blocked=inspector_2_blocked,
                current_round=round_num,
            )
            return "break", final_output, inspector_2_blocked["status"]

        _write_round_output(self.working_dir, round_num, "inspector_2", inspector_2_output)
        _write_living_doc(self.working_dir, INSPECTOR_2_COMMENTS_FILE,
                          inspector_2_output, round_num, "INSPECTOR 2 REVIEW")
        round_data["inspector_2_output"] = inspector_2_output
        self.ui.status(
            f"Inspector 2 finished ({duration2:.0f}s, {len(inspector_2_output)} chars)",
            C_MAGENTA)
        inspector_2_assessment = self._record_review_assessment(
            self._assess_review_output(
                "inspector_2",
                inspector_2_output,
                INSPECTOR_2_COMMENTS_FILE,
                round_num,
            )
        )
        round_data["inspector_2_review_assessment"] = _review_assessment_to_dict(inspector_2_assessment)
        if not self._write_current_session_request(user_query):
            final_output = self._finalize_result(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[
                    f"Could not update {SESSION_REQUEST_FILE} with Inspector 2 review context."
                ],
                current_round=round_num,
            )
            return "break", final_output, STATUS_FAILED_NONRESUMABLE
        self._save_state_now(user_query, round_num, "inspector_2_done")

        if inspector_2_assessment.status in (REVIEW_STATUS_MALFORMED, REVIEW_STATUS_STALE):
            reasons = inspector_2_assessment.malformed_reasons + inspector_2_assessment.stale_reasons
            final_output = self._finalize_result(
                user_query,
                STATUS_REVIEW_FAILED,
                blocking_reasons=reasons or ["Inspector 2 review did not satisfy the Markdown review contract."],
                current_round=round_num,
            )
            return "break", final_output, STATUS_REVIEW_FAILED

        verdict = inspector_2_assessment.verdict or REVIEW_VERDICT_NEEDS_REVISION
        self.ui.verdict_display(verdict, round_num, self.max_rounds)
        self.logger.log_round_summary(round_num, "INSPECTOR_2_" + verdict)

        if verdict == REVIEW_VERDICT_APPROVED:
            gate = self._build_final_approval_assessment(
                STATUS_APPROVED,
                allow_second_inspector_waiver=False,
            )
            if not gate.approved:
                if self._final_gate_should_continue(gate, round_num):
                    self.logger.log_master(
                        "SYSTEM",
                        "Final approval gate found retryable issue/excellence blockers; routing back to Researcher.",
                    )
                    return "continue", "", STATUS_FAILED_NONRESUMABLE
                final_output = self._finalize_result(
                    user_query,
                    gate.status,
                    blocking_reasons=gate.blocking_reasons,
                    current_round=round_num,
                )
                self.logger.log_session_end(final_output, round_num, gate.status)
                self.ui.final_result(round_num, self.logger.session_dir)
                return "return", final_output, gate.status
            final_output = self._finalize_result(
                user_query,
                gate.status,
                blocking_reasons=gate.waivers,
                current_round=round_num,
            )
            self.ui.status(f"Both inspectors APPROVED after {round_num} round(s)!", C_GREEN)
            self.logger.log_session_end(final_output, round_num, gate.status)
            self.ui.final_result(round_num, self.logger.session_dir)
            return "return", final_output, gate.status

        return "continue", "", STATUS_FAILED_NONRESUMABLE

    def _run_core(self, user_query: str, start_round: int = 1,
                  initial_substep: str = "",
                  source_files: list[str] | None = None,
                  required_source_files: list[str] | None = None,
                  saved_source_manifest: list[dict] | None = None,
                  source_context_version: int | None = None,
                  saved_issue_state: dict | None = None,
                  output_files: list[str] | None = None,
                  resume_comment: str | None = None,
                  saved_user_updates: list[dict] | None = None) -> str:
        """
        Run the full Researcher-Inspector loop. Returns a session summary string.

        start_round      : Resume from this round (1 = fresh start).
        initial_substep  : "researcher_done" -> skip researcher on start_round.
        source_files     : Fresh caller-provided direct inputs, or saved state compatibility list.
        required_source_files: New-version resume/direct required input metadata.
        output_files     : Required deliverable files for the session.
        """
        self._active_user_query = user_query
        self._active_round = start_round
        if saved_issue_state and not self.issue_records:
            self.issue_records = _issue_records_from_state(saved_issue_state)
            self.review_summary = self._review_summary_for_state()
        self.logger.log_user_query(user_query)

        try:
            if output_files is not None:
                self.output_files = _normalize_output_file_list(list(output_files), self.working_dir)
            elif self.output_files:
                self.output_files = _normalize_output_file_list(self.output_files, self.working_dir)
            self.result_file = _normalize_result_file_path(
                self.working_dir,
                self.result_file,
                output_files=self.output_files,
            )
            if not self.output_files:
                raise ValueError("Congress requires at least one output file for this workflow.")
        except ValueError as exc:
            msg = str(exc)
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_without_workspace_writes(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[msg],
            )
            self.logger.log_session_end(final_output, 0, STATUS_FAILED_NONRESUMABLE)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        resume_saved_state = _load_state(self.working_dir, normalize=True) if initial_substep else None

        try:
            self._acquire_workspace_lock()
        except WorkspaceLockError as exc:
            msg = str(exc)
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_without_workspace_writes(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[msg],
            )
            self.logger.log_session_end(final_output, 0, STATUS_FAILED_NONRESUMABLE)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        managed_errors = _managed_artifact_path_errors(self.working_dir)
        if managed_errors:
            msg = "Congress-managed artifact path conflict: " + "; ".join(managed_errors)
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_result(
                user_query,
                STATUS_INTERNAL_ERROR,
                blocking_reasons=managed_errors,
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, STATUS_INTERNAL_ERROR)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        self.logger.log_output_files(self.output_files)
        if saved_user_updates and not self.user_updates:
            self.user_updates = list(saved_user_updates)
        if resume_comment:
            self._record_user_update(
                resume_comment,
                "resume-comment",
                user_query=None,
                current_round=start_round,
                persist=False,
            )

        caller_required_sources: list[str] = []
        if required_source_files is not None:
            caller_required_sources = list(required_source_files)
        elif source_files and (start_round <= 1 or source_context_version == SOURCE_CONTEXT_VERSION):
            caller_required_sources = list(source_files)
        elif source_files:
            self.logger.log_master(
                "SYSTEM",
                f"Demoted {len(source_files)} legacy saved source_files entries to optional context during Phase 19 source-context rebuild.",
            )

        self.source_manifest = _build_source_manifest(
            self.working_dir,
            excluded_relpaths=self.output_files,
            user_query=user_query,
            output_files=self.output_files,
            caller_source_files=caller_required_sources,
        )
        if saved_source_manifest and source_context_version == SOURCE_CONTEXT_VERSION and not self.source_manifest:
            self.source_manifest = list(saved_source_manifest)
        self.required_source_files = [
            item["path"]
            for item in self.source_manifest
            if item.get("required") and item.get("path")
        ]
        self.source_files = list(self.required_source_files)

        self.preflight_requirements = _extract_preflight_requirements(
            user_query,
            output_files=self.output_files,
            source_manifest=self.source_manifest,
            working_dir=self.working_dir,
        )
        self.capability_inventory = _collect_capability_inventory(
            self.working_dir,
            self.codex_bin,
            self.preflight_requirements,
        )

        if not self._write_current_session_request(user_query):
            msg = (
                f"Could not write {SESSION_REQUEST_FILE}. "
                "Congress will not continue with a stale session request file."
            )
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_result(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[msg],
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, STATUS_FAILED_NONRESUMABLE)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        self.preflight_state = _run_preflight_requirements(self.preflight_requirements)
        self.capability_inventory["preflight_results"] = self.preflight_state.get("results", [])
        if not self._write_current_session_request(user_query):
            msg = (
                f"Could not update {SESSION_REQUEST_FILE} with preflight results. "
                "Congress will not continue with stale preflight context."
            )
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_result(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[msg],
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, STATUS_FAILED_NONRESUMABLE)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output
        self.logger.log_master("SYSTEM", "Capability/preflight inventory attached to session context")
        if self.preflight_state.get("blocked"):
            blocked = self.preflight_state["blocked"]
            self.blocked_state = blocked
            self.ui.error("Congress preflight blocked the run before launching Codex.")
            self.logger.log_master("SYSTEM", f"Preflight blocked: {blocked.get('reason')}")
            final_output = self._finalize_result(
                user_query,
                blocked.get("status", STATUS_BLOCKED_NEEDS_ENVIRONMENT),
                blocking_reasons=[blocked.get("reason", "Preflight blocked the run.")],
                blocked=blocked,
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, blocked.get("status", STATUS_BLOCKED_NEEDS_ENVIRONMENT))
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        self._classify_task(user_query)
        baseline_summary = self._run_verification_phase(user_query, VERIFICATION_PHASE_BASELINE)
        if not self._write_current_session_request(user_query):
            msg = (
                f"Could not update {SESSION_REQUEST_FILE} with baseline verification. "
                "Congress will not continue with stale verification context."
            )
            self.ui.error(msg)
            self.logger.log_master("SYSTEM", msg)
            final_output = self._finalize_result(
                user_query,
                STATUS_FAILED_NONRESUMABLE,
                blocking_reasons=[msg],
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, STATUS_FAILED_NONRESUMABLE)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output
        baseline_status, baseline_reasons, baseline_blocked = self._verification_terminal_blocker(baseline_summary)
        if baseline_status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT):
            self.blocked_state = baseline_blocked
            final_output = self._finalize_result(
                user_query,
                baseline_status,
                blocking_reasons=baseline_reasons,
                blocked=baseline_blocked,
                current_round=start_round,
            )
            self.logger.log_session_end(final_output, 0, baseline_status)
            self.ui.final_result(0, self.logger.session_dir)
            return final_output

        self._restore_resume_context_from_state(
            resume_saved_state,
            user_query,
            start_round,
            initial_substep,
        )

        optional_context_count = sum(1 for item in self.source_manifest if item.get("optional"))
        excluded_context_count = sum(1 for item in self.source_manifest if item.get("excluded"))
        if self.required_source_files:
            self.ui.status(f"Required source/input files ({len(self.required_source_files)}):", C_GREEN)
            for sf in self.required_source_files:
                self.ui.status(f"  {_relpath_from_workspace(self.working_dir, sf)}", C_DIM)
        else:
            self.ui.status("No required source/input files detected from the user prompt or direct inputs.", C_DIM)
        if optional_context_count or excluded_context_count:
            self.ui.status(
                f"Optional source context available: {optional_context_count} file(s); "
                f"excluded by default: {excluded_context_count} item(s).",
                C_DIM,
            )

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
        session_status = STATUS_FAILED_NONRESUMABLE
        resume_at_inspector_2 = (initial_substep == "inspector_2_running")
        skip_researcher = initial_substep in ("researcher_done", "inspector_2_running")

        for round_num in range(start_round, self.max_rounds + 1):
            self._active_round = round_num
            round_data = {"round": round_num}

            if skip_researcher:
                loaded = _read_round_output(self.working_dir, round_num, "researcher")
                if loaded:
                    researcher_output = loaded
                    round_data["researcher_output"] = researcher_output
                    _write_living_doc(self.working_dir, RESEARCHER_UPDATED_FILE,
                                      researcher_output, round_num, "RESEARCHER NOTES")
                    self._record_researcher_issue_claims(researcher_output, round_num)
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
                    final_output = self._finalize_result(
                        user_query,
                        STATUS_INTERRUPTED_RESUMABLE,
                        blocking_reasons=[
                            "Researcher was interrupted; resume will rerun the Researcher for this round."
                        ],
                        current_round=round_num,
                        resume_substep="researcher_running",
                    )
                    session_status = STATUS_INTERRUPTED_RESUMABLE
                    break

                researcher_output = stdout.strip()
                researcher_blocked = _parse_blocked_output(researcher_output, "researcher", round_num)
                if researcher_blocked:
                    self.blocked_state = researcher_blocked
                    round_data["researcher_output"] = researcher_output
                    round_data["blocked"] = researcher_blocked
                    self.round_history.append(round_data)
                    _write_round_output(self.working_dir, round_num, "researcher", researcher_output)
                    _write_living_doc(self.working_dir, RESEARCHER_UPDATED_FILE,
                                      researcher_output, round_num, "RESEARCHER NOTES")
                    final_output = self._finalize_result(
                        user_query,
                        researcher_blocked["status"],
                        blocking_reasons=[researcher_blocked["reason"]],
                        blocked=researcher_blocked,
                        current_round=round_num,
                    )
                    session_status = researcher_blocked["status"]
                    break

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
                self._record_researcher_issue_claims(researcher_output, round_num)

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
                    if round_num >= self.max_rounds:
                        session_status = STATUS_MAX_ROUNDS_UNAPPROVED
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_MAX_ROUNDS_UNAPPROVED,
                            blocking_reasons=[
                                "Maximum rounds reached with requested output file issues: "
                                + "; ".join(problem_parts)
                            ],
                            current_round=round_num,
                        )
                        break

                    repair_instruction = self._build_missing_output_repair_instruction(
                        round_num,
                        missing_outputs,
                        invalid_outputs,
                    )
                    self._record_user_update(
                        repair_instruction,
                        "congress_repair",
                        user_query=user_query,
                        current_round=round_num + 1,
                        persist=True,
                    )
                    self._save_retry_state(
                        user_query,
                        round_num + 1,
                        "researcher_running",
                        "researcher",
                        "missing_or_invalid_outputs",
                        session_id=self.researcher_session_id,
                        stdout=researcher_output,
                        stderr=stderr,
                        output_status=after_status,
                    )
                    try:
                        action = self._transition_menu("Researcher", round_num + 1, user_query=user_query)
                    except TypeError:
                        action = self._transition_menu("Researcher", round_num + 1)
                    if action == "output":
                        final_output = self._finalize_output(user_query, researcher_output)
                        session_status = STATUS_OUTPUT_EARLY
                        break
                    if action == "quit":
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_QUIT_BY_USER,
                            blocking_reasons=["User quit before requested output repair was completed."],
                            current_round=round_num,
                        )
                        session_status = STATUS_QUIT_BY_USER
                        break
                    self.ui.status(
                        f"Routing missing/invalid outputs to Researcher round {round_num + 1}.",
                        C_YELLOW,
                    )
                    time.sleep(COOLDOWN_BETWEEN)
                    continue

                changed_outputs = output_summary.get("created", []) + output_summary.get("updated", [])
                self._classify_task(user_query, changed_files=changed_outputs)
                post_summary = self._run_verification_phase(
                    user_query,
                    VERIFICATION_PHASE_POST_CHANGE,
                    changed_files=changed_outputs,
                )
                self._last_post_change_outputs = after_status
                if not self._write_current_session_request(user_query):
                    msg = (
                        f"Could not update {SESSION_REQUEST_FILE} with post-change verification. "
                        "Congress will not continue with stale verification context."
                    )
                    self.ui.error(msg)
                    self.logger.log_master("SYSTEM", msg)
                    round_data["error"] = msg
                    self.round_history.append(round_data)
                    final_output = self._finalize_result(
                        user_query,
                        STATUS_FAILED_NONRESUMABLE,
                        blocking_reasons=[msg],
                        current_round=round_num,
                    )
                    session_status = STATUS_FAILED_NONRESUMABLE
                    break
                post_status, post_reasons, post_blocked = self._verification_terminal_blocker(post_summary)
                if post_status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT):
                    self.blocked_state = post_blocked
                    round_data["verification"] = post_summary
                    self.round_history.append(round_data)
                    final_output = self._finalize_result(
                        user_query,
                        post_status,
                        blocking_reasons=post_reasons,
                        blocked=post_blocked,
                        current_round=round_num,
                    )
                    session_status = post_status
                    break

                self._save_state_now(user_query, round_num, "researcher_done")
                self.ui.status(
                    f"Researcher finished ({duration:.0f}s, {len(researcher_output)} chars of notes)",
                    C_BLUE)

            skip_researcher = False

            if resume_at_inspector_2 and round_num == start_round:
                inspector_output = _read_round_output(self.working_dir, round_num, "inspector")
                if inspector_output and researcher_output:
                    round_data["inspector_output"] = inspector_output
                    _write_living_doc(self.working_dir, INSPECTOR_COMMENTS_FILE,
                                      inspector_output, round_num, "INSPECTOR REVIEW")
                    self.round_history.append(round_data)
                    self.ui.status(
                        f"[Resume] Round {round_num}: Inspector 1 review loaded from disk "
                        f"({len(inspector_output)} chars). Continuing at Inspector 2.",
                        C_GREEN,
                    )
                    self.logger.log_master(
                        "SYSTEM",
                        f"Resume: loaded Inspector 1 review for round {round_num}; continuing at Inspector 2",
                    )

                    final_output_status = _collect_output_status(self.working_dir, self.output_files)
                    invalid_or_missing = [
                        item["path"] for item in final_output_status
                        if item.get("invalid_type") or not item["exists"]
                    ]
                    if invalid_or_missing:
                        self.ui.error(
                            "Inspector 2 resume blocked because requested output files are missing or invalid: "
                            + ", ".join(invalid_or_missing)
                        )
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_FAILED_NONRESUMABLE,
                            blocking_reasons=[
                                "Inspector 2 resume blocked because requested output files are missing or invalid: "
                                + ", ".join(invalid_or_missing)
                            ],
                            current_round=round_num,
                        )
                        session_status = STATUS_FAILED_NONRESUMABLE
                        break

                    resume_changed_files = [
                        item.get("path") for item in final_output_status if item.get("exists")
                    ]
                    if self._has_current_final_verification():
                        final_summary = _verification_summary_for_current_context(self.verification_summary)
                        self.logger.log_master(
                            "SYSTEM",
                            "Resume: reusing saved final verification for unchanged outputs before Inspector 2.",
                        )
                    else:
                        self._run_verification_phase(
                            user_query,
                            VERIFICATION_PHASE_POST_CHANGE,
                            changed_files=resume_changed_files,
                        )
                        final_summary = self._ensure_final_verification_phase(
                            user_query,
                            changed_files=resume_changed_files,
                        )
                    final_status, final_reasons, final_blocked = self._verification_terminal_blocker(final_summary)
                    if final_status:
                        if final_status == STATUS_VERIFICATION_FAILED and round_num < self.max_rounds:
                            self.logger.log_master(
                                "SYSTEM",
                                "Final verification failed after resumed Inspector 1 approval; "
                                "routing the verification evidence back to Researcher.",
                            )
                            continue
                        if final_blocked:
                            self.blocked_state = final_blocked
                        final_output = self._finalize_result(
                            user_query,
                            final_status,
                            blocking_reasons=final_reasons,
                            blocked=final_blocked,
                            current_round=round_num,
                        )
                        session_status = final_status
                        self.logger.log_session_end(final_output, round_num, final_status)
                        self.ui.final_result(round_num, self.logger.session_dir)
                        return final_output

                    inspector_assessment = self._record_review_assessment(
                        self._assess_review_output(
                            "inspector_1",
                            inspector_output,
                            INSPECTOR_COMMENTS_FILE,
                            round_num,
                        )
                    )
                    round_data["inspector_review_assessment"] = _review_assessment_to_dict(inspector_assessment)
                    if not self._write_current_session_request(user_query):
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_FAILED_NONRESUMABLE,
                            blocking_reasons=[
                                f"Could not update {SESSION_REQUEST_FILE} with resumed Inspector 1 review context."
                            ],
                            current_round=round_num,
                        )
                        session_status = STATUS_FAILED_NONRESUMABLE
                        break
                    self._save_state_now(user_query, round_num, "inspector_done")

                    if (
                        inspector_assessment.status in (REVIEW_STATUS_MALFORMED, REVIEW_STATUS_STALE)
                        or inspector_assessment.verdict != REVIEW_VERDICT_APPROVED
                    ):
                        reasons = (
                            inspector_assessment.malformed_reasons
                            + inspector_assessment.stale_reasons
                        )
                        if inspector_assessment.verdict != REVIEW_VERDICT_APPROVED:
                            reasons.append(
                                "Inspector 1 review must be current and approved before resuming Inspector 2."
                            )
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_REVIEW_FAILED,
                            blocking_reasons=reasons or [
                                "Inspector 1 review did not satisfy the resumed Inspector 2 approval contract."
                            ],
                            current_round=round_num,
                        )
                        session_status = STATUS_REVIEW_FAILED
                        break

                    if self.second_inspector_mode == "off":
                        gate = self._build_final_approval_assessment(
                            STATUS_APPROVED_WITH_WAIVER,
                            allow_second_inspector_waiver=True,
                            waiver_reason="Inspector 2 was explicitly waived by --second-inspector=off during resume.",
                        )
                        if not gate.approved and self._final_gate_should_continue(gate, round_num):
                            self.logger.log_master(
                                "SYSTEM",
                                "Final approval gate found retryable issue/excellence blockers during Inspector 2-waived resume; routing back to Researcher.",
                            )
                            continue
                        final_output = self._finalize_result(
                            user_query,
                            gate.status,
                            blocking_reasons=gate.waivers if gate.approved else gate.blocking_reasons,
                            current_round=round_num,
                        )
                        session_status = gate.status
                        self.logger.log_session_end(final_output, round_num, gate.status)
                        self.ui.final_result(round_num, self.logger.session_dir)
                        return final_output

                    inspector_2_action, final_output, session_status = self._run_inspector_2_stage(
                        user_query,
                        researcher_output,
                        inspector_output,
                        round_num,
                        pause_event,
                        round_data,
                    )
                    resume_at_inspector_2 = False
                    if inspector_2_action == "return":
                        return final_output
                    if inspector_2_action == "break":
                        break

                    if round_num == self.max_rounds:
                        self.ui.status(
                            f"Max rounds ({self.max_rounds}) reached. Keeping the latest deliverables on disk.",
                            C_YELLOW)
                        session_status = STATUS_MAX_ROUNDS_UNAPPROVED
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_MAX_ROUNDS_UNAPPROVED,
                            blocking_reasons=["Maximum rounds reached before both inspectors approved."],
                            current_round=round_num,
                        )
                        break

                    try:
                        action = self._transition_menu("Researcher", round_num + 1, user_query=user_query)
                    except TypeError:
                        action = self._transition_menu("Researcher", round_num + 1)
                    if action == "output":
                        final_output = self._finalize_output(user_query, researcher_output)
                        session_status = STATUS_OUTPUT_EARLY
                        break
                    if action == "quit":
                        final_output = self._finalize_result(
                            user_query,
                            STATUS_QUIT_BY_USER,
                            blocking_reasons=["User quit before approval was reached."],
                            current_round=round_num,
                        )
                        session_status = STATUS_QUIT_BY_USER
                        break

                    self.ui.status(f"Preparing round {round_num + 1}...", C_DIM)
                    time.sleep(COOLDOWN_BETWEEN)
                    continue

                self.ui.error(
                    f"Resume: expected round_{round_num}_inspector.md and researcher notes before "
                    "Inspector 2 resume. Falling back to the normal Inspector path."
                )
                self.logger.log_master(
                    "SYSTEM",
                    f"Inspector 2 resume artifacts missing for round {round_num}; falling back to Inspector 1",
                )
                resume_at_inspector_2 = False

            try:
                action = self._transition_menu("Inspector", round_num, user_query=user_query)
            except TypeError:
                action = self._transition_menu("Inspector", round_num)
            if action == "output":
                self.round_history.append(round_data)
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = STATUS_OUTPUT_EARLY
                break
            if action == "quit":
                self.round_history.append(round_data)
                final_output = self._finalize_result(
                    user_query,
                    STATUS_QUIT_BY_USER,
                    blocking_reasons=["User quit before approval was reached."],
                    current_round=round_num,
                )
                session_status = STATUS_QUIT_BY_USER
                break

            pre_inspector_output_status = _collect_output_status(self.working_dir, self.output_files)
            invalid_or_missing = [
                item["path"] for item in pre_inspector_output_status
                if item.get("invalid_type") or not item["exists"]
            ]
            if invalid_or_missing:
                self.ui.error(
                    "Inspector review blocked because requested output files are missing or invalid: "
                    + ", ".join(invalid_or_missing)
                )
                final_output = self._finalize_result(
                    user_query,
                    STATUS_FAILED_NONRESUMABLE,
                    blocking_reasons=[
                        "Inspector review blocked because requested output files are missing or invalid: "
                        + ", ".join(invalid_or_missing)
                    ],
                    current_round=round_num,
                )
                session_status = STATUS_FAILED_NONRESUMABLE
                break

            pre_inspector_final_summary = self._ensure_final_verification_phase(
                user_query,
                changed_files=[item.get("path") for item in pre_inspector_output_status if item.get("exists")],
            )
            final_status, final_reasons, final_blocked = self._verification_terminal_blocker(
                pre_inspector_final_summary
            )
            if final_status in (STATUS_BLOCKED_NEEDS_USER, STATUS_BLOCKED_NEEDS_ENVIRONMENT):
                if final_blocked:
                    self.blocked_state = final_blocked
                final_output = self._finalize_result(
                    user_query,
                    final_status,
                    blocking_reasons=final_reasons,
                    blocked=final_blocked,
                    current_round=round_num,
                )
                session_status = final_status
                self.logger.log_session_end(final_output, round_num, final_status)
                self.ui.final_result(round_num, self.logger.session_dir)
                return final_output
            if final_status:
                self.logger.log_master(
                    "SYSTEM",
                    "Final verification is not approval-ready before Inspector 1; "
                    "continuing so the Inspector can review the evidence.",
                )

            if not self._write_current_session_request(user_query):
                msg = (
                    f"Could not update {SESSION_REQUEST_FILE} with final verification before Inspector 1. "
                    "Congress will not continue with stale final verification context."
                )
                self.ui.error(msg)
                self.logger.log_master("SYSTEM", msg)
                final_output = self._finalize_result(
                    user_query,
                    STATUS_FAILED_NONRESUMABLE,
                    blocking_reasons=[msg],
                    current_round=round_num,
                )
                session_status = STATUS_FAILED_NONRESUMABLE
                break
            self._save_state_now(user_query, round_num, "researcher_done")

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
                round_data["inspector_output"] = ""
                round_data["error"] = "interrupted"
                self.round_history.append(round_data)
                final_output = self._finalize_result(
                    user_query,
                    STATUS_INTERRUPTED_RESUMABLE,
                    blocking_reasons=[
                        "Inspector was interrupted; resume will continue from the Inspector for this round."
                    ],
                    current_round=round_num,
                    resume_substep="researcher_done",
                )
                session_status = STATUS_INTERRUPTED_RESUMABLE
                break

            inspector_output = stdout.strip()
            if not inspector_output:
                self.ui.error(
                    f"Inspector produced no usable output (rc={rc}). "
                    f"Writing {INSPECTOR_COMMENTS_FILE} with NEEDS_REVISION feedback.")
                self.logger.log_master("SYSTEM",
                                       f"Inspector no-output fallback, rc={rc}: {stderr[:200]}")
                inspector_output = self._build_no_output_review_fallback(
                    "Inspector",
                    INSPECTOR_COMMENTS_FILE,
                    round_num,
                    rc,
                    stderr,
                )
                _write_round_output(self.working_dir, round_num, "inspector", inspector_output)
                _write_living_doc(self.working_dir, INSPECTOR_COMMENTS_FILE,
                                  inspector_output, round_num, "INSPECTOR REVIEW")
                self.logger.log_round_summary(round_num, "INSPECTOR_FAILED")
                round_data["inspector_output"] = inspector_output
                inspector_assessment = self._record_review_assessment(
                    self._assess_review_output(
                        "inspector_1",
                        inspector_output,
                        INSPECTOR_COMMENTS_FILE,
                        round_num,
                    )
                )
                round_data["inspector_review_assessment"] = _review_assessment_to_dict(inspector_assessment)
                self.round_history.append(round_data)
                self._save_state_now(user_query, round_num, "inspector_done")
                if round_num >= self.max_rounds:
                    final_output = self._finalize_result(
                        user_query,
                        STATUS_MAX_ROUNDS_UNAPPROVED,
                        blocking_reasons=[
                            f"Inspector produced no usable output in final round {round_num}; "
                            f"{INSPECTOR_COMMENTS_FILE} records NEEDS_REVISION feedback."
                        ],
                        current_round=round_num,
                    )
                    session_status = STATUS_MAX_ROUNDS_UNAPPROVED
                    break

                try:
                    action = self._transition_menu("Researcher", round_num + 1, user_query=user_query)
                except TypeError:
                    action = self._transition_menu("Researcher", round_num + 1)
                if action == "output":
                    final_output = self._finalize_output(user_query, researcher_output)
                    session_status = STATUS_OUTPUT_EARLY
                    break
                if action == "quit":
                    final_output = self._finalize_result(
                        user_query,
                        STATUS_QUIT_BY_USER,
                        blocking_reasons=["User quit before Inspector no-output feedback was addressed."],
                        current_round=round_num,
                    )
                    session_status = STATUS_QUIT_BY_USER
                    break
                self.ui.status(
                    f"Routing Inspector no-output feedback to Researcher round {round_num + 1}.",
                    C_YELLOW,
                )
                time.sleep(COOLDOWN_BETWEEN)
                continue

            inspector_blocked = _parse_blocked_output(inspector_output, "inspector", round_num)
            if inspector_blocked:
                self.blocked_state = inspector_blocked
                _write_round_output(self.working_dir, round_num, "inspector", inspector_output)
                _write_living_doc(self.working_dir, INSPECTOR_COMMENTS_FILE,
                                  inspector_output, round_num, "INSPECTOR REVIEW")
                round_data["inspector_output"] = inspector_output
                round_data["blocked"] = inspector_blocked
                self.round_history.append(round_data)
                final_output = self._finalize_result(
                    user_query,
                    inspector_blocked["status"],
                    blocking_reasons=[inspector_blocked["reason"]],
                    blocked=inspector_blocked,
                    current_round=round_num,
                )
                session_status = inspector_blocked["status"]
                break

            _write_round_output(self.working_dir, round_num, "inspector", inspector_output)
            _write_living_doc(self.working_dir, INSPECTOR_COMMENTS_FILE,
                              inspector_output, round_num, "INSPECTOR REVIEW")
            round_data["inspector_output"] = inspector_output
            self.round_history.append(round_data)

            self.ui.status(
                f"Inspector finished ({duration:.0f}s, {len(inspector_output)} chars)",
                C_MAGENTA)

            inspector_assessment = self._record_review_assessment(
                self._assess_review_output(
                    "inspector_1",
                    inspector_output,
                    INSPECTOR_COMMENTS_FILE,
                    round_num,
                )
            )
            round_data["inspector_review_assessment"] = _review_assessment_to_dict(inspector_assessment)
            self._save_state_now(user_query, round_num + 1, "")

            if inspector_assessment.status in (REVIEW_STATUS_MALFORMED, REVIEW_STATUS_STALE):
                reasons = inspector_assessment.malformed_reasons + inspector_assessment.stale_reasons
                final_output = self._finalize_result(
                    user_query,
                    STATUS_REVIEW_FAILED,
                    blocking_reasons=reasons or ["Inspector 1 review did not satisfy the Markdown review contract."],
                    current_round=round_num,
                )
                session_status = STATUS_REVIEW_FAILED
                break

            verdict = inspector_assessment.effective_verdict or inspector_assessment.verdict or REVIEW_VERDICT_NEEDS_REVISION
            raw_verdict = inspector_assessment.raw_verdict or inspector_assessment.verdict
            if raw_verdict and raw_verdict != verdict:
                self.logger.log_master(
                    "SYSTEM",
                    f"Inspector 1 raw verdict {raw_verdict} converted to effective verdict {verdict} "
                    f"by quality_mode={self.quality_mode}.",
                )
            self.ui.verdict_display(verdict, round_num, self.max_rounds)
            self.logger.log_round_summary(round_num, verdict)

            if verdict == REVIEW_VERDICT_APPROVED:
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
                    session_status = STATUS_FAILED_NONRESUMABLE
                    final_output = self._finalize_result(
                        user_query,
                        STATUS_FAILED_NONRESUMABLE,
                        blocking_reasons=[
                            "Approval blocked because requested output files are missing or invalid: "
                            + ", ".join(invalid_or_missing)
                        ],
                        current_round=round_num,
                    )
                    break
                final_summary = self._ensure_final_verification_phase(
                    user_query,
                    changed_files=[item.get("path") for item in final_output_status if item.get("exists")],
                )
                final_status, final_reasons, final_blocked = self._verification_terminal_blocker(final_summary)
                if final_status:
                    if final_status == STATUS_VERIFICATION_FAILED and round_num < self.max_rounds:
                        self.logger.log_master(
                            "SYSTEM",
                            "Final verification failed after Inspector 1 approval; "
                            "routing the verification evidence back to Researcher.",
                        )
                        continue
                    if final_blocked:
                        self.blocked_state = final_blocked
                    final_output = self._finalize_result(
                        user_query,
                        final_status,
                        blocking_reasons=final_reasons,
                        blocked=final_blocked,
                        current_round=round_num,
                    )
                    session_status = final_status
                    self.logger.log_session_end(final_output, round_num, final_status)
                    self.ui.final_result(round_num, self.logger.session_dir)
                    return final_output

                if self.second_inspector_mode == "off":
                    gate = self._build_final_approval_assessment(
                        STATUS_APPROVED_WITH_WAIVER,
                        allow_second_inspector_waiver=True,
                        waiver_reason="Inspector 2 was explicitly waived by --second-inspector=off.",
                    )
                    if not gate.approved and self._final_gate_should_continue(gate, round_num):
                        self.logger.log_master(
                            "SYSTEM",
                            "Final approval gate found retryable issue/excellence blockers with Inspector 2 waived; routing back to Researcher.",
                        )
                        continue
                    final_output = self._finalize_result(
                        user_query,
                        gate.status,
                        blocking_reasons=gate.waivers if gate.approved else gate.blocking_reasons,
                        current_round=round_num,
                    )
                    session_status = gate.status
                    self.logger.log_session_end(final_output, round_num, gate.status)
                    self.ui.final_result(round_num, self.logger.session_dir)
                    return final_output

                inspector_2_action, final_output, session_status = self._run_inspector_2_stage(
                    user_query,
                    researcher_output,
                    inspector_output,
                    round_num,
                    pause_event,
                    round_data,
                )
                if inspector_2_action == "return":
                    return final_output
                if inspector_2_action == "break":
                    break

            if round_num == self.max_rounds:
                self.ui.status(
                    f"Max rounds ({self.max_rounds}) reached. Keeping the latest deliverables on disk.",
                    C_YELLOW)
                session_status = STATUS_MAX_ROUNDS_UNAPPROVED
                max_round_reasons = ["Maximum rounds reached before both inspectors approved."]
                quality_blockers: list[str] = []
                for latest in (
                    _review_assessment_to_dict(inspector_assessment),
                    _latest_review_assessment(self.review_assessments, "inspector_2"),
                ):
                    if latest:
                        reviewer_label = "Inspector 2" if latest.get("reviewer") == "inspector_2" else "Inspector 1"
                        for reason in latest.get("quality_blocking_reasons") or []:
                            quality_blockers.append(f"{reviewer_label} quality policy blocker: {reason}")
                if quality_blockers:
                    max_round_reasons = [
                        "Maximum rounds reached with unresolved best-output quality blockers.",
                        *quality_blockers,
                    ]
                final_output = self._finalize_result(
                    user_query,
                    STATUS_MAX_ROUNDS_UNAPPROVED,
                    blocking_reasons=max_round_reasons,
                    current_round=round_num,
                )
                break

            try:
                action = self._transition_menu("Researcher", round_num + 1, user_query=user_query)
            except TypeError:
                action = self._transition_menu("Researcher", round_num + 1)
            if action == "output":
                final_output = self._finalize_output(user_query, researcher_output)
                session_status = STATUS_OUTPUT_EARLY
                break
            if action == "quit":
                final_output = self._finalize_result(
                    user_query,
                    STATUS_QUIT_BY_USER,
                    blocking_reasons=["User quit before approval was reached."],
                    current_round=round_num,
                )
                session_status = STATUS_QUIT_BY_USER
                break

            self.ui.status(f"Preparing round {round_num + 1}...", C_DIM)
            time.sleep(COOLDOWN_BETWEEN)

        if not final_output:
            final_output = self._finalize_result(
                user_query,
                session_status,
                blocking_reasons=[] if session_status in APPROVED_STATUSES else [f"Terminal status: {session_status}"],
            )

        total = len(self.round_history) or 1
        self.logger.log_session_end(final_output, total, session_status)
        self.ui.final_result(total, self.logger.session_dir)

        return final_output


# ============================================================================
# INTERACTIVE LOOP
# ============================================================================

def interactive_mode(max_rounds: int, codex_bin: str | None, working_dir: str,
                     verification_mode: str = DEFAULT_VERIFICATION_MODE,
                     max_verification_timeout: int = DEFAULT_MAX_VERIFICATION_TIMEOUT,
                     second_inspector_mode: str = DEFAULT_SECOND_INSPECTOR_MODE,
                     log_prompts_mode: str = DEFAULT_LOG_PROMPTS_MODE,
                     quality_mode: str = DEFAULT_QUALITY_MODE,
                     quality_mode_explicit: bool = False,
                     result_file: str = CONGRESS_RESULT_FILE,
                     strict_exit_codes: bool = True,
                     ci_mode: bool = DEFAULT_INTERACTIVE_CI_MODE,
                     ci_mode_explicit: bool | None = None):
    if ci_mode_explicit is None:
        ci_mode_explicit = not ci_mode
    interactive_requested = bool(ci_mode_explicit and not ci_mode)
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
    ui.status(f"Quality:    {quality_mode}", C_GREEN)
    ui.status(f"Working dir:{working_dir}", C_GREEN)
    ui.status(f"Logs:       {LOG_DIR}", C_GREEN)
    print()

    while True:
        try:
            saved = _load_state(working_dir, normalize=True)

            if _state_is_resumable(saved):
                resume_validation = _validate_resume_state(saved, working_dir)
                saved_version = saved.get("state_version", 1)
                saved_round   = saved.get("current_round", 1)
                saved_substep = saved.get("current_substep", "")
                saved_query   = saved.get("user_query", "")
                saved_max     = saved.get("max_rounds", max_rounds)
                saved_srcs    = saved.get("source_files", [])
                saved_outputs_raw = saved.get("output_files", [])
                saved_outputs = resume_validation["output_files"]
                resume_errors = resume_validation["errors"]
                resume_allowed = saved_version >= STATE_VERSION and resume_validation["ok"]

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
                    for item in resume_errors:
                        ui.status(f"  Reason: {item}", C_DIM)
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
                    session_quality_mode = (
                        _normalize_quality_mode(quality_mode)
                        if quality_mode_explicit
                        else _normalize_quality_mode(saved.get("quality_mode"), strict=False)
                    )
                    congress = Congress(
                        max_rounds=saved_max,
                        codex_bin_resolved=codex_path,
                        working_dir=working_dir,
                        verification_mode=verification_mode,
                        max_verification_timeout=max_verification_timeout,
                        second_inspector_mode=second_inspector_mode,
                        log_prompts_mode=log_prompts_mode,
                        quality_mode=session_quality_mode,
                        result_file=result_file,
                        strict_exit_codes=strict_exit_codes,
                        ci_mode=ci_mode,
                        ci_mode_explicit=ci_mode_explicit,
                        interactive_requested=interactive_requested,
                    )
                    congress.researcher_session_id = saved.get("researcher_session_id")
                    congress.inspector_session_id = saved.get("inspector_session_id")
                    congress.inspector_2_session_id = saved.get("inspector_2_session_id")
                    resume_note = congress._prompt_for_resume_note()
                    final = congress.run(
                        saved_query,
                        start_round=saved_round,
                        initial_substep=saved_substep,
                        source_files=saved_srcs,
                        required_source_files=saved.get("required_source_files"),
                        saved_source_manifest=saved.get("source_manifest"),
                        source_context_version=saved.get("source_context_version"),
                        saved_issue_state=saved.get("issue_state"),
                        output_files=saved_outputs,
                        resume_comment=resume_note or None,
                        saved_user_updates=saved.get("user_updates", []),
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
                verification_mode=verification_mode,
                max_verification_timeout=max_verification_timeout,
                second_inspector_mode=second_inspector_mode,
                log_prompts_mode=log_prompts_mode,
                quality_mode=quality_mode,
                result_file=result_file,
                strict_exit_codes=strict_exit_codes,
                ci_mode=ci_mode,
                ci_mode_explicit=ci_mode_explicit,
                interactive_requested=interactive_requested,
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


def _normalize_cli_args(raw_args: list[str]) -> list[str]:
    normalized: list[str] = []
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg == "--query":
            if i + 1 >= len(raw_args):
                normalized.append(arg)
                i += 1
                continue
            parts: list[str] = []
            i += 1
            while i < len(raw_args) and not raw_args[i].startswith("--"):
                parts.append(raw_args[i])
                i += 1
            normalized.append("--query=" + " ".join(parts).strip())
            continue
        if arg.startswith("--query="):
            parts = [arg.split("=", 1)[1]]
            i += 1
            while i < len(raw_args) and not raw_args[i].startswith("--"):
                parts.append(raw_args[i])
                i += 1
            normalized.append("--query=" + " ".join(parts).strip())
            continue
        normalized.append(arg)
        i += 1
    return normalized


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
    resume      = False
    resume_comment = None
    verification_mode = DEFAULT_VERIFICATION_MODE
    max_verification_timeout = DEFAULT_MAX_VERIFICATION_TIMEOUT
    second_inspector_mode = DEFAULT_SECOND_INSPECTOR_MODE
    log_prompts_mode = DEFAULT_LOG_PROMPTS_MODE
    quality_mode = DEFAULT_QUALITY_MODE
    quality_mode_explicit = False
    result_file = CONGRESS_RESULT_FILE
    strict_exit_codes = True
    ci_mode = True
    ci_mode_explicit = False

    for arg in _normalize_cli_args(sys.argv[1:]):
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
                SILENCE_TIMEOUT = max(3600, int(arg.split("=", 1)[1]))
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
        elif arg == "--resume":
            resume = True
        elif arg.startswith("--resume-comment="):
            resume_comment = arg.split("=", 1)[1].strip().strip('"')
        elif arg.startswith("--verification="):
            verification_mode = arg.split("=", 1)[1].strip().strip('"').lower()
            if verification_mode not in VERIFICATION_MODES:
                print(
                    "ERROR: Invalid --verification mode. Expected auto, required, or off.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--max-verification-timeout="):
            raw_timeout = arg.split("=", 1)[1].strip().strip('"')
            try:
                max_verification_timeout = int(raw_timeout)
                if max_verification_timeout < MIN_MAX_VERIFICATION_TIMEOUT:
                    raise ValueError
            except ValueError:
                print(
                    "ERROR: Invalid --max-verification-timeout. Expected a positive integer number of seconds.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--second-inspector="):
            second_inspector_mode = arg.split("=", 1)[1].strip().strip('"').lower()
            if second_inspector_mode not in SECOND_INSPECTOR_MODES:
                print(
                    "ERROR: Invalid --second-inspector mode. Expected auto, on, or off.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--log-prompts="):
            log_prompts_mode = arg.split("=", 1)[1].strip().strip('"').lower()
            if log_prompts_mode not in LOG_PROMPTS_MODES:
                print(
                    "ERROR: Invalid --log-prompts mode. Expected full, redacted, or off.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--quality-mode="):
            raw_quality_mode = arg.split("=", 1)[1].strip().strip('"')
            try:
                quality_mode = _normalize_quality_mode(raw_quality_mode)
                quality_mode_explicit = True
            except ValueError:
                print(
                    "ERROR: Invalid --quality-mode. Expected best or standard.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--result-file="):
            result_file = arg.split("=", 1)[1].strip().strip('"')
        elif arg == "--strict-exit-codes":
            strict_exit_codes = True
        elif arg == "--ci":
            ci_mode = True
            ci_mode_explicit = True
        elif arg in ("--interactive", "--no-ci"):
            ci_mode = False
            ci_mode_explicit = True
        elif arg in ("--help", "-h"):
            print("CONGRESS v2 - Multi-AI Debate System")
            print()
            print("Usage: python congress2.py [options]")
            print()
            print("Options:")
            print("  --max-rounds=N     Max debate rounds (default: 3)")
            print("  --timeout=N        Silence timeout in seconds (default/minimum: 3600)")
            print("  --codex-bin=PATH   Path to codex binary")
            print("  --workdir=PATH     Working directory for codex (default: cwd)")
            print("  --outputs=FILES    Comma-separated required output files")
            print("  --query=\"...\"      Run a single query non-interactively")
            print("  --query \"...\"      Alternate query form; split query text is reconstructed")
            print("  --resume           Resume a saved resumable session")
            print("  --resume-comment=T Add a sanitized user update before resume")
            print("  --verification=M   Verification mode: auto, required, off (default: auto)")
            print("  --max-verification-timeout=N")
            print("                     Max Congress verification time in seconds (default: 3600)")
            print("  --second-inspector=M")
            print("                     Inspector 2 mode: auto, on, off (default: auto)")
            print("  --log-prompts=M    Prompt logging: full, redacted, off (default: full)")
            print("  --quality-mode=M   Quality policy: best, standard (default: best)")
            print("  --result-file=PATH Custom final result Markdown path inside workdir")
            print("  --strict-exit-codes")
            print("                     Use controlled non-interactive exit codes")
            print("  --ci               Use unattended waits/prompts (default for --query/--resume)")
            print("  --interactive, --no-ci")
            print("                     Enable manual transition menus and prompts")
            print("  --approval=FLAG    Codex approval flag")
            print("  --help             Show this help")
            print()
            print("Trust model:")
            print("  Congress runs real full-access Codex agents and real project commands when needed.")
            print("  Final approval depends on requested output files, hashes, verification evidence,")
            print("  Inspector 1 review, Inspector 2 review unless explicitly waived, and blocked-state absence.")
            print("  Managed artifacts include congress_result.md, congress_history.md,")
            print("  congress_verification.md, inspector_comments.md, and inspector_2_comments.md.")
            print("  approved_with_waiver means an explicit waiver, such as --second-inspector=off, was used.")
            sys.exit(0)

    if resume:
        saved = _load_state(working_dir, normalize=True)
        validation = _validate_resume_state(saved, working_dir)
        if not validation["ok"]:
            print("ERROR: Saved session cannot be resumed safely.", file=sys.stderr)
            for item in validation["errors"]:
                print(f"ERROR: {item}", file=sys.stderr)
            sys.exit(1)
        if result_file == CONGRESS_RESULT_FILE and saved.get("result_file"):
            result_file = saved.get("result_file")
        try:
            result_file = _normalize_result_file_path(
                working_dir,
                result_file,
                output_files=validation["output_files"],
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        codex_path = _resolve_codex_binary(codex_bin)
        if not codex_path:
            print("ERROR: Codex CLI not found!")
            sys.exit(1)
        if not ci_mode_explicit:
            ci_mode = False if _state_requests_interactive(saved) else True
            ci_mode_explicit = _state_requests_interactive(saved)
        if not quality_mode_explicit:
            quality_mode = _normalize_quality_mode(saved.get("quality_mode"), strict=False)
        congress = Congress(
            max_rounds=max(_safe_int(saved.get("max_rounds"), max_rounds), max_rounds),
            codex_bin_resolved=codex_path,
            working_dir=working_dir,
            verification_mode=verification_mode,
            max_verification_timeout=max_verification_timeout,
            second_inspector_mode=second_inspector_mode,
            log_prompts_mode=log_prompts_mode,
            quality_mode=quality_mode,
            result_file=result_file,
            strict_exit_codes=strict_exit_codes,
            ci_mode=ci_mode,
            ci_mode_explicit=ci_mode_explicit,
            interactive_requested=not ci_mode if ci_mode_explicit else False,
        )
        resume_start_round = saved.get("current_round", 1)
        resume_initial_substep = saved.get("current_substep", "")
        if (
            saved.get("status") in {STATUS_MAX_ROUNDS_UNAPPROVED, STATUS_REVIEW_FAILED}
            and resume_initial_substep == PHASE_TERMINAL
        ):
            resume_initial_substep = "researcher_done"
        congress.researcher_session_id = saved.get("researcher_session_id")
        congress.inspector_session_id = saved.get("inspector_session_id")
        congress.inspector_2_session_id = saved.get("inspector_2_session_id")
        final = congress.run(
            saved.get("user_query", ""),
            start_round=resume_start_round,
            initial_substep=resume_initial_substep,
            source_files=saved.get("source_files", []),
            required_source_files=saved.get("required_source_files"),
            saved_source_manifest=saved.get("source_manifest"),
            source_context_version=saved.get("source_context_version"),
            saved_issue_state=saved.get("issue_state"),
            output_files=validation["output_files"],
            resume_comment=resume_comment,
            saved_user_updates=saved.get("user_updates", []),
        )
        print()
        if final:
            print(final)
        sys.exit(congress.last_result.exit_code if congress.last_result else 1)

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
        try:
            result_file = _normalize_result_file_path(
                working_dir,
                result_file,
                output_files=output_files,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        codex_path = _resolve_codex_binary(codex_bin)
        if not codex_path:
            print("ERROR: Codex CLI not found!")
            sys.exit(1)
        congress = None
        try:
            congress = Congress(
                max_rounds=max_rounds,
                codex_bin_resolved=codex_path,
                working_dir=working_dir,
                verification_mode=verification_mode,
                max_verification_timeout=max_verification_timeout,
                second_inspector_mode=second_inspector_mode,
                log_prompts_mode=log_prompts_mode,
                quality_mode=quality_mode,
                result_file=result_file,
                strict_exit_codes=strict_exit_codes,
                ci_mode=ci_mode,
                ci_mode_explicit=ci_mode_explicit,
                interactive_requested=not ci_mode if ci_mode_explicit else False,
            )
            final = congress.run(query, output_files=output_files)
            print()
            if final:
                print(final)
                exit_code = (
                    congress.last_result.exit_code
                    if congress.last_result is not None
                    else 1
                )
                sys.exit(exit_code)
            print("ERROR: No output produced. Check logs for details.", file=sys.stderr)
            sys.exit(1)
        except SystemExit:
            raise
        except Exception as exc:
            if congress is not None:
                final = congress._finalize_internal_error(query, exc)
                print()
                print(final)
                exit_code = (
                    congress.last_result.exit_code
                    if congress.last_result is not None
                    else _status_to_exit_code(STATUS_INTERNAL_ERROR)
                )
                sys.exit(exit_code)
            raise

    try:
        result_file = _normalize_result_file_path(working_dir, result_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if ci_mode and not ci_mode_explicit:
        ci_mode = False
        ci_mode_explicit = True
    if ci_mode:
        print("ERROR: --ci requires --query or --resume; interactive prompting is disabled.", file=sys.stderr)
        sys.exit(1)

    interactive_mode(
        max_rounds,
        codex_bin,
        working_dir,
        verification_mode,
        max_verification_timeout,
        second_inspector_mode,
        log_prompts_mode,
        quality_mode,
        quality_mode_explicit,
        result_file,
        strict_exit_codes,
        ci_mode,
        ci_mode_explicit=ci_mode_explicit,
    )


if __name__ == "__main__":
    main()
