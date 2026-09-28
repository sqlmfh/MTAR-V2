from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from document_store import FileDocumentStore
from models import new_sample
from prolab_parser import suggested_mapping


FIXTURE = Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json"


class LabWorkflowTests(unittest.TestCase):
    def test_suggested_mapping_prefers_sample_serial_numbers(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))

        outdoor = new_sample("Air Sample", "Outdoor Control", outdoor_control=True)
        outdoor["serial_number"] = "Q2986725"

        bedroom = new_sample("Air Sample")
        bedroom["name"] = "Wrong room name on purpose"
        bedroom["serial_number"] = "Q2986713"

        coat = new_sample("Surface Sample")
        coat["name"] = "Another wrong name"
        coat["serial_number"] = "CC"

        job = {"samples": [coat, outdoor, bedroom]}
        mapping = suggested_mapping(parsed, job)

        self.assertEqual(mapping["2030805-1"], outdoor["id"])
        self.assertEqual(mapping["2030805-2"], bedroom["id"])
        self.assertEqual(mapping["2030805-3"], coat["id"])

    def test_document_store_keeps_binary_files_outside_job_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileDocumentStore(tmp)
            source = b"%PDF-1.7\nexample"
            path = store.save_bytes("job_123", "lab", "PROLAB 2030805.pdf", source)

            self.assertTrue(path.exists())
            self.assertEqual(store.read_bytes("job_123", "lab", "PROLAB_2030805.pdf"), source)

            files = store.list_files("job_123")
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["category"], "lab")
            self.assertEqual(files[0]["name"], "PROLAB_2030805.pdf")


if __name__ == "__main__":
    unittest.main()
