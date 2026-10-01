from __future__ import annotations

from datetime import datetime
from io import BytesIO
import re
from typing import BinaryIO, Iterable

import fitz


AIR_SAMPLE_CODES = {"PRO-15", "PRO15", "P15", "AOC", "BRZ", "BREEZE", "SPORE TRAP", "ST"}
SURFACE_SAMPLE_CODES = {
    "SWAB",
    "TAPE",
    "TAPE LIFT",
    "TAPE-LIFT",
    "BULK",
    "CARPET",
    "CARPET CASSETTE",
    "SURFACE",
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _upper(value: str) -> str:
    return _norm(value).upper()


def _as_bytes(pdf: bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(pdf, (bytes, bytearray)):
        return bytes(pdf)
    pos = None
    try:
        pos = pdf.tell()
    except Exception:
        pass
    data = pdf.read()
    if pos is not None:
        try:
            pdf.seek(pos)
        except Exception:
            pass
    return data


def _extract_metadata(first_page_text: str) -> dict:
    lines = [_norm(x) for x in first_page_text.splitlines() if _norm(x)]

    def after(label: str) -> str:
        target = _upper(label)
        for i, line in enumerate(lines):
            if _upper(line).rstrip(":") == target.rstrip(":"):
                if i + 1 < len(lines):
                    return lines[i + 1]
        return ""

    return {
        "project_name": after("Project Name:"),
        "report_number": after("Report Number:"),
        "received_date": after("Received Date:"),
        "report_date": after("Report Date:"),
        "prepared_for": after("Prepared for:"),
        "test_location": _extract_test_location(lines),
    }


def _extract_test_location(lines: list[str]) -> str:
    for i, line in enumerate(lines):
        if _upper(line).rstrip(":") == "TEST LOCATION":
            parts = []
            for next_line in lines[i + 1 : i + 4]:
                if _upper(next_line).startswith(("REPORT NUMBER", "RECEIVED DATE", "REPORT DATE")):
                    break
                parts.append(next_line)
            return ", ".join(parts)
    return ""


def _words_by_line(words: list[tuple], tolerance: float = 1.4) -> list[dict]:
    usable = [
        {"x0": float(w[0]), "y0": float(w[1]), "x1": float(w[2]), "y1": float(w[3]), "text": str(w[4])}
        for w in words
    ]
    usable.sort(key=lambda w: (w["y0"], w["x0"]))
    lines: list[dict] = []
    for word in usable:
        if not lines or abs(word["y0"] - lines[-1]["y"]) > tolerance:
            lines.append({"y": word["y0"], "words": [word]})
        else:
            lines[-1]["words"].append(word)
            ys = [x["y0"] for x in lines[-1]["words"]]
            lines[-1]["y"] = sum(ys) / len(ys)
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = _norm(" ".join(w["text"] for w in line["words"]))
    return lines


def _left_text(line: dict, cutoff: float = 130.0) -> str:
    return _norm(" ".join(w["text"] for w in line["words"] if w["x0"] < cutoff))


def _find_row(lines: list[dict], label: str, start_y: float = 0.0) -> dict | None:
    target = _upper(label)
    for line in lines:
        if line["y"] < start_y:
            continue
        if target in _upper(_left_text(line)):
            return line
    return None


def _column_bounds(centers: list[float], data_left: float = 130.0, page_right: float = 590.0) -> list[tuple[float, float]]:
    if not centers:
        return []
    centers = sorted(centers)
    bounds = []
    for i, center in enumerate(centers):
        left = data_left if i == 0 else (centers[i - 1] + center) / 2.0
        right = page_right if i == len(centers) - 1 else (center + centers[i + 1]) / 2.0
        # PRO-LAB result pages reserve up to four table slots. Keep the final
        # populated sample from swallowing text in intentionally-blank slots.
        if len(centers) < 4:
            nominal_width = 110.0
            left = max(left, center - nominal_width / 2.0)
            right = min(right, center + nominal_width / 2.0)
        bounds.append((left, right))
    return bounds


def _words_in_region(words: list[dict], y0: float, y1: float, x0: float, x1: float) -> list[dict]:
    return sorted(
        [w for w in words if y0 <= w["y0"] < y1 and x0 <= (w["x0"] + w["x1"]) / 2.0 < x1],
        key=lambda w: (w["y0"], w["x0"]),
    )


def _region_text(words: list[dict], y0: float, y1: float, x0: float, x1: float) -> str:
    region = _words_in_region(words, y0, y1, x0, x1)
    if not region:
        return ""
    out: list[str] = []
    current_y = None
    current_line: list[str] = []
    for w in region:
        if current_y is None or abs(w["y0"] - current_y) <= 1.4:
            current_line.append(w["text"])
            current_y = w["y0"] if current_y is None else current_y
        else:
            out.append(" ".join(current_line))
            current_line = [w["text"]]
            current_y = w["y0"]
    if current_line:
        out.append(" ".join(current_line))
    return _norm(" ".join(out))


def _numeric_nearest(words: list[dict], y: float, center: float, tolerance_y: float = 2.0, max_dx: float = 50.0):
    candidates = []
    for w in words:
        if abs(w["y0"] - y) > tolerance_y:
            continue
        txt = w["text"].replace(",", "")
        if not re.fullmatch(r"\d+(?:\.\d+)?", txt):
            continue
        cx = (w["x0"] + w["x1"]) / 2.0
        dx = abs(cx - center)
        if dx <= max_dx:
            candidates.append((dx, txt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    txt = candidates[0][1]
    return int(float(txt)) if float(txt).is_integer() else float(txt)

def _is_air_sample(sample_type: str, volume: str) -> bool:
    """Classify PRO-LAB sample media without treating 'NA' volume as air."""
    media = _upper(sample_type)
    if media in SURFACE_SAMPLE_CODES:
        return False
    if media in AIR_SAMPLE_CODES:
        return True

    volume_text = _upper(volume).replace(" ", "")
    if volume_text in {"", "NA", "N/A", "NOTAPPLICABLE"}:
        return False
    return bool(re.search(r"\d+(?:\.\d+)?L\b", volume_text))


def _surface_mark_present(
    words: list[dict],
    y: float,
    left: float,
    right: float,
    tolerance_y: float = 2.5,
) -> bool:
    """Return True when a surface-result cell contains an X/presence mark."""
    text = _region_text(words, y - tolerance_y, y + tolerance_y + 7.0, left, right)
    tokens = {_upper(token) for token in re.findall(r"[A-Za-z]+", text)}
    return "X" in _upper(text).split() or bool(tokens & {"PRESENT", "POSITIVE"})


def _sample_row_text(
    words: list[dict],
    row: dict | None,
    left: float,
    right: float,
    height: float = 18.0,
) -> str:
    if not row:
        return ""
    return _region_text(words, row["y"] - 1.0, row["y"] + height, left, right)


def _sample_key(sample: dict, page_number: int, index: int) -> str:
    return sample.get("coc_line") or sample.get("serial_number") or f"page{page_number}_sample{index + 1}"


def _parse_result_page(page: fitz.Page, page_number: int) -> tuple[list[dict], list[str]]:
    text = page.get_text("text")
    if "COC / LINE #" not in text or "DETERMINATION" not in text:
        return [], []

    words_raw = page.get_text("words")
    all_words = [
        {"x0": float(w[0]), "y0": float(w[1]), "x1": float(w[2]), "y1": float(w[3]), "text": str(w[4])}
        for w in words_raw
    ]
    lines = _words_by_line(words_raw)
    warnings: list[str] = []

    coc_row = _find_row(lines, "COC")
    sample_type_row = _find_row(lines, "SAMPLE TYPE")
    determination_row = _find_row(lines, "DETERMINATION")
    identification_row = _find_row(lines, "IDENTIFICATION")
    total_row = _find_row(lines, "TOTAL SPORES")

    if not coc_row or not sample_type_row or not determination_row:
        return [], [f"Page {page_number}: result table headings were found, but key rows could not be located."]

    report_number_words = [
        w for w in coc_row["words"]
        if w["x0"] >= 130 and re.fullmatch(r"\d{5,}", w["text"].replace(",", ""))
    ]
    centers = sorted((w["x0"] + w["x1"]) / 2.0 for w in report_number_words)
    if not centers:
        return [], [f"Page {page_number}: could not determine populated sample columns."]

    bounds = _column_bounds(centers, page_right=float(page.rect.width) - 20)
    location_row = _find_row(lines, "LOCATION")
    volume_row = _find_row(lines, "VOLUME")
    serial_row = _find_row(lines, "SERIAL NUMBER")
    collection_row = _find_row(lines, "COLLECTION DATE")
    analysis_date_row = _find_row(lines, "ANALYSIS DATE")
    background_row = _find_row(lines, "BACKGROUND DEBRIS")
    observations_row = _find_row(lines, "OBSERVATIONS")

    row_order = [
        r
        for r in [
            location_row,
            coc_row,
            sample_type_row,
            volume_row,
            serial_row,
            collection_row,
            analysis_date_row,
            determination_row,
            identification_row,
        ]
        if r
    ]

    def next_y(row: dict, fallback: float | None = None) -> float:
        later = sorted(r["y"] for r in row_order if r["y"] > row["y"] + 0.5)
        if later:
            return later[0]
        return fallback if fallback is not None else row["y"] + 13.0

    samples: list[dict] = []
    for idx, (center, (left, right)) in enumerate(zip(centers, bounds)):
        coc_text = _region_text(all_words, coc_row["y"] - 1, next_y(coc_row), left, right)
        coc_match = re.search(r"(\d+)\s*-\s*(\d+)", coc_text)
        coc_line = f"{coc_match.group(1)}-{coc_match.group(2)}" if coc_match else _norm(coc_text)

        location = ""
        if location_row:
            location = _region_text(all_words, location_row["y"] - 6, coc_row["y"] - 0.2, left, right)

        sample_type = _region_text(all_words, sample_type_row["y"] - 1, next_y(sample_type_row), left, right)
        volume = _region_text(all_words, volume_row["y"] - 1, next_y(volume_row), left, right) if volume_row else ""
        serial = _region_text(all_words, serial_row["y"] - 1, next_y(serial_row), left, right) if serial_row else ""
        collection_date = (
            _region_text(all_words, collection_row["y"] - 1, next_y(collection_row), left, right)
            if collection_row
            else ""
        )
        analysis_date = (
            _region_text(all_words, analysis_date_row["y"] - 1, next_y(analysis_date_row), left, right)
            if analysis_date_row
            else ""
        )
        determination = _region_text(
            all_words,
            determination_row["y"] - 1,
            determination_row["y"] + 9.5,
            left,
            right,
        )

        sample = {
            "key": "",
            "page": page_number,
            "location": location,
            "coc_line": coc_line,
            "sample_type": sample_type,
            "volume": volume,
            "serial_number": serial,
            "collection_date": collection_date,
            "analysis_date": analysis_date,
            "determination": _upper(determination),
            "total_spores": None,
            "fungi": {},
            "background_debris": _sample_row_text(all_words, background_row, left, right),
            "observations": _sample_row_text(all_words, observations_row, left, right),
            "is_air": _is_air_sample(sample_type, volume),
        }
        sample["key"] = _sample_key(sample, page_number, idx)
        samples.append(sample)

    # Read only the actual result-table band. Narrative definitions and mold
    # reference pages never become findings.
    if identification_row and total_row:
        species_lines = [
            line
            for line in lines
            if identification_row["y"] + 5 < line["y"] < total_row["y"] - 1 and _left_text(line)
        ]
        for line in species_lines:
            species = _norm(_left_text(line))
            if not species or _upper(species) in {"RAW COUNT", "SPORES PER M³", "PERCENT OF TOTAL", "MOLD PRESENT"}:
                continue

            for sample, center, (left, right) in zip(samples, centers, bounds):
                if sample["is_air"]:
                    value = _numeric_nearest(all_words, line["y"], center)
                    if value is not None:
                        sample["fungi"][species] = int(value)
                elif _surface_mark_present(all_words, line["y"], left, right):
                    sample["fungi"][species] = "Present"

        for sample, center in zip(samples, centers):
            if sample["is_air"]:
                sample["total_spores"] = _numeric_nearest(all_words, total_row["y"], center)

    return samples, warnings


def parse_prolab_pdf(pdf: bytes | bytearray | BinaryIO) -> dict:
    """Parse structured PRO-LAB result tables without scanning narrative pages.

    Mold detections and lab determinations are read only from the result table.
    Text such as "ELEVATED means..." and species reference pages is ignored.
    """
    pdf_bytes = _as_bytes(pdf)
    result = {
        "metadata": {},
        "samples": [],
        "warnings": [],
        "page_count": 0,
    }
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        result["warnings"].append(f"Could not open PDF: {exc}")
        return result

    try:
        result["page_count"] = len(doc)
        if len(doc):
            result["metadata"] = _extract_metadata(doc[0].get_text("text"))
        for page_index in range(len(doc)):
            samples, warnings = _parse_result_page(doc[page_index], page_index + 1)
            result["samples"].extend(samples)
            result["warnings"].extend(warnings)
    finally:
        doc.close()

    if not result["samples"]:
        result["warnings"].append("No structured PRO-LAB sample result table was found.")
    return result


def suggested_mapping(parsed: dict, job: dict) -> dict[str, str]:
    """Suggest lab-sample -> job-sample mappings without mutating the job.

    Matching is intentionally conservative and deterministic:
    1. outdoor controls map to outdoor controls;
    2. exact sample serial numbers are preferred;
    3. exact sample/location names are used next;
    4. only then do we fall back to the next unused sample of the same media type.

    This supports the inspection-first workflow where sample serial numbers are
    entered before the PRO-LAB PDF arrives.
    """
    mappings: dict[str, str] = {}
    parsed_samples = parsed.get("samples", [])
    job_samples = job.get("samples", [])

    def norm(value) -> str:
        return _upper(str(value or ""))

    def job_serial(sample: dict) -> str:
        return norm(sample.get("serial_number") or sample.get("lab_serial_number"))

    def job_name(sample: dict) -> str:
        return norm(sample.get("name") or sample.get("location"))

    def same_media(lab: dict, sample: dict) -> bool:
        if lab.get("is_air"):
            return sample.get("type") == "Air Sample"
        return sample.get("type") in {"Swab", "Surface Sample"}

    used: set[str] = set()

    for lab in parsed_samples:
        location = norm(lab.get("location"))
        determination = norm(lab.get("determination"))
        is_control = "OUTDOOR" in location or determination == "CONTROL"

        candidates = [
            sample
            for sample in job_samples
            if sample.get("id") not in used
            and (
                sample.get("outdoor_control")
                if is_control
                else (not sample.get("outdoor_control") and same_media(lab, sample))
            )
        ]

        target = None
        serial = norm(lab.get("serial_number"))
        if serial:
            target = next(
                (sample for sample in candidates if job_serial(sample) == serial),
                None,
            )

        if target is None and location:
            target = next(
                (sample for sample in candidates if job_name(sample) == location),
                None,
            )

        if target is None:
            target = next(iter(candidates), None)

        if target:
            mappings[lab["key"]] = target["id"]
            used.add(target["id"])

    return mappings

def apply_prolab_results(
    job: dict,
    parsed: dict,
    mapping: dict[str, str],
    supported_molds: Iterable[str],
) -> dict:
    """Apply reviewed parsed results to V2 without deciding report outcome.

    Air-result row interpretations are a simple species-by-species comparison
    against the mapped outdoor control. The original PRO-LAB sample-level
    determination is preserved separately on each sample and remains visible
    for consultant review.
    """
    supported = set(supported_molds)
    sample_map = {s["id"]: s for s in job.get("samples", [])}
    air_rows: list[dict] = []
    surface_rows: list[dict] = []
    detected_molds: list[str] = []

    from models import new_air_lab_row, new_surface_lab_row

    # Build the outdoor species baseline from the reviewed sample mapping.
    outdoor_fungi: dict[str, int] = {}
    for lab in parsed.get("samples", []):
        mapped_id = mapping.get(lab.get("key", ""))
        mapped_sample = sample_map.get(mapped_id or "")
        if mapped_sample and mapped_sample.get("outdoor_control") and lab.get("is_air"):
            outdoor_fungi = {
                fungus: int(count or 0)
                for fungus, count in lab.get("fungi", {}).items()
            }
            break

    for lab in parsed.get("samples", []):
        job_sample_id = mapping.get(lab.get("key", ""))
        if not job_sample_id or job_sample_id not in sample_map:
            continue

        job_sample = sample_map[job_sample_id]
        job_sample["lab_coc_line"] = lab.get("coc_line", "")
        job_sample["lab_serial_number"] = lab.get("serial_number", "")
        job_sample["lab_sample_type"] = lab.get("sample_type", "")
        job_sample["lab_volume"] = lab.get("volume", "")
        job_sample["lab_determination"] = lab.get("determination", "")
        job_sample["lab_collection_date"] = lab.get("collection_date", "")
        job_sample["lab_analysis_date"] = lab.get("analysis_date", "")
        job_sample["lab_total_spores"] = lab.get("total_spores")
        job_sample["lab_fungi"] = dict(lab.get("fungi", {}))
        job_sample["lab_background_debris"] = lab.get("background_debris", "")
        job_sample["lab_observations"] = lab.get("observations", "")

        if lab.get("is_air"):
            for fungus, count in lab.get("fungi", {}).items():
                numeric_count = int(count or 0)
                if job_sample.get("outdoor_control"):
                    interpretation = "Baseline (Reference)"
                elif fungus in outdoor_fungi:
                    outdoor_count = outdoor_fungi[fungus]
                    interpretation = (
                        "ELEVATED"
                        if numeric_count > 0 and numeric_count >= outdoor_count
                        else "Not Elevated"
                    )
                else:
                    # Fallback only when this species is absent from the outdoor
                    # control. The sample-level lab determination is not used to
                    # overwrite a direct species comparison when one exists.
                    interpretation = (
                        "ELEVATED"
                        if _upper(lab.get("determination", "")) == "ELEVATED"
                        else "Not Elevated"
                    )

                row = new_air_lab_row(job_sample_id)
                row["fungal_type"] = fungus
                row["spore_count"] = numeric_count
                row["interpretation"] = interpretation
                air_rows.append(row)

                if (
                    not job_sample.get("outdoor_control")
                    and fungus in supported
                    and numeric_count > 0
                    and fungus not in detected_molds
                ):
                    detected_molds.append(fungus)
        else:
            row = new_surface_lab_row(job_sample_id)
            determination = _upper(lab.get("determination", ""))
            row["result"] = "UNUSUAL / Mold Present" if "UNUSUAL" in determination else "Normal"
            surface_rows.append(row)
            for fungus in lab.get("fungi", {}):
                if fungus in supported and fungus not in detected_molds:
                    detected_molds.append(fungus)

    if air_rows:
        job["air_lab_rows"] = air_rows
    if surface_rows or any(s.get("type") == "Swab" for s in job.get("samples", [])):
        job["surface_lab_rows"] = surface_rows
    if detected_molds:
        job["mold_types"] = detected_molds

    job["lab_metadata"] = dict(parsed.get("metadata", {}))
    return job


def _parse_lab_date(value: str):
    value = _norm(value)
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _split_test_location(value: str) -> dict:
    """Split a PRO-LAB test-location string into editable property fields."""
    value = _norm(value)
    result = {"address": "", "city": "", "state": "", "zip": ""}
    if not value:
        return result

    parts = [p.strip() for p in value.split(",") if p.strip()]
    if parts:
        result["address"] = parts[0]

    if len(parts) >= 3:
        result["city"] = parts[1]
        state_zip = parts[2]
    elif len(parts) == 2:
        state_zip_match = re.search(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", parts[1], re.I)
        if state_zip_match:
            # In two-part strings, the city is commonly included before state/ZIP.
            prefix = parts[1][: state_zip_match.start()].strip(" ,")
            if prefix:
                result["city"] = prefix
            state_zip = state_zip_match.group(0)
        else:
            state_zip = parts[1]
    else:
        state_zip = ""

    match = re.search(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", state_zip, re.I)
    if match:
        result["state"] = match.group(1).upper()
        result["zip"] = match.group(2)

    # Fallback for the common "street, city, ST ZIP" pattern when text
    # extraction produced an unexpected comma layout.
    if not result["zip"]:
        match = re.search(
            r"^(.*?)[,\s]+([A-Za-z .'-]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$",
            value,
        )
        if match:
            result["address"] = _norm(match.group(1))
            result["city"] = _norm(match.group(2))
            result["state"] = match.group(3).upper()
            result["zip"] = match.group(4)

    return result


def _review_area_name(location: str, index: int) -> str:
    location = _norm(location)
    generic = {
        "",
        "INDOOR",
        "INDOORS",
        "INTERIOR",
        "INTERIOR SAMPLE",
        "INDOOR SAMPLE",
        "AIR SAMPLE",
        "SAMPLE",
    }
    if _upper(location) in generic:
        return f"Area of Concern {index}"
    return location.title()


def build_draft_job_from_prolab(
    job: dict,
    parsed: dict,
    supported_molds: Iterable[str],
) -> dict[str, str]:
    """Build a review-ready V2 job directly from a parsed PRO-LAB report.

    Samples are created as independent entities. Indoor/surface samples are
    intentionally left unassigned to inspection areas so the consultant can
    create the actual inspection areas and map samples manually.
    """
    from models import new_sample

    metadata = parsed.get("metadata", {})
    project_name = _norm(metadata.get("project_name", ""))
    if project_name:
        job["client_name"] = project_name.title()

    property_fields = _split_test_location(metadata.get("test_location", ""))
    for key in ("address", "city", "state", "zip"):
        if property_fields.get(key):
            value = property_fields[key]
            job[key] = value.title() if key in ("address", "city") else value

    lab_report_date = _parse_lab_date(metadata.get("report_date", ""))
    if lab_report_date:
        job["lab_report_date"] = lab_report_date

    sample_dates = [
        _parse_lab_date(sample.get("collection_date", ""))
        for sample in parsed.get("samples", [])
    ]
    sample_dates = [d for d in sample_dates if d]
    if sample_dates:
        job["inspection_date"] = min(sample_dates)

    samples: list[dict] = []
    mapping: dict[str, str] = {}
    indoor_counter = 0
    surface_counter = 0

    for lab in parsed.get("samples", []):
        lab_location = _norm(lab.get("location", ""))
        determination = _upper(lab.get("determination", ""))
        is_control = "OUTDOOR" in _upper(lab_location) or determination == "CONTROL"
        sample_type = "Air Sample" if lab.get("is_air") else "Surface Sample"

        if is_control:
            sample = new_sample(
                sample_type="Air Sample",
                location="Outdoor Control",
                outdoor_control=True,
            )
            sample["name"] = lab_location or "Outdoor Control"
        else:
            if lab.get("is_air"):
                indoor_counter += 1
                fallback_name = f"Indoor Air Sample {indoor_counter}"
            else:
                surface_counter += 1
                fallback_name = f"Surface Sample {surface_counter}"

            sample = new_sample(
                sample_type=sample_type,
                location="",
                area_id=None,
            )
            sample["name"] = lab_location or fallback_name

        sample["lab_location"] = lab_location
        samples.append(sample)
        mapping[lab["key"]] = sample["id"]

    if not any(s.get("outdoor_control") for s in samples):
        outdoor = new_sample(
            sample_type="Air Sample",
            location="Outdoor Control",
            outdoor_control=True,
        )
        outdoor["name"] = "Outdoor Control"
        samples.insert(0, outdoor)

    # Inspection areas are deliberately separate from lab samples. The
    # consultant creates the actual areas and assigns samples in Streamlit.
    job["areas"] = []
    job["samples"] = samples
    job["air_lab_rows"] = []
    job["surface_lab_rows"] = []
    job["mold_types"] = []
    job["report_outcome"] = "Pending consultant review"
    job["humidity"] = None
    job["lab_metadata"] = dict(metadata)

    apply_prolab_results(job, parsed, mapping, supported_molds)
    return mapping



def _automated_area_finding(lab: dict) -> str:
    """Create a conservative preliminary area finding from lab facts only."""
    determination = _upper(lab.get("determination", ""))
    observations = _upper(lab.get("observations", ""))

    if not lab.get("is_air") and determination == "UNUSUAL":
        if lab.get("fungi") or "GROWTH" in observations:
            return "Active mold growth confirmed"
        return "Surface sample unusual"

    if lab.get("is_air") and determination == "ELEVATED":
        return "Elevated spore counts"

    if lab.get("is_air") and determination in {"NOT ELEVATED", "CONTROL"}:
        return "Mold levels not elevated" if determination == "NOT ELEVATED" else "Outdoor control"

    return "Needs consultant review"


def _automated_sample_summary(lab: dict) -> str:
    determination = _upper(lab.get("determination", ""))
    if lab.get("is_air"):
        if determination == "NOT ELEVATED":
            return (
                "Air Sample: Mold levels indoors were lower than mold levels observed "
                "in Outdoor Control Sample. Determination is NOT ELEVATED."
            )
        if determination == "ELEVATED":
            return (
                "Air Sample: Mold levels were elevated compared with the outdoor control "
                "sample. Determination is ELEVATED."
            )
        return f"Air Sample: Laboratory determination is {determination or 'pending review'}."

    fungi = [name for name, value in lab.get("fungi", {}).items() if value]
    if fungi:
        joined = ", ".join(fungi)
        return (
            f"Swab Sample: Sample returned positive for {joined} growth. "
            f"Determination is {determination or 'pending review'}."
        )
    return f"Surface Sample: Laboratory determination is {determination or 'pending review'}."


def build_automated_job_from_prolab(
    job: dict,
    parsed: dict,
    supported_molds: Iterable[str],
) -> dict[str, str]:
    """Create a review-ready assessment directly from a PRO-LAB report.

    Unlike the manual draft builder, this automation-oriented builder creates
    inspection-area placeholders from lab locations and assigns each
    non-control sample to its matching area. The area findings and narratives
    are explicitly preliminary lab-derived facts; moisture observations,
    photos, indoor RH, and the licensed consultant's final report outcome
    remain review items.
    """
    from models import new_area

    mapping = build_draft_job_from_prolab(job, parsed, supported_molds)
    samples_by_id = {sample["id"]: sample for sample in job.get("samples", [])}

    areas: list[dict] = []
    areas_by_name: dict[str, dict] = {}
    review_index = 0

    for lab in parsed.get("samples", []):
        sample_id = mapping.get(lab.get("key", ""))
        sample = samples_by_id.get(sample_id or "")
        if not sample:
            continue

        serial = _norm(lab.get("serial_number", ""))
        if serial:
            sample["serial_number"] = serial

        media = _upper(lab.get("sample_type", ""))
        sample["sample_type_code"] = "P15" if lab.get("is_air") else (media or "SWAB")

        if lab.get("is_air"):
            volume_match = re.search(r"(\d+(?:\.\d+)?)", str(lab.get("volume", "")))
            volume_liters = float(volume_match.group(1)) if volume_match else None
            sample["flow_rate_liters"] = 15
            if volume_liters:
                minutes = volume_liters / 15.0
                sample["flow_rate_minutes"] = int(minutes) if minutes.is_integer() else minutes
                sample["sample_volume_liters"] = volume_liters

        if sample.get("outdoor_control"):
            continue

        review_index += 1
        area_name = _review_area_name(lab.get("location", ""), review_index)
        key = _upper(area_name)
        area = areas_by_name.get(key)
        if area is None:
            area = new_area(area_name)
            area["finding"] = _automated_area_finding(lab)
            area["description"] = _automated_sample_summary(lab)
            area["source"] = "PRO-LAB"
            area["source_lab_key"] = lab.get("key", "")
            areas.append(area)
            areas_by_name[key] = area
        sample["area_id"] = area["id"]
        sample["location"] = area_name

    job["areas"] = areas
    unusual_growth = any(
        (not lab.get("is_air"))
        and _upper(lab.get("determination", "")) == "UNUSUAL"
        and (lab.get("fungi") or "GROWTH" in _upper(lab.get("observations", "")))
        for lab in parsed.get("samples", [])
    )
    elevated_air = any(
        lab.get("is_air") and _upper(lab.get("determination", "")) == "ELEVATED"
        for lab in parsed.get("samples", [])
    )
    if unusual_growth or elevated_air:
        job["suggested_report_outcome"] = "Mold remediation required"
        job["suggested_report_outcome_reason"] = "Lab findings require licensed consultant review."
    else:
        job["suggested_report_outcome"] = "No significant mold contamination identified"
        job["suggested_report_outcome_reason"] = "No elevated or unusual lab determination was detected; inspection findings still require review."

    job["automation_source"] = "PRO-LAB Gmail"
    job["automation_missing_fields"] = [
        "Indoor RH",
        "Inspection photos",
        "Moisture assessment",
        "Licensed consultant report outcome",
    ]
    return mapping
