from __future__ import annotations

from pathlib import Path
import unittest

from prolab_parser import parse_prolab_pdf


FIXTURE = Path(__file__).parent / "fixtures" / "Scarlet_Lab_Results_pages_1_2.pdf"


class ScarletLabRegressionTests(unittest.TestCase):
    def test_real_scarlet_report_parses_air_and_surface_samples(self):
        parsed = parse_prolab_pdf(FIXTURE.read_bytes())

        self.assertEqual(parsed["metadata"].get("report_number"), "2030805")
        self.assertEqual(parsed["metadata"].get("project_name"), "SCARLET HARPER")
        self.assertEqual(len(parsed["samples"]), 3)

        samples = {sample["coc_line"]: sample for sample in parsed["samples"]}

        outdoor = samples["2030805-1"]
        self.assertTrue(outdoor["is_air"])
        self.assertEqual(outdoor["determination"], "CONTROL")
        self.assertEqual(outdoor["serial_number"], "Q2986725")
        self.assertEqual(outdoor["total_spores"], 3179)

        bedroom = samples["2030805-2"]
        self.assertTrue(bedroom["is_air"])
        self.assertEqual(bedroom["determination"], "NOT ELEVATED")
        self.assertEqual(bedroom["serial_number"], "Q2986713")
        self.assertEqual(bedroom["total_spores"], 273)

        coat = samples["2030805-3"]
        self.assertFalse(coat["is_air"])
        self.assertEqual(coat["sample_type"], "SWAB")
        self.assertEqual(coat["determination"], "UNUSUAL")
        self.assertIn("Presence of growth observed", coat["observations"])

        expected_surface_fungi = {
            "Cladosporium",
            "Curvularia",
            "Epicoccum",
            "Hyphae",
            "Other Ascospores",
            "Other Basidiospores",
            "Penicillium/Aspergillus",
            "Smuts, myxomycetes",
        }
        self.assertTrue(expected_surface_fungi.issubset(set(coat["fungi"])))


if __name__ == "__main__":
    unittest.main()
