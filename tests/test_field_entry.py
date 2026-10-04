from __future__ import annotations

import unittest

from coc_builder import validate_coc_payload
import mtar_services
from prolab_parser import _extract_metadata, _split_test_location


class TestLocationTests(unittest.TestCase):
    def test_zip_is_found_when_the_city_runs_onto_its_own_line(self):
        page = "Test Location:\n6108 CHERRY GLOW LANE\nNORTH RICHLAND HILLS, TX\n76180\nReport Number:\n2033849"
        location = _extract_metadata(page)["test_location"]
        self.assertEqual(
            _split_test_location(location),
            {"address": "6108 CHERRY GLOW LANE", "city": "NORTH RICHLAND HILLS", "state": "TX", "zip": "76180"},
        )

    def test_common_layouts(self):
        cases = {
            "16371 COUNTY ROAD 245, TERRELL, TX 75160": ("16371 COUNTY ROAD 245", "TERRELL", "TX", "75160"),
            "6108 CHERRY GLOW LANE, NORTH RICHLAND HILLS TX 76180": ("6108 CHERRY GLOW LANE", "NORTH RICHLAND HILLS", "TX", "76180"),
            "6108 CHERRY GLOW LANE, NORTH RICHLAND HILLS, TX76180": ("6108 CHERRY GLOW LANE", "NORTH RICHLAND HILLS", "TX", "76180"),
            "123 Main St, Dallas, Texas 75201": ("123 Main St", "Dallas", "TX", "75201"),
            "123 Main St, Dallas, 75201": ("123 Main St", "Dallas", "", "75201"),
        }
        for value, (address, city, state, zip_code) in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    _split_test_location(value),
                    {"address": address, "city": city, "state": state, "zip": zip_code},
                )


class MoistureSentenceTests(unittest.TestCase):
    def test_template_sentences(self):
        self.assertEqual(
            mtar_services.moisture_sentence("dry", 14.0),
            "No surfaces were wet in this area. Highest moisture content observed was 14%.",
        )
        self.assertEqual(
            mtar_services.moisture_sentence("wet", 99.5, "Flooring has trapped moisture."),
            "Surfaces were wet in this area. Highest moisture content observed was 99.5%. Flooring has trapped moisture.",
        )
        self.assertEqual(mtar_services.moisture_sentence(None, None, "Old note."), "Old note.")


class CocTests(unittest.TestCase):
    def test_serial_number_is_optional(self):
        payload = {
            "property": {"address": "1 Main", "city": "Dallas", "state": "TX", "zip": "75201"},
            "samples": [{
                "serial_number": "", "collection_location": "Closet", "sample_type_code": "P15",
                "flow_rate_liters": "15", "flow_rate_minutes": "5",
            }],
        }
        self.assertEqual(validate_coc_payload(payload), [])


if __name__ == "__main__":
    unittest.main()
