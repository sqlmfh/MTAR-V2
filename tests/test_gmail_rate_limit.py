from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import gmail_intake
from gmail_intake import GmailApiClient, GmailRateLimited
import mtar_services


class _Request:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class _FakeService:
    """Two emails with one PDF each; records which Gmail calls were made."""

    def __init__(self):
        self.calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, **kwargs):
        self.calls.append(("list",))
        return _Request({"messages": [{"id": "old"}, {"id": "new"}]})

    def get(self, *, userId, id=None, messageId=None, format=None):
        if messageId:  # attachments().get
            self.calls.append(("attachment", messageId))
            return _Request({"data": base64.urlsafe_b64encode(b"%PDF-1.7").decode()})
        self.calls.append(("message", id))
        return _Request({
            "id": id, "threadId": "t",
            "payload": {"headers": [{"name": "Subject", "value": "PRO-LAB"}], "parts": [
                {"filename": "Lab.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
            ]},
        })


class _HttpError(Exception):
    def __init__(self, status, content):
        super().__init__(content)
        self.resp = type("Resp", (), {"status": status})()
        self.content = content.encode()


class GmailClientTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(gmail_intake, "GMAIL_CALL_PAUSE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_emails_already_handled_are_not_downloaded_again(self):
        client = GmailApiClient()
        client._service = service = _FakeService()
        found = client.search_pdf_attachments(skip_message_ids={"old"})
        self.assertEqual([a.message_id for a in found], ["new"])
        self.assertEqual(service.calls, [("list",), ("message", "new"), ("attachment", "new")])

    def test_rate_limit_answer_becomes_a_pause_until_the_retry_time(self):
        client = GmailApiClient()
        error = _HttpError(429, '{"error": {"message": "User-rate limit exceeded.  Retry after 2026-10-06T20:02:02.591Z"}}')
        with self.assertRaises(GmailRateLimited) as caught:
            client._execute(_Request(error=error))
        self.assertEqual(caught.exception.retry_at, datetime(2026, 10, 6, 20, 2, 2, 591000, tzinfo=timezone.utc))
        with self.assertRaises(_HttpError):
            client._execute(_Request(error=_HttpError(500, "backend error")))


class GmailPauseTests(unittest.TestCase):
    def setUp(self):
        mtar_services.GMAIL_PAUSED_UNTIL = None
        self.addCleanup(setattr, mtar_services, "GMAIL_PAUSED_UNTIL", None)

    def test_checks_wait_until_gmail_allows_them_again(self):
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        with patch.object(mtar_services, "run_gmail_intake", side_effect=GmailRateLimited(retry_at)) as run:
            with self.assertRaises(mtar_services.GmailPaused):
                mtar_services.check_gmail_now()
            self.assertEqual(mtar_services.LAST_GMAIL_CHECK["status"], "paused")
            self.assertEqual(mtar_services.LAST_GMAIL_CHECK["until"], retry_at)
            with self.assertRaises(mtar_services.GmailPaused):
                mtar_services.check_gmail_now()
            self.assertEqual(run.call_count, 1)  # no Gmail call while paused

        mtar_services.GMAIL_PAUSED_UNTIL = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch.object(mtar_services, "run_gmail_intake", return_value={"configured": True, "checked": 0}):
            mtar_services.check_gmail_now()
        self.assertIsNone(mtar_services.GMAIL_PAUSED_UNTIL)
        self.assertEqual(mtar_services.LAST_GMAIL_CHECK["status"], "ok")


if __name__ == "__main__":
    unittest.main()
