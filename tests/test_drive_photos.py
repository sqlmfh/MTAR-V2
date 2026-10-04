from __future__ import annotations

import json

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from document_store import FileDocumentStore
from drive_photos import FOLDER_MIME, find_job_folder, folder_id_from_link, match_job_folders
from job_store import SQLiteJobStore
import mtar_services
from models import new_area


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _folder(id_, name):
    return {"id": id_, "name": name, "mimeType": FOLDER_MIME}


def _image(id_, name):
    return {"id": id_, "name": name, "mimeType": "image/jpeg"}


class FakeDrive:
    """Scarlet Harper's assessment folder as an inspector would lay it out."""

    tree = {
        "root": [_folder("job", "Scarlet Harper - 16371 County Road 245"), _folder("other", "John Smith")],
        "job": [
            _folder("prop", "Property"),
            _folder("out", "Outdoor"),
            _folder("rh", "RH"),
            _folder("coat", "Coat Closet"),
            _folder("attic", "Attic"),
            _image("loose", "IMG_0001.jpg"),
        ],
        "prop": [_image("p1", "house1.jpg"), _image("p2", "house2.jpg")],
        "out": [_image("o1", "pump.jpg")],
        "rh": [_image("r1", "meter.jpg")],
        "coat": [_image("c1", "wall.jpg"), _folder("coat_th", "Thermal"), _folder("coat_s", "Sampling")],
        "coat_th": [_image("t1", "flir.jpg")],
        "coat_s": [_image("s1", "swab.jpg")],
        "attic": [_image("a1", "attic.jpg")],
    }

    def configured(self):
        return True

    def list_children(self, folder_id):
        return list(self.tree.get(folder_id, []))

    def download(self, file_id):
        return _jpeg()


class DrivePhotoTests(unittest.TestCase):
    def test_folder_links_are_parsed(self):
        self.assertEqual(
            folder_id_from_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing"),
            "1AbCdEfGhIjKlMnOp",
        )
        self.assertEqual(folder_id_from_link("not a link"), "")

    def test_folder_is_found_by_client_name_only_when_unique(self):
        root = FakeDrive().list_children("root")
        self.assertEqual(find_job_folder({"client_name": "Scarlet Harper"}, root)["id"], "job")
        self.assertIsNone(find_job_folder({"client_name": "Nobody"}, root))
        duplicate = root + [_folder("dup", "Scarlet Harper (old)")]
        self.assertIsNone(find_job_folder({"client_name": "Scarlet Harper"}, duplicate))

    def test_loosely_named_job_folders_are_matched(self):
        root = [
            _folder("f1", "5302 Scarlet"), _folder("f2", "6108 William"), _folder("f3", "John Smith"),
            _folder("f4", "Maria"), _folder("f5", "Linda Mold Test"), _folder("f6", "2034001"),
        ]
        jobs = [
            {"id": "scarlet", "client_name": "Scarlet Harper", "address": "16371 County Road 245"},
            {"id": "william", "client_name": "William Villalobos", "address": "6108 Cherry Glow Lane"},
            {"id": "john", "client_name": "John Doe", "address": "1 Elm St"},  # "John Smith" is someone else
            {"id": "maria1", "client_name": "Maria Lopez", "address": "411 Oak Bend Dr"},
            {"id": "maria2", "client_name": "Maria Garcia", "address": "9 Pine St"},  # two Marias: no guess
            {"id": "linda", "client_name": "Linda Nguyen", "address": "77 Willow Way"},
            {"id": "lab", "client_name": "", "address": "", "lab_metadata": {"report_number": "2034001"}},
        ]
        matches = {job_id: folder["id"] for job_id, folder in match_job_folders(jobs, root).items()}
        self.assertEqual(matches, {"scarlet": "f1", "william": "f2", "linda": "f5", "lab": "f6"})
        # A folder another assessment already uses is never taken.
        self.assertNotIn("scarlet", match_job_folders(jobs, root, taken={"f1"}))
        # Two folders that fit equally well: pick by hand.
        self.assertNotIn("scarlet", match_job_folders(jobs, root + [_folder("f7", "Harper retest")]))

    def test_new_assessment_from_a_lab_report_links_its_folder(self):
        parsed = json.loads((Path(__file__).parent / "fixtures" / "scarlet_parsed_expected.json").read_text())
        drive = FakeDrive()
        drive.tree = {**FakeDrive.tree, "root": [_folder("job", "5302 Scarlet"), _folder("other", "John Smith")]}
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "drive_client", drive),
                patch.object(mtar_services, "DRIVE_PHOTOS_FOLDER", "root"),
                patch.object(mtar_services, "parse_prolab_pdf", return_value=parsed),
                patch.object(mtar_services, "_generate_auto_drafts"),
            ):
                store.record_inbox_item("msg", "waiting", filename="Lab.pdf", report_number="2030805")
                documents.save_bytes("inbox_msg", "lab", "Lab.pdf", b"%PDF-1.7")
                job = mtar_services.create_job_from_inbox_item("msg")
                self.assertEqual((job["drive_folder_id"], job["drive_folder_name"]), ("job", "5302 Scarlet"))

    def test_sync_places_photos_by_subfolder_and_skips_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            with (
                patch.object(mtar_services, "store", store),
                patch.object(mtar_services, "documents", documents),
                patch.object(mtar_services, "drive_client", FakeDrive()),
                patch.object(mtar_services, "DRIVE_PHOTOS_FOLDER", "root"),
            ):
                job = mtar_services.create_field_job()
                job["client_name"] = "Scarlet Harper"
                job["areas"] = [new_area("COAT CLOSET")]
                job = store.save(job)

                job, result = mtar_services.sync_drive_photos(job)
                self.assertEqual(job["drive_folder_id"], "job")
                self.assertEqual(result["imported"], 8)
                self.assertEqual(result["areas_created"], ["Attic"])
                self.assertEqual(result["unplaced"], [])

                photos = job["photos"]
                by_role = {}
                for photo in photos:
                    by_role.setdefault(photo["role"], []).append(photo)
                self.assertEqual(len(by_role["property"]), 1)
                self.assertEqual(len(by_role["outdoor"]), 1)
                self.assertEqual(len(by_role["environment"]), 1)

                coat = next(a for a in job["areas"] if a["name"] == "COAT CLOSET")
                coat_kinds = sorted(p["kind"] for p in photos if p.get("area_id") == coat["id"])
                self.assertEqual(coat_kinds, ["inspection", "sampling", "thermal"])

                # The loose photo waits in Unsorted and is left out of the report.
                self.assertEqual([p["drive_file_id"] for p in by_role["unsorted"]], ["loose"])
                self.assertNotIn("unsorted", mtar_services.report_photos(job))

                # A second pass imports nothing new.
                job, again = mtar_services.sync_drive_photos(job)
                self.assertEqual(again["imported"], 0)
                self.assertEqual(len(job["photos"]), 8)

    def _synced_job(self, store, documents):
        job = mtar_services.create_field_job()
        job["client_name"] = "Scarlet Harper"
        job = store.save(job)
        job, _ = mtar_services.sync_drive_photos(job)
        return job

    def _patches(self, store, documents):
        return (
            patch.object(mtar_services, "store", store),
            patch.object(mtar_services, "documents", documents),
            patch.object(mtar_services, "drive_client", FakeDrive()),
            patch.object(mtar_services, "DRIVE_PHOTOS_FOLDER", "root"),
        )

    def test_unsorted_photo_is_filed_where_the_user_picks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            p1, p2, p3, p4 = self._patches(store, documents)
            with p1, p2, p3, p4:
                job = self._synced_job(store, documents)
                loose = next(p for p in job["photos"] if p["role"] == "unsorted")
                attic = next(a for a in job["areas"] if a["name"] == "Attic")

                job = mtar_services.assign_photo(job, loose["id"], "area", attic["id"])
                filed = next(p for p in job["photos"] if p["id"] == loose["id"])
                self.assertEqual((filed["role"], filed["area_id"]), ("area", attic["id"]))
                self.assertEqual(len(mtar_services.report_photos(job)[attic["id"]]), 2)

                # Filing as the cover replaces the existing property photo.
                job = mtar_services.assign_photo(job, loose["id"], "property")
                self.assertEqual([p["id"] for p in job["photos"] if p["role"] == "property"], [loose["id"]])

                with self.assertRaises(ValueError):
                    mtar_services.assign_photo(job, loose["id"], "area", "missing")

    def test_deleted_photo_is_not_imported_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "db.sqlite3")
            documents = FileDocumentStore(Path(tmp) / "docs")
            p1, p2, p3, p4 = self._patches(store, documents)
            with p1, p2, p3, p4:
                job = self._synced_job(store, documents)
                loose = next(p for p in job["photos"] if p["role"] == "unsorted")
                job = mtar_services.delete_photo(job, loose["id"])

                job, again = mtar_services.sync_drive_photos(store.get(job["id"]))
                self.assertEqual(again["imported"], 0)
                self.assertFalse(any(p.get("drive_file_id") == "loose" for p in job["photos"]))


if __name__ == "__main__":
    unittest.main()
