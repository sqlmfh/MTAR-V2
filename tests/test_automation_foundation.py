from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

import fitz

from coc_builder import build_coc_payload, fill_coc_pdf, validate_coc_payload
from job_store import SQLiteJobStore, prepare_persistent_job
from workflow import (
    STATUS_AWAITING_LAB,
    STATUS_DRAFT,
    STATUS_INSPECTION,
    transition_job,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


class AutomationFoundationTests(unittest.TestCase):
    def test_sqlite_store_round_trip_preserves_dates_and_status(self):
        job = prepare_persistent_job(
            {
                "client_name": "Scarlet Harper",
                "address": "16371 County Road 245",
                "inspection_date": date(2026, 9, 22),
                "report_date": date(2026, 9, 23),
                "areas": [],
                "samples": [],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "jobs.sqlite3")
            stored = store.save(job)
            loaded = store.get(stored["id"])

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["client_name"], "Scarlet Harper")
            self.assertEqual(loaded["inspection_date"], date(2026, 9, 22))
            self.assertEqual(loaded["status"], STATUS_DRAFT)

            rows = store.list()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, stored["id"])
            self.assertEqual(rows[0].property_address, "16371 County Road 245")

    def test_workflow_transitions_are_explicit(self):
        job = {"status": STATUS_DRAFT}
        inspection = transition_job(job, STATUS_INSPECTION)
        awaiting = transition_job(inspection, STATUS_AWAITING_LAB)

        self.assertEqual(inspection["status"], STATUS_INSPECTION)
        self.assertEqual(awaiting["status"], STATUS_AWAITING_LAB)
        self.assertEqual(job["status"], STATUS_DRAFT)

        with self.assertRaises(ValueError):
            transition_job(job, STATUS_AWAITING_LAB + "-unknown")

    def test_coc_payload_and_pdf_fill_support_air_and_surface_samples(self):
        job = {
            "client_name": "Scarlet Harper",
            "address": "16371 County Road 245",
            "city": "Terrell",
            "state": "TX",
            "zip": "75160",
            "inspection_date": date(2026, 9, 22),
            "sampling_time": "10:30 AM",
            "humidity": 49,
            "temperature": 74,
            "areas": [
                {"id": "area_bedroom", "name": "Bedroom Closet"},
                {"id": "area_coat", "name": "Coat Closet"},
            ],
            "samples": [
                {
                    "id": "sample_outdoor",
                    "name": "Outdoor Control",
                    "type": "Air Sample",
                    "outdoor_control": True,
                    "serial_number": "Q2986725",
                    "sample_type_code": "P15",
                    "flow_rate_liters": 15,
                    "flow_rate_minutes": 5,
                },
                {
                    "id": "sample_bedroom",
                    "name": "Bedroom Closet",
                    "type": "Air Sample",
                    "area_id": "area_bedroom",
                    "serial_number": "Q2986713",
                    "sample_type_code": "P15",
                    "flow_rate_liters": 15,
                    "flow_rate_minutes": 5,
                },
                {
                    "id": "sample_coat",
                    "name": "Coat Closet",
                    "type": "Surface Sample",
                    "area_id": "area_coat",
                    "serial_number": "CC",
                    "sample_type_code": "SW",
                },
            ],
        }

        payload = build_coc_payload(job)
        self.assertEqual(validate_coc_payload(payload), [])
        self.assertEqual(payload["samples"][2]["sample_type_code"], "SW")

        template = (FIXTURE_DIR / "BLANK_COC.pdf").read_bytes()
        output = fill_coc_pdf(template, payload)
        doc = fitz.open(stream=output, filetype="pdf")
        try:
            values = {widget.field_name: widget.field_value for widget in doc[0].widgets()}
        finally:
            doc.close()

        self.assertEqual(values["Text Field 84"], "Scarlet Harper")
        self.assertEqual(values["Text Field 85"], "16371 County Road 245")
        self.assertEqual(values["Text Field 88"], "TX")
        self.assertEqual(values["Text Field 87"], "75160")
        self.assertEqual(values["Text Field 90"], "Q2986725")
        self.assertEqual(values["Text Field 100"], "Outdoor Control")
        self.assertEqual(values["Text Field 164"], "P15")
        self.assertEqual(values["Check Box 160"], "Yes")
        self.assertEqual(values["Text Field 92"], "CC")
        self.assertEqual(values["Text Field 102"], "Coat Closet")
        self.assertEqual(values["Text Field 142"], "SW")
        self.assertEqual(values["Check Box 162"], "Yes")


if __name__ == "__main__":
    unittest.main()
