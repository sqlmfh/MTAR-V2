from __future__ import annotations

from dataclasses import dataclass
import base64
import os
from typing import Iterable

from prolab_parser import parse_prolab_pdf


DEFAULT_GMAIL_QUERY = (
    'has:attachment filename:pdf newer_than:30d '
    '-in:trash -in:spam'
)


@dataclass(frozen=True)
class GmailPdfAttachment:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    filename: str
    content: bytes


def _norm(value: str) -> str:
    return " ".join(str(value or "").upper().split())


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def job_match_score(parsed: dict, job: dict) -> tuple[int, list[str]]:
    """Score a parsed PRO-LAB report against one MTAR job.

    The matcher rewards independent evidence and never treats a weak text match
    as sufficient by itself. Serial-number matches are strongest because they
    are entered during sampling before the lab report arrives.
    """
    score = 0
    reasons: list[str] = []
    metadata = parsed.get("metadata", {})

    project = _norm(metadata.get("project_name"))
    client = _norm(job.get("client_name"))
    if project and client and project == client:
        score += 35
        reasons.append("client name")

    location = _norm(metadata.get("test_location"))
    address = _norm(job.get("address"))
    city = _norm(job.get("city"))
    zip_code = _digits(job.get("zip"))
    if address and address in location:
        score += 30
        reasons.append("property address")
    if city and city in location:
        score += 10
        reasons.append("city")
    if zip_code and zip_code in _digits(location):
        score += 15
        reasons.append("ZIP")

    job_serials = {
        _norm(sample.get("serial_number") or sample.get("lab_serial_number"))
        for sample in job.get("samples", [])
        if _norm(sample.get("serial_number") or sample.get("lab_serial_number"))
    }
    lab_serials = {
        _norm(sample.get("serial_number"))
        for sample in parsed.get("samples", [])
        if _norm(sample.get("serial_number"))
    }
    serial_matches = sorted(job_serials & lab_serials)
    if serial_matches:
        score += min(60, 25 * len(serial_matches))
        reasons.append(f"{len(serial_matches)} sample serial match(es)")

    known_report = _norm(job.get("lab_metadata", {}).get("report_number"))
    incoming_report = _norm(metadata.get("report_number"))
    if known_report and incoming_report and known_report == incoming_report:
        score += 80
        reasons.append("report number")

    return score, reasons


def is_confident_prolab_result(parsed: dict) -> bool:
    """Return True when parsed metadata and sample COC lines agree on a report."""
    metadata = parsed.get("metadata", {})
    report_number = _norm(metadata.get("report_number"))
    samples = parsed.get("samples", [])
    if not report_number or not samples:
        return False

    matching_coc_lines = 0
    for sample in samples:
        coc_line = _norm(sample.get("coc_line"))
        if coc_line == report_number or coc_line.startswith(report_number + "-"):
            matching_coc_lines += 1

    return matching_coc_lines >= 1


def find_job_by_report_number(parsed: dict, jobs: Iterable[dict]) -> dict | None:
    """Find an existing MTAR assessment already linked to this lab report."""
    report_number = _norm(parsed.get("metadata", {}).get("report_number"))
    if not report_number:
        return None

    for job in jobs:
        known = _norm(job.get("lab_metadata", {}).get("report_number"))
        if not known:
            known = _norm(
                (job.get("lab_parsed") or {}).get("metadata", {}).get("report_number")
            )
        if (
            known
            and known == report_number
            and (job.get("lab_filename") or (job.get("lab_parsed") or {}).get("samples"))
        ):
            return job
    return None


def rank_jobs(parsed: dict, jobs: Iterable[dict]) -> list[dict]:
    ranked = []
    for job in jobs:
        score, reasons = job_match_score(parsed, job)
        ranked.append(
            {
                "job_id": job.get("id"),
                "score": score,
                "reasons": reasons,
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


def confident_job_match(parsed: dict, jobs: Iterable[dict], *, minimum_score: int = 60) -> dict | None:
    ranked = rank_jobs(parsed, jobs)
    if not ranked or ranked[0]["score"] < minimum_score:
        return None

    if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < 20:
        return None

    return ranked[0]


class GmailApiClient:
    """Minimal Gmail API adapter used by the MTAR intake worker.

    Expected environment variables:
    - GMAIL_CLIENT_ID
    - GMAIL_CLIENT_SECRET
    - GMAIL_REFRESH_TOKEN
    - GMAIL_USER_ID (optional; defaults to "me")

    The OAuth refresh token should belong to the mailbox that receives PRO-LAB
    results. No interactive OAuth flow runs inside the app.
    """

    def __init__(self) -> None:
        self.user_id = os.environ.get("GMAIL_USER_ID", "me")
        self._service = None

    def configured(self) -> bool:
        return all(
            os.environ.get(name)
            for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
        )

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.configured():
            raise RuntimeError("Gmail API credentials are not configured")

        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Install google-api-python-client and google-auth to enable Gmail intake"
            ) from exc

        credentials = Credentials(
            token=None,
            refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    @staticmethod
    def _headers(message: dict) -> dict[str, str]:
        return {
            header.get("name", "").lower(): header.get("value", "")
            for header in message.get("payload", {}).get("headers", [])
        }

    def _pdf_parts(self, payload: dict) -> list[dict]:
        parts = []
        stack = [payload]
        while stack:
            part = stack.pop()
            stack.extend(part.get("parts", []) or [])
            filename = part.get("filename") or ""
            mime = (part.get("mimeType") or "").lower()
            attachment_id = part.get("body", {}).get("attachmentId")
            if attachment_id and (mime == "application/pdf" or filename.lower().endswith(".pdf")):
                parts.append(part)
        return parts

    def search_pdf_attachments(self, query: str = DEFAULT_GMAIL_QUERY, max_results: int = 25) -> list[GmailPdfAttachment]:
        service = self._get_service()
        result = (
            service.users()
            .messages()
            .list(userId=self.user_id, q=query, maxResults=max_results)
            .execute()
        )

        attachments: list[GmailPdfAttachment] = []
        for item in result.get("messages", []):
            message = (
                service.users()
                .messages()
                .get(userId=self.user_id, id=item["id"], format="full")
                .execute()
            )
            headers = self._headers(message)
            for part in self._pdf_parts(message.get("payload", {})):
                attachment_id = part["body"]["attachmentId"]
                raw = (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId=self.user_id, messageId=message["id"], id=attachment_id)
                    .execute()
                    .get("data", "")
                )
                if not raw:
                    continue
                content = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                attachments.append(
                    GmailPdfAttachment(
                        message_id=message["id"],
                        thread_id=message.get("threadId", ""),
                        sender=headers.get("from", ""),
                        subject=headers.get("subject", ""),
                        filename=part.get("filename") or "PROLAB_Result.pdf",
                        content=content,
                    )
                )
        return attachments


def inspect_attachment(attachment: GmailPdfAttachment) -> dict:
    parsed = parse_prolab_pdf(attachment.content)
    return {
        "message_id": attachment.message_id,
        "thread_id": attachment.thread_id,
        "sender": attachment.sender,
        "subject": attachment.subject,
        "filename": attachment.filename,
        "parsed": parsed,
    }
