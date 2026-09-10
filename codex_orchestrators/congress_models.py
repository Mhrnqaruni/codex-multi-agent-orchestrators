"""Persisted evidence and review data models for Congress."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .congress_config import (
    CONGRESS_HISTORY_FILE,
    CONGRESS_RESULT_FILE,
    DEFAULT_QUALITY_MODE,
    EXCELLENCE_STATUS_UNKNOWN,
    ISSUE_STATUS_OPEN,
    REVIEW_STATUS_MALFORMED,
    VERIFICATION_STATUS_PENDING,
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
