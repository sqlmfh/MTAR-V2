from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from coc_builder import build_coc_payload, validate_coc_payload
from job_store import SQLiteJobStore, prepare_persistent_job
from workflow import (
    STATUS_AWAITING_LAB,
    STATUS_DRAFT,
    STATUS_INSPECTION,
    transition_job,
)


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

    def test_coc_payload_supports_air_and_surface_samples(self):
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
        self.assertEqual(payload["property"]["address"], "16371 County Road 245")
        self.assertEqual(payload["samples"][0]["sample_type_code"], "P15")
        self.assertEqual(payload["samples"][0]["flow_rate_liters"], "15")
        self.assertEqual(payload["samples"][1]["serial_number"], "Q2986713")
        self.assertEqual(payload["samples"][2]["sample_type_code"], "SW")
        self.assertEqual(payload["samples"][2]["collection_location"], "Coat Closet")


if __name__ == "__main__":
    unittest.main()
