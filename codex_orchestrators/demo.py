"""Offline contract demonstration, explicitly not a model-quality benchmark."""

import json
from pathlib import Path
import tempfile

from .events import parse_events
from .policies import Role, build_command
from .recovery import Failure, RunBudget, classify_failure
from .workspace import fingerprint


def run_demo() -> dict:
    budget = RunBudget(max_calls=4)
    with tempfile.TemporaryDirectory(prefix="orchestrators-demo-") as directory:
        root = Path(directory)
        target = root / "example.txt"
        target.write_text("Synthetic draft with a missing acceptance detail.\n", encoding="utf-8")
        original = fingerprint(root)
        budget.reserve()
        transient = classify_failure("rate limit exceeded", 1)
        assert transient is Failure.TRANSIENT
        budget.reserve()
        target.write_text("Synthetic draft with its acceptance detail included.\n", encoding="utf-8")
        candidate = fingerprint(root)
        assert candidate != original
        budget.reserve()
        events = [
            {"type": "thread.started", "thread_id": "fictional-demo-thread"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Synthetic contract review: acceptance detail present.",
                },
            },
            {"type": "turn.completed"},
        ]
        result = parse_events("\n".join(json.dumps(event) for event in events), "", 0)
        assert result.returncode == 0
        assert fingerprint(root) == candidate
        review_argv = build_command("codex", Role.REVIEW, str(root))
        assert 'sandbox_mode="read-only"' in review_argv
        target.write_text("Post-review change.\n", encoding="utf-8")
        assert fingerprint(root) != candidate
    return {
        "demo": "offline-contracts",
        "live_model_calls": 0,
        "simulated_calls": budget.calls,
        "checks": {
            "transient_failure_classified": True,
            "candidate_change_detected": True,
            "structured_response_validated": True,
            "review_policy_read_only": True,
            "post_review_drift_invalidates_evidence": True,
        },
        "scope": "Boundary demonstration; not an end-to-end engine run or model benchmark.",
    }
