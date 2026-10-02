from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from document_store import FileDocumentStore
from drive_photos import FOLDER_MIME, find_job_folder, folder_id_from_link
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
                self.assertEqual(result["imported"], 7)
                self.assertEqual(result["areas_created"], ["Attic"])
                self.assertEqual(result["unplaced"], ["IMG_0001.jpg"])

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

                # A second pass imports nothing new.
                job, again = mtar_services.sync_drive_photos(job)
                self.assertEqual(again["imported"], 0)
                self.assertEqual(len(job["photos"]), 7)


if __name__ == "__main__":
    unittest.main()
