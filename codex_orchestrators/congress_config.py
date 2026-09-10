"""Defaults, state vocabulary, and review policy constants for Congress."""

import re

MAX_ROUNDS = 3
SILENCE_TIMEOUT = 3600  # kill codex if zero output for 60 min
STARTUP_TIMEOUT = 3600  # kill if no output within 60 min of launch
COOLDOWN_BETWEEN = 5  # seconds between codex calls
from codex_orchestrators.metadata import state_base

LOG_DIR = str(state_base() / "diagnostics")
MAX_PROMPT_CHARS = 50000  # max inline text in prompts
AUTO_CONTINUE_SECS = 10  # seconds before auto-continue in transition menu
LOCK_STALE_SECONDS = 24 * 60 * 60
CONGRESS_ROUNDS_DIR = "congress_rounds"  # stable round output files (in working_dir)
CONGRESS_STATE_FILE = "congress_state.json"  # resume state (in working_dir)
CONGRESS_RESULT_FILE = "congress_result.md"  # terminal result/status artifact (in working_dir)
CONGRESS_HISTORY_FILE = "congress_history.md"  # append-only readable lifecycle history
CONGRESS_VERIFICATION_FILE = "congress_verification.md"  # future verification evidence
CONGRESS_BLOCKED_FILE = "congress_blocked.md"  # future blocked-state details
CONGRESS_LOCK_FILE = "congress.lock"  # future workspace lock
CONGRESS_LOCK_TAKEOVER_FILE = CONGRESS_LOCK_FILE + ".takeover"
RESEARCHER_UPDATED_FILE = "researcher_updated.md"  # managed researcher notes / reasoning doc
INSPECTOR_COMMENTS_FILE = "inspector_comments.md"  # managed inspector review doc
INSPECTOR_2_COMMENTS_FILE = "inspector_2_comments.md"  # future second inspector review doc
SESSION_REQUEST_FILE = "session_request.md"  # managed copy of the original request + output contract
STATE_VERSION = 3
STATE_SCHEMA_VERSION = 3
MAX_RECOVERY_RETRIES = 3  # Initial attempt plus at most three retries
RATE_LIMIT_AUTO_RETRY_SECONDS = 180  # auto-retry every 3 minutes while waiting on rate limits
MAX_CONTINUATIONS = 10  # max "continue" prompts when context limit cuts output
INTERNET_CHECK_INTERVAL = 30  # seconds between internet checks during silence
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
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".next",
    ".tox",
    ".nox",
    "dist",
    "build",
    "target",
    "coverage",
    ".coverage",
    ".cache",
}
SOURCE_CONTEXT_EXCLUDED_DIR_PARTS = {
    "secrets",
    "secret",
    "logs",
    CONGRESS_ROUNDS_DIR.lower(),
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
DEFAULT_LOG_PROMPTS_MODE = "off"
LOG_PROMPTS_MODES = {"off"}
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
    (
        "current_info",
        r"\b(?:latest|newest|today|recent|up[- ]to[- ]date|news|price|schedule|version)\b|\b(?:current|up[- ]to[- ]date)\s+(?:info|information|docs?|documentation|sources?|references?|version|api)\b|\bcurrent\s+(?:[a-z0-9_-]+\s+){0,3}(?:data|datasets?|prices?|pricing|news|schedules?|versions?|changelogs?|migration\s+guides?|release\s+notes?)\b|\b(?:latest|newest|current|recent|most\s+recent|up[- ]to[- ]date)\s+(?:package\s+version|changelogs?|migration\s+guides?|release\s+notes?)\b|\bcurrent\s+external\s+(?:data|sources?|evidence|information)\b",
    ),
    ("official_website", r"\b(?:official|unofficial)\s+(?:websites?|sites?|docs?|documentation)\b"),
    (
        "github_source",
        r"\b(?:research|search|find|check|use|review|compare|cite|source|sources|external)\b[^\n]{0,80}(?<![.\w-])github\b(?!\s+actions\b)|(?<![.\w-])github\b(?!\s+actions\b)[^\n]{0,80}\b(?:repos?|repositories|sources?|references?|examples?|projects?|algorithms?|research)\b",
    ),
    ("forum_source", r"\bforums?\b"),
    (
        "non_english_source",
        r"\b(?:chinese|russian|non[- ]english)\s+(?:websites?|sites?|sources?|forums?|docs?|documentation)?\b",
    ),
    ("paper_identifier", r"\b(?:ssrn|arxiv|doi|research papers?|papers?)\b"),
    (
        "citation_or_reference",
        r"\b(?:citations?|cite)\b|\b(?:external|cited|research)\s+references?\b|\breferences?\s+(?:used|cited|consulted|for\s+(?:research|external|current|latest))\b",
    ),
    (
        "source_evidence",
        r"\b(?:cite|cited|citation|research|researching)\b[^\n]{0,80}\bsources?\b|\b(?:search|find)\b[^\n]{0,80}\b(?:external|online|web|internet|current|latest|research|citation)\b[^\n]{0,80}\bsources?\b|\b(?:search|find)\b[^\n]{0,80}\bsources?\b[^\n]{0,80}\b(?:external|online|web|internet|current|latest|research|citation)\b|\balgorithm\s+search\b[^\n]{0,80}\bsources?\b|\b(?:external|online|current|latest|research|citation)\s+sources?\b|\bsources?\s+(?:used|cited|consulted|checked|for\s+(?:current|latest|external|research|citation|api))\b|\bsource\s+evidence\b",
    ),
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
    (
        "claims_sourced_or_assumptions_labeled",
        "Claims sourced or assumptions labeled",
        ("claims sourced", "assumptions labeled"),
    ),
    (
        "implementation_details_present",
        "Implementation details present",
        ("implementation details", "details present"),
    ),
    (
        "edge_cases_failure_modes_covered",
        "Edge cases and failure modes covered",
        ("edge cases", "failure modes"),
    ),
    (
        "verification_test_strategy_specific",
        "Verification/test strategy specific",
        ("verification", "test strategy"),
    ),
    ("internally_consistent", "Internally consistent", ("internally consistent", "internal consistency")),
    ("no_raw_secrets", "No raw secrets included", ("raw secrets", "secrets included")),
    ("best_practical_version", "Best practical version", ("best practical", "best possible")),
)
EXCELLENCE_CHECKLIST_KEYS = {item[0] for item in EXCELLENCE_CHECKLIST_ITEMS}

REVIEW_REQUIRED_SECTION_ALIASES = {
    "findings": ("findings", "issues", "review findings", "findings and issues"),
    "evidence": ("evidence reviewed", "evidence", "files reviewed", "review evidence"),
    "commands": ("commands/tests run", "commands and tests run", "commands run", "tests run", "checks run"),
    "output_hashes": (
        "output hashes",
        "requested output hashes",
        "hashes reviewed",
        "output hashes reviewed",
    ),
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
    r"\brate limit exceeded\b",
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
    "exceeds the context window",  # "Your input exceeds the context window of this model"
    "context length exceeded",  # "context length exceeded"
    "context_length_exceeded",  # API error code
    "maximum context length",  # "This model's maximum context length is X tokens"
    "please adjust your input",  # "Please adjust your input and try again"
    "input is too long",  # generic too-long message
    "input exceeds",  # "input exceeds the context window"
)

# Network error keywords
NETWORK_ERROR_SIGNALS = (
    "network error",
    "connection refused",
    "connection reset",
    "dns resolution",
    "etimedout",
    "econnrefused",
    "enotfound",
    "socket hang up",
    "fetch failed",
    "econnreset",
    "unable to connect",
    "network is unreachable",
    "no internet",
    "getaddrinfo",
    "eai_again",
)

# Terminal colors (ANSI)
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"

# Box drawing
BOX_H = "\u2500"
BOX_V = "\u2502"
BOX_TL = "\u250c"
BOX_TR = "\u2510"
BOX_BL = "\u2514"
BOX_BR = "\u2518"
