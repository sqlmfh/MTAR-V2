from __future__ import annotations

import unittest

from gmail_intake import confident_job_match, is_confident_prolab_result, job_match_score, rank_jobs


class GmailIntakeTests(unittest.TestCase):
    def setUp(self):
        self.parsed = {
            "metadata": {
                "project_name": "TEST CLIENT",
                "report_number": "2030805",
                "test_location": "123 MAIN ST, DALLAS, TX 75201",
            },
            "samples": [
                {"serial_number": "AIR001", "coc_line": "2030805-1"},
                {"serial_number": "AIR002", "coc_line": "2030805-2"},
                {"serial_number": "SWAB01", "coc_line": "2030805-3"},
            ],
        }

    def test_prolab_result_requires_matching_report_and_coc_line(self):
        self.assertTrue(is_confident_prolab_result(self.parsed))

        bad = {
            "metadata": {"report_number": "9999999"},
            "samples": [{"coc_line": "2030805-1"}],
        }
        self.assertFalse(is_confident_prolab_result(bad))

    def test_matching_rewards_property_and_serial_numbers(self):
        job = {
            "id": "job_correct",
            "client_name": "Test Client",
            "address": "123 Main St",
            "city": "Dallas",
            "zip": "75201",
            "samples": [
                {"serial_number": "AIR001"},
                {"serial_number": "AIR002"},
                {"serial_number": "SWAB01"},
            ],
        }

        score, reasons = job_match_score(self.parsed, job)
        self.assertGreaterEqual(score, 100)
        self.assertIn("client name", reasons)
        self.assertIn("property address", reasons)
        self.assertIn("3 sample serial match(es)", reasons)

    def test_confident_match_returns_none_when_two_jobs_are_too_close(self):
        jobs = [
            {
                "id": "job_a",
                "client_name": "Test Client",
                "address": "123 Main St",
                "city": "Dallas",
                "zip": "75201",
                "samples": [],
            },
            {
                "id": "job_b",
                "client_name": "Test Client",
                "address": "123 Main St",
                "city": "Dallas",
                "zip": "75201",
                "samples": [],
            },
        ]
        self.assertIsNone(confident_job_match(self.parsed, jobs))

    def test_serial_number_breaks_property_tie(self):
        jobs = [
            {
                "id": "job_correct",
                "client_name": "Test Client",
                "address": "123 Main St",
                "city": "Dallas",
                "zip": "75201",
                "samples": [{"serial_number": "AIR001"}],
            },
            {
                "id": "job_other",
                "client_name": "Test Client",
                "address": "123 Main St",
                "city": "Dallas",
                "zip": "75201",
                "samples": [{"serial_number": "NO-MATCH"}],
            },
        ]
        match = confident_job_match(self.parsed, jobs)
        self.assertIsNotNone(match)
        self.assertEqual(match["job_id"], "job_correct")
        ranked = rank_jobs(self.parsed, jobs)
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


if __name__ == "__main__":
    unittest.main()
