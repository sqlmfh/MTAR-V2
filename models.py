from __future__ import annotations

from datetime import date
from uuid import uuid4


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def new_area(name: str = "") -> dict:
    return {
        "id": _id("area"),
        "name": name,
        "finding": "Needs consultant review",
        "description": "",
        "moisture_notes": "",
        "thermal_notes": "",
    }


def new_sample(
    sample_type: str = "Air Sample",
    location: str = "",
    area_id: str | None = None,
    *,
    outdoor_control: bool = False,
) -> dict:
    return {
        "id": _id("sample") if not outdoor_control else "sample_outdoor_control",
        "name": "Outdoor Control" if outdoor_control else (location or ""),
        "type": sample_type,
        "location": location,
        "area_id": area_id,
        "outdoor_control": outdoor_control,
    }


def new_air_lab_row(sample_id: str = "") -> dict:
    return {
        "id": _id("airrow"),
        "sample_id": sample_id,
        "fungal_type": "Penicillium/Aspergillus",
        "spore_count": 0,
        "interpretation": "Not Elevated",
    }


def new_surface_lab_row(sample_id: str = "") -> dict:
    return {
        "id": _id("surfrow"),
        "sample_id": sample_id,
        "result": "Normal",
    }


def new_job_state() -> dict:
    first_area = new_area()
    outdoor = new_sample(
        sample_type="Air Sample",
        location="Outdoor Control",
        outdoor_control=True,
    )
    indoor = new_sample(
        sample_type="Air Sample",
        location="",
        area_id=first_area["id"],
    )

    return {
        "client_name": "",
        "address": "",
        "city": "",
        "state": "TX",
        "zip": "",
        "inspection_date": date.today(),
        "report_date": date.today(),
        "humidity": None,
        "temperature": None,
        "general_observations": "",
        "areas": [first_area],
        "samples": [outdoor, indoor],
        "air_lab_rows": [
            {
                "id": _id("airrow"),
                "sample_id": outdoor["id"],
                "fungal_type": "Penicillium/Aspergillus",
                "spore_count": 0,
                "interpretation": "Baseline (Reference)",
            },
            {
                "id": _id("airrow"),
                "sample_id": indoor["id"],
                "fungal_type": "Penicillium/Aspergillus",
                "spore_count": 0,
                "interpretation": "Not Elevated",
            },
        ],
        "surface_lab_rows": [],
        "mold_types": ["Penicillium/Aspergillus"],
        "report_outcome": "Pending consultant review",
    }


def sample_location(sample: dict, areas: list[dict]) -> str:
    if sample.get("outdoor_control"):
        return "Outdoor Control"
    if sample.get("location"):
        return sample["location"]
    area_id = sample.get("area_id")
    for area in areas:
        if area.get("id") == area_id:
            return area.get("name") or "Unnamed Area"
    return "Unnamed Sample"


def validate_job(job: dict, lab_pdf_present: bool) -> list[str]:
    missing = []
    if not job.get("client_name", "").strip():
        missing.append("Client name")
    if not job.get("address", "").strip():
        missing.append("Property address")
    if not job.get("city", "").strip():
        missing.append("City")
    if not job.get("zip", "").strip():
        missing.append("ZIP code")
    if not any(a.get("name", "").strip() for a in job.get("areas", [])):
        missing.append("At least one inspection area")
    if job.get("humidity") is None:
        missing.append("Indoor RH")

    named_areas = [a for a in job.get("areas", []) if a.get("name", "").strip()]
    for area in named_areas:
        if not str(area.get("moisture_notes", "")).strip():
            missing.append(f"Moisture assessment for {area['name']}")

    photos = job.get("photos", [])
    if not any(photo.get("role") == "property" for photo in photos):
        missing.append("Property exterior photo")

    for area in named_areas:
        if not any(
            photo.get("role") == "area" and photo.get("area_id") == area.get("id")
            for photo in photos
        ):
            missing.append(f"Inspection photos for {area['name']}")

    if any(sample.get("outdoor_control") for sample in job.get("samples", [])):
        if not any(photo.get("role") == "outdoor" for photo in photos):
            missing.append("Outdoor control sampling photo")

    unassigned = [
        s for s in job.get("samples", [])
        if not s.get("outdoor_control") and not s.get("area_id")
    ]
    if unassigned:
        missing.append("Assign every indoor/surface sample to an inspection area")
    if not lab_pdf_present:
        missing.append("PRO-LAB PDF")
    if job.get("report_outcome") in ("", "Pending consultant review"):
        missing.append("Consultant report outcome")
    return missing
