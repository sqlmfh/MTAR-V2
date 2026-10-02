"""Google Drive photo intake.

Inspectors upload photos from their phone into one Google Drive folder per
assessment. MTAR reads that folder and places each photo by its subfolder:

    MTAR Photos/                      <- DRIVE_PHOTOS_FOLDER (optional root)
        Scarlet Harper - 16371 County Road 245/   <- one folder per assessment
            Property/                 -> cover photo
            Outdoor/                  -> outdoor control sampling photos
            RH/                       -> environmental / RH meter photo
            Coat Closet/              -> inspection photos for that area
                Sampling/             -> sampling photos for that area
                Thermal/              -> thermal images for that area
            IMG_0001.jpg              -> loose photos go to Unsorted; the user files them in MTAR

Only the folder structure decides where a photo goes; MTAR never guesses
from image content. Files already imported are skipped by Drive file ID.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Callable

FOLDER_MIME = "application/vnd.google-apps.folder"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")

PROPERTY_NAMES = {"PROPERTY", "EXTERIOR", "COVER", "PROPERTY EXTERIOR", "HOUSE"}
OUTDOOR_NAMES = {"OUTDOOR", "OUTDOOR CONTROL", "OUTSIDE", "CONTROL", "OUTDOOR CONTROL SAMPLE"}
ENVIRONMENT_NAMES = {"RH", "ENVIRONMENT", "ENVIRONMENTAL", "HUMIDITY", "RH METER", "INDOOR RH"}
KIND_NAMES = {
    "THERMAL": "thermal",
    "THERMAL IMAGING": "thermal",
    "FLIR": "thermal",
    "IR": "thermal",
    "SAMPLING": "sampling",
    "SAMPLE": "sampling",
    "SAMPLES": "sampling",
    "INSPECTION": "inspection",
    "MOISTURE": "inspection",
}


def _key(value: str) -> str:
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", str(value or "")).upper().split())


def folder_id_from_link(value: str) -> str:
    """Accept a Drive folder link or a bare folder ID."""
    value = str(value or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value) else ""


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _is_image(item: dict) -> bool:
    return item.get("mimeType", "").startswith("image/") or item.get("name", "").lower().endswith(IMAGE_EXTENSIONS)


class DriveClient:
    """Read-only Google Drive adapter.

    Uses the same OAuth client as Gmail. The refresh token must have been
    granted the ``drive.readonly`` scope (DRIVE_REFRESH_TOKEN, falling back to
    GMAIL_REFRESH_TOKEN when one token was issued for both scopes).
    """

    SCOPE = "https://www.googleapis.com/auth/drive.readonly"

    def __init__(self) -> None:
        self._service = None

    @staticmethod
    def _refresh_token() -> str:
        return os.environ.get("DRIVE_REFRESH_TOKEN") or os.environ.get("GMAIL_REFRESH_TOKEN") or ""

    def configured(self) -> bool:
        return bool(
            os.environ.get("GMAIL_CLIENT_ID")
            and os.environ.get("GMAIL_CLIENT_SECRET")
            and self._refresh_token()
        )

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.configured():
            raise RuntimeError("Google Drive credentials are not configured")
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            token=None,
            refresh_token=self._refresh_token(),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            scopes=[self.SCOPE],
        )
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def list_children(self, folder_id: str) -> list[dict]:
        service = self._get_service()
        items: list[dict] = []
        page_token = None
        while True:
            response = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, createdTime)",
                    orderBy="name",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            items.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    def download(self, file_id: str) -> bytes:
        return self._get_service().files().get_media(fileId=file_id, supportsAllDrives=True).execute()


def find_job_folder(job: dict, root_children: list[dict]) -> dict | None:
    """Find the assessment's folder inside the root by client name or street address."""
    client = _key(job.get("client_name"))
    address = _key(job.get("address"))
    folders = [item for item in root_children if item.get("mimeType") == FOLDER_MIME]
    matches = [
        folder for folder in folders
        if (client and client in _key(folder.get("name")))
        or (address and address in _key(folder.get("name")))
    ]
    # Only link automatically when exactly one folder matches.
    return matches[0] if len(matches) == 1 else None


@dataclass
class DriveSyncResult:
    imported: int = 0
    skipped: int = 0
    areas_created: list[str] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def plan_job_photos(job: dict, client, folder_id: str) -> tuple[list[dict], list[str]]:
    """Walk the assessment folder and decide each photo's slot.

    Returns (planned photos, unplaced file names). A planned photo is
    {"file": drive item, "role", "area_name", "kind", "path"}. Photos that sit
    loose in the job folder, or in a subfolder MTAR does not recognise, get the
    role "unsorted" so the user can pick their section in the app.
    """
    planned: list[dict] = []
    unplaced: list[str] = []

    for item in client.list_children(folder_id):
        name_key = _key(item.get("name"))
        if item.get("mimeType") != FOLDER_MIME:
            if _is_image(item):
                # Loose photos in the job folder go to Unsorted for the user to file.
                planned.append({"file": item, "role": "unsorted", "area_name": None, "kind": "inspection", "path": ""})
            continue

        if name_key in PROPERTY_NAMES:
            role, kind = "property", "inspection"
        elif name_key in OUTDOOR_NAMES:
            role, kind = "outdoor", "sampling"
        elif name_key in ENVIRONMENT_NAMES:
            role, kind = "environment", "environment"
        else:
            role, kind = "area", "inspection"

        for child in client.list_children(item["id"]):
            if child.get("mimeType") == FOLDER_MIME:
                sub_kind = KIND_NAMES.get(_key(child.get("name")))
                path = f"{item['name']}/{child.get('name')}"
                for grandchild in client.list_children(child["id"]):
                    if not _is_image(grandchild):
                        continue
                    if role == "area" and sub_kind:
                        planned.append({"file": grandchild, "role": "area", "area_name": item["name"], "kind": sub_kind, "path": path})
                    else:
                        planned.append({"file": grandchild, "role": "unsorted", "area_name": None, "kind": "inspection", "path": path})
            elif _is_image(child):
                planned.append({
                    "file": child, "role": role, "area_name": item["name"] if role == "area" else None,
                    "kind": kind, "path": item["name"],
                })

    return planned, unplaced


def sync_job_photos(
    job: dict,
    client,
    *,
    add_photo: Callable[..., dict],
    add_area: Callable[[dict, str], dict],
) -> tuple[dict, DriveSyncResult]:
    """Import new photos from the assessment's linked Drive folder.

    ``add_photo(job, content, filename, role, area_id=, kind=, drive_file_id=, drive_path=)``
    stores one photo and returns the saved job. ``add_area(job, name)`` creates
    an inspection area for a Drive folder that matches no existing area.
    """
    result = DriveSyncResult()
    folder_id = job.get("drive_folder_id")
    if not folder_id:
        return job, result

    # Remember every file ever imported, so a photo deleted in MTAR stays deleted.
    known = set(job.get("drive_imported_ids", []))
    known |= {photo.get("drive_file_id") for photo in job.get("photos", []) if photo.get("drive_file_id")}
    planned, result.unplaced = plan_job_photos(job, client, folder_id)

    # The newest-named property photo wins; only one cover photo is kept.
    property_items = [p for p in planned if p["role"] == "property"]
    planned = [p for p in planned if p["role"] != "property"] + property_items[-1:]

    for item in planned:
        file = item["file"]
        if file["id"] in known:
            result.skipped += 1
            continue

        area_id = None
        if item["role"] == "area":
            area = next((a for a in job.get("areas", []) if _key(a.get("name")) == _key(item["area_name"])), None)
            if area is None:
                job = add_area(job, item["area_name"])
                area = job["areas"][-1]
                result.areas_created.append(item["area_name"])
            area_id = area["id"]

        try:
            content = client.download(file["id"])
            job = add_photo(
                job, content, file.get("name", "photo.jpg"), item["role"],
                area_id=area_id, kind=item["kind"], drive_file_id=file["id"], drive_path=item.get("path", ""),
            )
        except Exception as exc:
            result.errors.append(f"{file.get('name')}: {exc}")
            continue
        known.add(file["id"])
        job.setdefault("drive_imported_ids", []).append(file["id"])
        result.imported += 1

    return job, result
