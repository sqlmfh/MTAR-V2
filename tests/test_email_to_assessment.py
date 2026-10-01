from __future__ import annotations

import json
from pathlib import Path
import unittest
import tempfile

from job_store import SQLiteJobStore, new_persistent_job
from prolab_parser import build_automated_job_from_prolab
from report_builder import MOLD_DESCRIPTIONS, create_report
from pdf_report_builder import create_customer_pdf
import fitz


FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"


class EmailToAssessmentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_scarlet_lab_creates_assessment_areas_and_assigns_samples(self):
        job = new_persistent_job()
        mapping = build_automated_job_from_prolab(
            job,
            self.parsed,
            MOLD_DESCRIPTIONS.keys(),
        )

        self.assertEqual(job["client_name"], "Scarlet Harper")
        self.assertEqual(job["address"], "16371 County Road 245")
        self.assertEqual(job["city"], "Terrell")
        self.assertEqual(job["state"], "TX")
        self.assertEqual(job["zip"], "75160")
        self.assertEqual(job["lab_metadata"]["report_number"], "2030805")
        self.assertEqual(set(mapping), {"2030805-1", "2030805-2", "2030805-3"})

        areas = {area["name"]: area for area in job["areas"]}
        self.assertEqual(set(areas), {"Bedroom Closet", "Coat Closet"})
        self.assertEqual(areas["Bedroom Closet"]["finding"], "Mold levels not elevated")
        self.assertEqual(areas["Coat Closet"]["finding"], "Active mold growth confirmed")

        samples = {sample.get("lab_coc_line"): sample for sample in job["samples"]}
        outdoor = samples["2030805-1"]
        bedroom = samples["2030805-2"]
        coat = samples["2030805-3"]

        self.assertTrue(outdoor["outdoor_control"])
        self.assertEqual(outdoor["serial_number"], "Q2986725")
        self.assertEqual(outdoor["flow_rate_liters"], 15)
        self.assertEqual(outdoor["flow_rate_minutes"], 5)

        self.assertEqual(bedroom["serial_number"], "Q2986713")
        self.assertEqual(bedroom["area_id"], areas["Bedroom Closet"]["id"])
        self.assertEqual(bedroom["flow_rate_liters"], 15)
        self.assertEqual(bedroom["flow_rate_minutes"], 5)
        self.assertEqual(bedroom["lab_determination"], "NOT ELEVATED")

        self.assertEqual(coat["serial_number"], "CC")
        self.assertEqual(coat["area_id"], areas["Coat Closet"]["id"])
        self.assertEqual(coat["lab_determination"], "UNUSUAL")
        self.assertIn("Hyphae", coat["lab_fungi"])

        self.assertEqual(
            set(job["mold_types"]),
            {
                "Cladosporium",
                "Curvularia",
                "Epicoccum",
                "Hyphae",
                "Other Ascospores",
                "Other Basidiospores",
                "Penicillium/Aspergillus",
                "Smuts, myxomycetes",
            },
        )
        self.assertNotIn("Alternaria", job["mold_types"])

    def test_customer_and_property_profiles_are_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "mtar.sqlite3")
            customer_a = store.ensure_customer("Scarlet Harper")
            customer_b = store.ensure_customer("  SCARLET   HARPER  ")
            self.assertEqual(customer_a["id"], customer_b["id"])

            property_a = store.ensure_property(
                customer_a["id"],
                "16371 County Road 245",
                "Terrell",
                "TX",
                "75160",
            )
            property_b = store.ensure_property(
                customer_b["id"],
                "16371 COUNTY ROAD 245",
                "TERRELL",
                "tx",
                "75160",
            )
            self.assertEqual(property_a["id"], property_b["id"])

    def test_lab_only_assessment_can_generate_customer_pdf(self):
        job = new_persistent_job()
        build_automated_job_from_prolab(job, self.parsed, MOLD_DESCRIPTIONS.keys())

        pdf = create_customer_pdf(job, {})
        output = pdf.getvalue()

        self.assertTrue(output.startswith(b"%PDF"))
        doc = fitz.open(stream=output, filetype="pdf")
        try:
            self.assertGreaterEqual(len(doc), 8)
            first_page = doc[0].get_text("text")
            self.assertIn("MOLD ASSESSMENT REPORT", first_page)
            self.assertIn("Scarlet Harper", first_page)
        finally:
            doc.close()

    def test_lab_only_assessment_can_generate_review_draft(self):
        job = new_persistent_job()
        build_automated_job_from_prolab(job, self.parsed, MOLD_DESCRIPTIONS.keys())

        report = create_report(job, {}, None)
        output = report.getvalue()

        self.assertGreater(len(output), 10000)
        self.assertTrue(output.startswith(b"PK"))
        self.assertEqual(job["report_outcome"], "Pending consultant review")


if __name__ == "__main__":
    unittest.main()
