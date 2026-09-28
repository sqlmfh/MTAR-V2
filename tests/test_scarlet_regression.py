from __future__ import annotations

import json
from pathlib import Path
import unittest

from models import new_job_state
from prolab_parser import build_draft_job_from_prolab
from report_builder import MOLD_DESCRIPTIONS


FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"


class ScarletLabRegressionTests(unittest.TestCase):
    def test_scarlet_fixture_builds_air_and_surface_samples(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        job = new_job_state()

        mapping = build_draft_job_from_prolab(
            job,
            parsed,
            MOLD_DESCRIPTIONS.keys(),
        )

        self.assertEqual(job["client_name"], "Scarlet Harper")
        self.assertEqual(job["address"], "16371 County Road 245")
        self.assertEqual(job["city"], "Terrell")
        self.assertEqual(job["state"], "TX")
        self.assertEqual(job["zip"], "75160")
        self.assertEqual(len(job["samples"]), 3)
        self.assertEqual(job["areas"], [])
        self.assertEqual(len(mapping), 3)

        samples_by_coc = {
            sample.get("lab_coc_line"): sample
            for sample in job["samples"]
        }

        outdoor = samples_by_coc["2030805-1"]
        self.assertTrue(outdoor["outdoor_control"])
        self.assertEqual(outdoor["lab_serial_number"], "Q2986725")
        self.assertEqual(outdoor["lab_total_spores"], 3179)

        bedroom = samples_by_coc["2030805-2"]
        self.assertEqual(bedroom["name"], "BEDROOM CLOSET")
        self.assertEqual(bedroom["type"], "Air Sample")
        self.assertIsNone(bedroom["area_id"])
        self.assertEqual(bedroom["lab_determination"], "NOT ELEVATED")
        self.assertEqual(bedroom["lab_total_spores"], 273)

        coat = samples_by_coc["2030805-3"]
        self.assertEqual(coat["name"], "COAT CLOSET")
        self.assertEqual(coat["type"], "Surface Sample")
        self.assertIsNone(coat["area_id"])
        self.assertEqual(coat["lab_determination"], "UNUSUAL")
        self.assertEqual(coat["lab_observations"], "Presence of growth observed.")
        self.assertIn("Cladosporium", coat["lab_fungi"])
        self.assertIn("Penicillium/Aspergillus", coat["lab_fungi"])

        surface_row = next(
            row for row in job["surface_lab_rows"]
            if row["sample_id"] == coat["id"]
        )
        self.assertEqual(surface_row["result"], "UNUSUAL / Mold Present")


if __name__ == "__main__":
    unittest.main()
