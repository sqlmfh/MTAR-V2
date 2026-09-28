from __future__ import annotations

import copy
from datetime import datetime, timezone

STATUS_DRAFT = "draft"
STATUS_INSPECTION = "inspection"
STATUS_AWAITING_LAB = "awaiting_lab"
STATUS_LAB_RECEIVED = "lab_received"
STATUS_REPORT_REVIEW = "report_review"
STATUS_READY_TO_SEND = "ready_to_send"
STATUS_SENT = "sent"
STATUS_CLOSED = "closed"

JOB_STATUSES = (
    STATUS_DRAFT,
    STATUS_INSPECTION,
    STATUS_AWAITING_LAB,
    STATUS_LAB_RECEIVED,
    STATUS_REPORT_REVIEW,
    STATUS_READY_TO_SEND,
    STATUS_SENT,
    STATUS_CLOSED,
)

ALLOWED_TRANSITIONS = {
    STATUS_DRAFT: {STATUS_INSPECTION, STATUS_AWAITING_LAB},
    STATUS_INSPECTION: {STATUS_DRAFT, STATUS_AWAITING_LAB},
    STATUS_AWAITING_LAB: {STATUS_INSPECTION, STATUS_LAB_RECEIVED},
    STATUS_LAB_RECEIVED: {STATUS_AWAITING_LAB, STATUS_REPORT_REVIEW},
    STATUS_REPORT_REVIEW: {STATUS_LAB_RECEIVED, STATUS_READY_TO_SEND},
    STATUS_READY_TO_SEND: {STATUS_REPORT_REVIEW, STATUS_SENT},
    STATUS_SENT: {STATUS_READY_TO_SEND, STATUS_CLOSED},
    STATUS_CLOSED: set(),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def transition_job(job: dict, new_status: str) -> dict:
    """Return a copy of *job* after applying a valid workflow transition."""
    if new_status not in JOB_STATUSES:
        raise ValueError(f"Unknown job status: {new_status}")

    current = job.get("status") or STATUS_DRAFT
    if current not in JOB_STATUSES:
        raise ValueError(f"Unknown current job status: {current}")
    if new_status == current:
        return copy.deepcopy(job)
    if new_status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid job transition: {current} -> {new_status}")

    updated = copy.deepcopy(job)
    updated["status"] = new_status
    updated["status_changed_at"] = _utc_now_iso()
    return updated


def final_report_issues(job: dict, *, lab_pdf_present: bool) -> list[str]:
    """Central final-report gate shared by any future UI or automation worker."""
    from models import validate_job

    issues = validate_job(job, lab_pdf_present=lab_pdf_present)
    if any(
        area.get("finding") == "Needs consultant review"
        for area in job.get("areas", [])
    ):
        issues.append("Review every inspection-area finding")
    return issues


def can_generate_final_report(job: dict, *, lab_pdf_present: bool) -> bool:
    return not final_report_issues(job, lab_pdf_present=lab_pdf_present)
