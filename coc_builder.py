from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import re
from typing import BinaryIO

import fitz


COMPANY_DEFAULTS = {
    "company": "MOLD TESTING & REMOVAL LLC",
    "address": "2031 JOHN WEST RD SUITE 119",
    "city": "DALLAS",
    "state": "TX",
    "zip": "75228",
    "contact": "AZEEM IQBAL",
    "phone": "8177185086",
    "email": "help@moldtestingandremoval.com",
}

SAMPLE_TYPE_CODES = {
    "AIR SAMPLE": "P15",
    "PRO-15": "P15",
    "PRO15": "P15",
    "P15": "P15",
    "AIR-O-CELL": "AOC",
    "AOC": "AOC",
    "BREEZE": "BRZ",
    "BRZ": "BRZ",
    "SPORE TRAP": "ST",
    "ST": "ST",
    "SURFACE SAMPLE": "SW",
    "SWAB": "SW",
    "SW": "SW",
    "TAPE": "T",
    "TAPE LIFT": "T",
    "TAPE-LIFT": "T",
    "BULK": "B",
    "CARPET": "CA",
    "DUST": "D",
    "WATER": "W",
    "SOIL": "SO",
    "PAINT": "P",
}

SERIAL_FIELDS = [f"Text Field {n}" for n in range(90, 100)]
LOCATION_FIELDS = [f"Text Field {n}" for n in range(100, 110)]
TURNAROUND_FIELDS = [f"Text Field {n}" for n in range(110, 120)]
FLOW_LITERS_FIELDS = [f"Text Field {n}" for n in range(120, 130)]
FLOW_MINUTES_FIELDS = [f"Text Field {n}" for n in range(130, 140)]
SAMPLE_TYPE_FIELDS = ["Text Field 164"] + [f"Text Field {n}" for n in range(141, 150)]
MOLD_ANALYSIS_CHECKBOXES = [f"Check Box {n}" for n in range(160, 170)]


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _as_bytes(pdf: bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(pdf, (bytes, bytearray)):
        return bytes(pdf)
    position = None
    try:
        position = pdf.tell()
    except Exception:
        pass
    data = pdf.read()
    if position is not None:
        try:
            pdf.seek(position)
        except Exception:
            pass
    return data


def _sample_type_code(sample: dict) -> str:
    explicit = _clean(sample.get("sample_type_code"))
    if explicit:
        return explicit.upper()
    for key in ("lab_sample_type", "type"):
        candidate = _clean(sample.get(key)).upper()
        if candidate in SAMPLE_TYPE_CODES:
            return SAMPLE_TYPE_CODES[candidate]
    return ""


def _sample_location(sample: dict, areas: list[dict]) -> str:
    name = _clean(sample.get("name"))
    if name:
        return name
    location = _clean(sample.get("location"))
    if location:
        return location
    area_id = sample.get("area_id")
    for area in areas:
        if area.get("id") == area_id:
            return _clean(area.get("name"))
    return "Outdoor Control" if sample.get("outdoor_control") else ""


def _number_text(value) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _clean(value)
    return str(int(number)) if number.is_integer() else str(number)


def build_coc_payload(
    job: dict,
    *,
    company: dict | None = None,
    turnaround_code: str = "ND",
    report_delivery: str = "Email",
    report_type: str = "Detailed",
) -> dict:
    """Convert the existing MTAR job dict into a PRO-LAB COC payload.

    The payload is UI/framework independent. It can be produced from Streamlit,
    an API, or a future background automation without changing the PDF
    renderer.
    """
    company_data = dict(COMPANY_DEFAULTS)
    company_data.update(company or {})
    areas = job.get("areas", [])

    samples = []
    for sample in job.get("samples", []):
        flow_liters = sample.get("flow_rate_liters")
        flow_minutes = sample.get("flow_rate_minutes")
        serial = (
            sample.get("serial_number")
            or sample.get("lab_serial_number")
            or ""
        )
        samples.append(
            {
                "serial_number": _clean(serial),
                "collection_location": _sample_location(sample, areas),
                "turnaround_code": _clean(sample.get("turnaround_code") or turnaround_code).upper(),
                "flow_rate_liters": _number_text(flow_liters),
                "flow_rate_minutes": _number_text(flow_minutes),
                "sample_type_code": _sample_type_code(sample),
                "mold_analysis": bool(sample.get("mold_analysis", True)),
                "outdoor_control": bool(sample.get("outdoor_control")),
            }
        )

    return {
        "company": company_data,
        "report_delivery": report_delivery,
        "report_type": report_type,
        "property": {
            "name": _clean(job.get("client_name")),
            "address": _clean(job.get("address")),
            "city": _clean(job.get("city")),
            "state": _clean(job.get("state") or "TX"),
            "zip": _clean(job.get("zip")),
            "phone": _clean(job.get("property_phone")),
        },
        "sampling_date": job.get("inspection_date"),
        "sampling_time": job.get("sampling_time"),
        "relinquished_by": _clean(job.get("relinquished_by") or company_data.get("contact")),
        "relinquished_date": job.get("relinquished_date") or job.get("inspection_date"),
        "relative_humidity": job.get("humidity"),
        "temperature": job.get("temperature"),
        "weather": {
            "fog": bool(job.get("weather_fog")),
            "rain": bool(job.get("weather_rain")),
            "snow": bool(job.get("weather_snow")),
            "wind": bool(job.get("weather_wind")),
        },
        "samples": samples,
    }


def validate_coc_payload(payload: dict) -> list[str]:
    issues: list[str] = []
    prop = payload.get("property", {})
    if not _clean(prop.get("address")):
        issues.append("Property address")
    if not _clean(prop.get("city")):
        issues.append("Property city")
    if not _clean(prop.get("state")):
        issues.append("Property state")
    if not _clean(prop.get("zip")):
        issues.append("Property ZIP")

    samples = payload.get("samples", [])
    if not samples:
        issues.append("At least one sample")
    if len(samples) > 10:
        issues.append("PRO-LAB COC supports a maximum of 10 samples")

    for index, sample in enumerate(samples[:10], 1):
        if not _clean(sample.get("serial_number")):
            issues.append(f"Sample {index}: serial number")
        if not _clean(sample.get("collection_location")):
            issues.append(f"Sample {index}: collection location")
        code = _clean(sample.get("sample_type_code")).upper()
        if not code:
            issues.append(f"Sample {index}: sample type code")
        if code in {"P15", "AOC", "BRZ", "ST"}:
            if not _clean(sample.get("flow_rate_liters")):
                issues.append(f"Sample {index}: flow rate liters")
            if not _clean(sample.get("flow_rate_minutes")):
                issues.append(f"Sample {index}: flow rate minutes")
    return issues


def _coerce_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _coerce_time(value) -> tuple[str, str, str] | None:
    if isinstance(value, datetime):
        hour, minute = value.hour, value.minute
    else:
        text = _clean(value)
        if not text:
            return None
        parsed = None
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                parsed = datetime.strptime(text.upper(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        hour, minute = parsed.hour, parsed.minute
    meridiem = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour:02d}", f"{minute:02d}", meridiem


def _field_map(page: fitz.Page) -> dict[str, fitz.Widget]:
    return {widget.field_name: widget for widget in (page.widgets() or [])}


def _set_text(fields: dict[str, fitz.Widget], name: str, value) -> None:
    widget = fields.get(name)
    if not widget or value is None:
        return
    widget.field_value = str(value)
    widget.update()


def _set_checkbox(fields: dict[str, fitz.Widget], name: str, checked: bool) -> None:
    widget = fields.get(name)
    if not widget:
        return
    widget.field_value = "Yes" if checked else "Off"
    widget.update()


def _set_date_parts(fields: dict[str, fitz.Widget], names: tuple[str, str, str], value) -> None:
    parsed = _coerce_date(value)
    if not parsed:
        return
    values = (f"{parsed.month:02d}", f"{parsed.day:02d}", f"{parsed.year:04d}")
    for name, part in zip(names, values):
        _set_text(fields, name, part)


def fill_coc_pdf(template_pdf: bytes | bytearray | BinaryIO, payload: dict) -> bytes:
    """Fill the known fields in the provided PRO-LAB COC AcroForm.

    Unknown/unsupported fields are left untouched rather than guessed. The
    returned PDF remains fillable so the consultant can review it before use.
    """
    issues = validate_coc_payload(payload)
    if issues:
        raise ValueError("COC is incomplete: " + "; ".join(issues))

    doc = fitz.open(stream=_as_bytes(template_pdf), filetype="pdf")
    try:
        if len(doc) != 1:
            raise ValueError("Expected a one-page PRO-LAB COC template")
        page = doc[0]
        fields = _field_map(page)

        company = payload.get("company", {})
        for field, key in {
            "Text Field 1": "company",
            "Text Field 2": "address",
            "Text Field 3": "city",
            "Text Field 5": "state",
            "Text Field 4": "zip",
            "Text Field 80": "contact",
            "Text Field 81": "phone",
            "Text Field 82": "email",
        }.items():
            _set_text(fields, field, company.get(key, ""))

        delivery = _clean(payload.get("report_delivery")).lower()
        _set_checkbox(fields, "Check Box 16", delivery == "online")
        _set_checkbox(fields, "Check Box 17", delivery == "email")
        report_type = _clean(payload.get("report_type")).lower()
        _set_checkbox(fields, "Check Box 19", report_type == "standard")
        _set_checkbox(fields, "Check Box 20", report_type == "detailed")

        prop = payload.get("property", {})
        for field, key in {
            "Text Field 84": "name",
            "Text Field 85": "address",
            "Text Field 86": "city",
            "Text Field 88": "state",
            "Text Field 87": "zip",
            "Text Field 89": "phone",
        }.items():
            _set_text(fields, field, prop.get(key, ""))

        _set_date_parts(fields, ("Text Field 18", "Text Field 150", "Text Field 151"), payload.get("sampling_date"))
        time_parts = _coerce_time(payload.get("sampling_time"))
        if time_parts:
            hour, minute, meridiem = time_parts
            _set_text(fields, "Text Field 19", hour)
            _set_text(fields, "Text Field 152", minute)
            _set_checkbox(fields, "Check Box 157", meridiem == "AM")
            _set_checkbox(fields, "Check Box 158", meridiem == "PM")

        _set_text(fields, "Text Field 21", payload.get("relinquished_by", ""))
        _set_date_parts(fields, ("Text Field 20", "Text Field 153", "Text Field 154"), payload.get("relinquished_date"))
        _set_text(fields, "Text Field 23", _number_text(payload.get("relative_humidity")))
        _set_text(fields, "Text Field 24", _number_text(payload.get("temperature")))

        weather = payload.get("weather", {})
        _set_checkbox(fields, "Check Box 21", bool(weather.get("fog")))
        _set_checkbox(fields, "Check Box 22", bool(weather.get("rain")))
        _set_checkbox(fields, "Check Box 34", bool(weather.get("snow")))
        _set_checkbox(fields, "Check Box 35", bool(weather.get("wind")))

        for index, sample in enumerate(payload.get("samples", [])[:10]):
            _set_text(fields, SERIAL_FIELDS[index], sample.get("serial_number", ""))
            _set_text(fields, LOCATION_FIELDS[index], sample.get("collection_location", ""))
            _set_text(fields, TURNAROUND_FIELDS[index], sample.get("turnaround_code", ""))
            _set_text(fields, FLOW_LITERS_FIELDS[index], sample.get("flow_rate_liters", ""))
            _set_text(fields, FLOW_MINUTES_FIELDS[index], sample.get("flow_rate_minutes", ""))
            _set_text(fields, SAMPLE_TYPE_FIELDS[index], sample.get("sample_type_code", ""))
            _set_checkbox(fields, MOLD_ANALYSIS_CHECKBOXES[index], bool(sample.get("mold_analysis", True)))

        output = BytesIO()
        doc.save(output, garbage=4, deflate=True)
        return output.getvalue()
    finally:
        doc.close()
