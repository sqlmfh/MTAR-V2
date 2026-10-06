from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from document_store import FileDocumentStore
from gmail_intake import GmailPdfAttachment
from job_store import SQLiteJobStore
import mtar_services


FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"


class _FakeGmailClient:
    def __init__(self, attachment):
        self.attachment = attachment

    def configured(self):
        return True

    def search_pdf_attachments(self, **kwargs):
        return [self.attachment]


class GmailAutoCreateEndToEndTests(unittest.TestCase):
    def test_unmatched_prolab_email_creates_assessment_and_reports(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        attachment = GmailPdfAttachment(
            message_id="gmail_scarlet_1",
            thread_id="thread_scarlet_1",
            sender="prolab@example.test",
            subject="PRO-LAB Scarlet Harper",
            filename="Scarlet_Lab_Results.pdf",
            content=b"%PDF-1.7\nfixture",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "mtar.sqlite3")
            store.set_setting(mtar_services.AUTO_CREATE_SETTING, "1")  # the switch on PRO-LAB Reports
            documents = FileDocumentStore(Path(tmp) / "documents")
            fake_gmail = _FakeGmailClient(attachment)

            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "gmail_client", fake_gmail),
                patch.object(
                    mtar_services,
                    "inspect_attachment",
                    return_value={
                        "message_id": attachment.message_id,
                        "thread_id": attachment.thread_id,
                        "sender": attachment.sender,
                        "subject": attachment.subject,
                        "filename": attachment.filename,
                        "parsed": parsed,
                    },
                ),
            ):
                result = mtar_services.run_gmail_intake()

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["created"], 1)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["ignored"], 0)

            summaries = store.list()
            self.assertEqual(len(summaries), 1)
            job = store.get(summaries[0].id)
            self.assertEqual(job["client_name"], "Scarlet Harper")
            self.assertEqual(job["status"], "report_review")
            self.assertTrue(job.get("customer_id"))
            self.assertTrue(job.get("property_id"))
            self.assertEqual(job["gmail_message_ids"], ["gmail_scarlet_1"])

            files = documents.list_files(job["id"])
            names = {item["name"] for item in files}
            self.assertIn("Scarlet_Lab_Results.pdf", names)
            self.assertIn("Scarlet_Harper_Mold_Assessment_AUTO_DRAFT.docx", names)
            self.assertIn("Scarlet_Harper_Mold_Assessment_AUTO_DRAFT.pdf", names)

            # The same Gmail message must not create a duplicate assessment.
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "gmail_client", fake_gmail),
                patch.object(
                    mtar_services,
                    "inspect_attachment",
                    return_value={"parsed": parsed},
                ),
            ):
                second = mtar_services.run_gmail_intake()

            self.assertEqual(second["created"], 0)
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(len(store.list()), 1)

            # A resend in a different Gmail message is also deduplicated by
            # the PRO-LAB report number, not only by Gmail message ID.
            resent = GmailPdfAttachment(
                message_id="gmail_scarlet_resend",
                thread_id="thread_scarlet_resend",
                sender="prolab@example.test",
                subject="PRO-LAB Scarlet Harper resend",
                filename="Scarlet_Lab_Results_Resent.pdf",
                content=b"%PDF-1.7\\nfixture-resend",
            )
            resent_client = _FakeGmailClient(resent)
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "gmail_client", resent_client),
                patch.object(
                    mtar_services,
                    "inspect_attachment",
                    return_value={"parsed": parsed},
                ),
            ):
                third = mtar_services.run_gmail_intake()

            self.assertEqual(third["created"], 0)
            self.assertEqual(third["skipped"], 1)
            self.assertEqual(len(store.list()), 1)
            persisted = store.get(store.list()[0].id)
            self.assertIn("gmail_scarlet_resend", persisted["gmail_message_ids"])

            forwarded = GmailPdfAttachment(
                message_id="gmail_scarlet_forwarded",
                thread_id="thread_scarlet_forwarded",
                sender="someone@example.test",
                subject="Fwd: PRO-LAB Scarlet Harper",
                filename="Scarlet_Lab_Results.pdf",
                content=b"%PDF-1.7\nfixture",
            )
            forwarded_gmail = _FakeGmailClient(forwarded)
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "gmail_client", forwarded_gmail),
                patch.object(
                    mtar_services,
                    "inspect_attachment",
                    return_value={
                        "message_id": forwarded.message_id,
                        "thread_id": forwarded.thread_id,
                        "sender": forwarded.sender,
                        "subject": forwarded.subject,
                        "filename": forwarded.filename,
                        "parsed": parsed,
                    },
                ),
            ):
                third = mtar_services.run_gmail_intake()

            self.assertEqual(third["created"], 0)
            self.assertEqual(third["skipped"], 1)
            self.assertEqual(len(store.list()), 1)
            refreshed = store.get(job["id"])
            self.assertIn("gmail_scarlet_forwarded", refreshed["gmail_message_ids"])


if __name__ == "__main__":
    unittest.main()
