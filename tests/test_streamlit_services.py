from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from document_store import FileDocumentStore
from gmail_intake import GmailPdfAttachment
from job_store import SQLiteJobStore
import mtar_services


FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"
APP = Path(__file__).resolve().parent.parent / "app.py"


class _FakeGmailClient:
    def __init__(self, attachments):
        self.attachments = attachments

    def configured(self):
        return True

    def search_pdf_attachments(self):
        return list(self.attachments)


def _pdf(pages: int) -> bytes:
    doc = fitz.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 72), f"page {index + 1}")
    return doc.tobytes()


class LabInboxTests(unittest.TestCase):
    def _run(self, store, documents, attachment, parsed):
        with (
            patch.object(mtar_services, "store", store),
            patch.object(mtar_services, "documents", documents),
            patch.object(mtar_services, "gmail_client", _FakeGmailClient([attachment])),
            patch.object(mtar_services, "inspect_attachment", return_value={"parsed": parsed}),
        ):
            return mtar_services.run_gmail_intake()

    def test_uncertain_prolab_pdf_goes_to_review_without_creating_records(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for sample in parsed["samples"]:
            sample["coc_line"] = "9999999-1"  # disagrees with the report number
        attachment = GmailPdfAttachment("msg_odd", "t", "lab@example.test", "Result", "Odd.pdf", b"%PDF-1.7")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            result = self._run(store, documents, attachment, parsed)

            self.assertEqual(result["imported"], 0)
            self.assertEqual(len(result["ambiguous"]), 1)
            self.assertEqual(store.list(), [])
            items = store.list_inbox()
            self.assertEqual([item["message_id"] for item in items], ["msg_odd"])
            self.assertEqual(items[0]["report_number"], "2030805")

            # The same message is not reprocessed on the next poll.
            again = self._run(store, documents, attachment, parsed)
            self.assertEqual(again["skipped"], 1)
            self.assertEqual(len(store.list_inbox()), 1)

    def test_unrelated_pdf_is_ignored_and_not_listed(self):
        attachment = GmailPdfAttachment("msg_invoice", "t", "a@example.test", "Invoice", "Invoice.pdf", b"%PDF-1.7")
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            result = self._run(store, documents, attachment, {"metadata": {}, "samples": []})

            self.assertEqual(result["ignored"], 1)
            self.assertEqual(store.list_inbox(), [])
            self.assertEqual(store.list(), [])


class FinalPackageTests(unittest.TestCase):
    def test_lab_certificate_is_appended_unchanged(self):
        package = mtar_services.append_lab_certificate(_pdf(3), _pdf(2))
        with fitz.open(stream=package, filetype="pdf") as doc:
            self.assertEqual(doc.page_count, 5)
            self.assertIn("page 1", doc[3].get_text())
            self.assertIn("page 2", doc[4].get_text())


class StreamlitSmokeTests(unittest.TestCase):
    def test_dashboard_and_assessment_render(self):
        from streamlit.testing.v1 import AppTest

        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
            ):
                job, created = mtar_services.import_lab_report(_pdf(1), "Lab.pdf", parsed, all_jobs=[])
                self.assertTrue(created)

                dashboard = AppTest.from_file(str(APP), default_timeout=60)
                dashboard.run()
                self.assertFalse(dashboard.exception)

                page = AppTest.from_file(str(APP), default_timeout=60)
                page.query_params["assessment"] = job["id"]
                page.run()
                self.assertFalse(page.exception)
                self.assertEqual(page.title[0].value, "Scarlet Harper")


if __name__ == "__main__":
    unittest.main()
