from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
import fitz

from document_store import FileDocumentStore
from gmail_intake import GmailPdfAttachment
from job_store import SQLiteJobStore
import mtar_services
from workflow import complete_job, reopen_job

FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"


class _FakeGmailClient:
    def __init__(self, attachments):
        self.attachments = attachments

    def configured(self):
        return True

    def search_pdf_attachments(self):
        return list(self.attachments)


def _pdf_with_text(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def _docx_with_text(text: str) -> bytes:
    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    out = BytesIO()
    document.save(out)
    return out.getvalue()


class _TempServices(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.store = SQLiteJobStore(tmp / "db.sqlite3")
        self.documents = FileDocumentStore(tmp / "docs")
        self._patches = [
            patch.object(mtar_services, "store", self.store),
            patch.object(mtar_services, "documents", self.documents),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _job(self, name: str, address: str, status: str = "report_review") -> dict:
        job = mtar_services.create_field_job()
        job.update({"client_name": name, "address": address, "city": "Dallas", "status": status})
        return self.store.save(job)


class CompleteAndReopenTests(unittest.TestCase):
    def test_any_status_can_be_completed_and_reopened(self):
        job = {"id": "job_1", "status": "awaiting_lab"}
        done = complete_job(job)
        self.assertEqual(done["status"], "closed")
        self.assertEqual(reopen_job(done)["status"], "awaiting_lab")

    def test_reopening_a_normally_closed_job_goes_back_to_sent(self):
        self.assertEqual(reopen_job({"status": "closed"})["status"], "sent")


class DeleteAssessmentTests(_TempServices):
    def test_delete_removes_the_job_and_its_files(self):
        job = self._job("Scarlet Harper", "16371 County Road 245")
        self.documents.save_bytes(job["id"], "photos", "a.jpg", b"jpg")
        self.assertTrue(mtar_services.delete_assessment(job["id"]))
        self.assertIsNone(self.store.get(job["id"]))
        self.assertFalse((Path(self._tmp.name) / "docs" / "jobs" / job["id"]).exists())
        self.assertEqual(self.store.list_inbox(), [])  # nothing shows up under Needs review
        self.assertFalse(mtar_services.delete_assessment(job["id"]))

    def test_gmail_does_not_bring_a_deleted_assessment_back(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        first = GmailPdfAttachment("msg_1", "t", "lab@example.test", "Result", "Scarlet.pdf", b"%PDF-1.7")
        forwarded = GmailPdfAttachment("msg_2", "t", "me@example.test", "Fwd: Result", "Scarlet.pdf", b"%PDF-1.7")

        def run(attachment):
            with (
                patch.object(mtar_services, "gmail_client", _FakeGmailClient([attachment])),
                patch.object(mtar_services, "inspect_attachment", return_value={"parsed": parsed}),
                patch.object(mtar_services, "_generate_auto_drafts"),
            ):
                return mtar_services.run_gmail_intake()

        self.assertEqual(run(first)["created"], 1)
        job_id = self.store.list()[0].id
        mtar_services.delete_assessment(job_id)

        again = run(first)
        self.assertEqual((again["created"], again["skipped"]), (0, 1))
        other_email = run(forwarded)
        self.assertEqual((other_email["created"], other_email["skipped"]), (0, 1))
        self.assertEqual(self.store.list(), [])

        # Uploading the lab PDF by hand still works if it was deleted by mistake.
        with (
            patch.object(mtar_services, "parse_prolab_pdf", return_value=parsed),
            patch.object(mtar_services, "_generate_auto_drafts"),
        ):
            self.assertEqual(mtar_services.create_from_lab_upload(b"%PDF-1.7", "Scarlet.pdf")["status"], "created")


class FinishedReportTests(_TempServices):
    def test_attaching_a_finished_report_completes_the_assessment(self):
        job = self._job("William Villalobos", "6108 Cherry Glow Lane")
        saved = mtar_services.attach_finished_report(job, b"%PDF-1.4 report", "William Report.pdf")
        self.assertEqual(saved["status"], "closed")
        self.assertEqual(saved["finished_report_filename"], "William_Report.pdf")
        self.assertEqual(self.documents.read_bytes(job["id"], "finished", "William_Report.pdf"), b"%PDF-1.4 report")
        with self.assertRaises(ValueError):
            mtar_services.attach_finished_report(job, b"x", "notes.txt")

    def test_reports_are_matched_by_property_address(self):
        william = self._job("William Villalobos", "6108 Cherry Glow Lane")
        scarlet = self._job("Scarlet Harper", "16371 County Road 245")
        report = _pdf_with_text(
            "MOLD ASSESSMENT REPORT\nClient & Property:​ William Villalobos​ 6108 Cherry Glow Lane​"
        )
        word = _docx_with_text("MOLD ASSESSMENT REPORT\nScarlet Harper\n16371 County Road 245\nTerrell, TX 75160")
        lab = _pdf_with_text("Certificate of Mold Analysis\nTest Location: 6108 CHERRY GLOW LANE")
        unknown = _pdf_with_text("MOLD ASSESSMENT REPORT\nJane Doe\n12 Elm Street")

        results = mtar_services.import_finished_reports([
            ("William.pdf", report), ("Scarlet.docx", word), ("Lab.pdf", lab), ("Jane.pdf", unknown),
        ])
        by_file = {r["filename"]: r for r in results}
        self.assertEqual(by_file["William.pdf"]["job_id"], william["id"])
        self.assertEqual(by_file["Scarlet.docx"]["job_id"], scarlet["id"])
        self.assertIsNone(by_file["Lab.pdf"]["job_id"])
        self.assertIn("PRO-LAB", by_file["Lab.pdf"]["reason"])
        self.assertIsNone(by_file["Jane.pdf"]["job_id"])
        self.assertEqual(self.store.get(william["id"])["status"], "closed")
        self.assertEqual(self.store.get(scarlet["id"])["status"], "closed")

    def test_same_address_needs_the_customer_name(self):
        first = self._job("William Villalobos", "6108 Cherry Glow Lane")
        self._job("Maria Lopez", "6108 Cherry Glow Lane")
        report = _pdf_with_text("MOLD ASSESSMENT REPORT\nWilliam Villalobos\n6108 Cherry Glow Lane")
        job, reason = mtar_services.match_finished_report(report, "report.pdf", mtar_services._all_jobs())
        self.assertEqual(job["id"], first["id"])

        nameless = _pdf_with_text("MOLD ASSESSMENT REPORT\n6108 Cherry Glow Lane")
        job, reason = mtar_services.match_finished_report(nameless, "report.pdf", mtar_services._all_jobs())
        self.assertIsNone(job)
        self.assertIn("2 assessments", reason)


if __name__ == "__main__":
    unittest.main()
