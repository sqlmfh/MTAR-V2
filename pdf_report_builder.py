from __future__ import annotations

from io import BytesIO
from pathlib import Path
from textwrap import wrap

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from report_builder import MOLD_DESCRIPTIONS

BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "assets"

PAGE_W, PAGE_H = letter
LEFT = 78
RIGHT = PAGE_W - 78
TOP = PAGE_H - 72
BOTTOM = 52

NAVY = colors.HexColor("#183F58")
LIGHT_BLUE = colors.HexColor("#D5E8F0")
RED = colors.HexColor("#D93645")
TEXT = colors.HexColor("#111111")


def _date_text(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%B %d, %Y")
    return str(value or "")


def _safe_text(value) -> str:
    return str(value or "").replace("\u2014", "-").replace("\u2013", "-")


def _photos(value) -> list[dict]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [v if isinstance(v, dict) else {"content": v} for v in value]
    if isinstance(value, dict):
        return [value]
    return [{"content": value}]


def _image_reader(content):
    if content is None:
        return None
    try:
        if hasattr(content, "seek"):
            content.seek(0)
        return ImageReader(content)
    except Exception:
        return None


def _draw_contain(c: canvas.Canvas, content, x: float, y: float, w: float, h: float):
    image = _image_reader(content)
    if not image:
        return
    try:
        iw, ih = image.getSize()
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        c.drawImage(
            image,
            x + (w - dw) / 2,
            y + (h - dh) / 2,
            width=dw,
            height=dh,
            preserveAspectRatio=True,
            mask="auto",
        )
    except Exception:
        return


def _tdlr_mark(c: canvas.Canvas):
    mark = ASSET_DIR / "Azeem_TDLR_Signature.png"
    if mark.exists():
        _draw_contain(c, str(mark), PAGE_W - 62, PAGE_H - 58, 42, 42)


def _cover_header(c: canvas.Canvas):
    logo = ASSET_DIR / "MTAR_logo.png"
    if logo.exists():
        _draw_contain(c, str(logo), LEFT, PAGE_H - 140, 195, 85)

    _tdlr_mark(c)
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 10)
    y = PAGE_H - 82
    for line in [
        "Mold Testing and Removal",
        "2031 John West Rd. #119",
        "Dallas, TX 75228",
        "(817) 718-5086",
        "help@moldtestingandremoval.com",
    ]:
        c.drawRightString(PAGE_W - LEFT, y, line)
        y -= 13


def _header(c: canvas.Canvas):
    # Interior pages in the Scarlet reference use the small TDLR mark only.
    _tdlr_mark(c)


def _footer(c: canvas.Canvas, page_no: int):
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 9)
    c.drawCentredString(PAGE_W / 2, 24, str(page_no))


def _section_title(c: canvas.Canvas, text: str, y: float, size: float = 16) -> float:
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", size)
    c.drawString(LEFT, y, _safe_text(text).upper())
    return y - size - 5


def _wrap_lines(text: str, width_chars: int = 92) -> list[str]:
    lines: list[str] = []
    for paragraph in _safe_text(text).splitlines() or [""]:
        if not paragraph:
            lines.append("")
        else:
            lines.extend(wrap(paragraph, width=max(20, width_chars), break_long_words=False))
    return lines


def _paragraph(
    c: canvas.Canvas,
    text: str,
    y: float,
    *,
    x: float = LEFT,
    width_chars: int = 92,
    font: str = "Helvetica",
    size: float = 10,
    leading: float = 13,
    bold_prefix: str | None = None,
) -> float:
    c.setFillColor(TEXT)
    if bold_prefix and _safe_text(text).startswith(bold_prefix):
        c.setFont("Helvetica-Bold", size)
        c.drawString(x, y, bold_prefix)
        offset = c.stringWidth(bold_prefix, "Helvetica-Bold", size)
        c.setFont(font, size)
        remainder = _safe_text(text)[len(bold_prefix):]
        # For short labeled lines, keep the remainder on the same line.
        if len(remainder) < width_chars - len(bold_prefix):
            c.drawString(x + offset, y, remainder)
            return y - leading

    c.setFont(font, size)
    for line in _wrap_lines(text, width_chars):
        c.drawString(x, y, line)
        y -= leading
    return y


def _new_page(c: canvas.Canvas, page_no: int, *, header: bool = False) -> tuple[int, float]:
    if page_no:
        _footer(c, page_no)
        c.showPage()
    page_no += 1
    if header:
        _header(c)
        return page_no, PAGE_H - 160
    return page_no, TOP


def _photo_grid(
    c: canvas.Canvas,
    entries: list[dict],
    y_top: float,
    *,
    cols: int = 3,
    cell_h: float = 118,
    max_rows: int = 4,
) -> tuple[float, int]:
    if not entries:
        return y_top, 0
    gap = 3
    grid_w = RIGHT - LEFT
    cell_w = (grid_w - gap * (cols - 1)) / cols
    count = min(len(entries), cols * max_rows)
    for idx, entry in enumerate(entries[:count]):
        row, col = divmod(idx, cols)
        x = LEFT + col * (cell_w + gap)
        y = y_top - (row + 1) * cell_h - row * gap
        _draw_contain(c, entry.get("content"), x, y, cell_w, cell_h)
    rows = (count + cols - 1) // cols
    return y_top - rows * cell_h - max(0, rows - 1) * gap, count


def _sample_for_area(job: dict, area_id: str) -> dict | None:
    return next((s for s in job.get("samples", []) if s.get("area_id") == area_id), None)


def _outdoor_sample(job: dict) -> dict | None:
    return next((s for s in job.get("samples", []) if s.get("outdoor_control")), None)


def _sample_label(sample: dict | None) -> str:
    if not sample:
        return ""
    return sample.get("lab_coc_line") or sample.get("name") or ""


def _letter_finding(area: dict) -> str:
    finding = _safe_text(area.get("finding", ""))
    if finding == "Mold levels not elevated":
        return "No mold detected"
    return finding


def _draw_cover(c: canvas.Canvas, job: dict, photos: dict, page_no: int):
    _cover_header(c)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 178, "MOLD ASSESSMENT REPORT")

    property_photos = _photos(photos.get("property"))
    if property_photos:
        _draw_contain(c, property_photos[0].get("content"), 126, 250, 360, 360)

    y = 215
    y = _section_title(c, "Client & Property:", y, 13)
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 10)
    for line in [
        job.get("client_name", ""),
        job.get("address", ""),
        f"{job.get('city', '')}, {job.get('state', '')} {job.get('zip', '')}".strip(),
    ]:
        c.drawString(LEFT, y, _safe_text(line))
        y -= 13

    y -= 12
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(LEFT, y, "ASSESSMENT DATE:")
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 10)
    c.drawString(LEFT + 110, y, _date_text(job.get("inspection_date")))
    y -= 18
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(LEFT, y, "REPORT DATE:")
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 10)
    c.drawString(LEFT + 88, y, _date_text(job.get("report_date")))
    _footer(c, page_no)


def _draw_samples_page(c: canvas.Canvas, job: dict, page_no: int):
    _header(c)
    y = PAGE_H - 180
    y = _section_title(c, "Samples Taken:", y, 15)
    indoor_num = 0
    for sample in job.get("samples", []):
        if sample.get("outdoor_control"):
            line = "Exterior control sample (outdoor air)"
        else:
            indoor_num += 1
            media = "Air Sample" if sample.get("type") == "Air Sample" else "Swab"
            location = sample.get("location") or sample.get("lab_location") or sample.get("name") or "Interior"
            line = f"Sample {indoor_num}: {media} taken at {location}"
        y = _paragraph(c, line, y, size=11, leading=17)
    _footer(c, page_no)


def _draw_letter_page(c: canvas.Canvas, job: dict, page_no: int):
    _header(c)
    y = PAGE_H - 170
    y = _paragraph(c, "State Licensed Mold Assessment Consultant:", y, font="Helvetica-Bold", size=10)
    y = _paragraph(c, "Azeem Iqbal - TDLR MAC #2189", y, size=10)
    y -= 8
    y = _paragraph(c, "Report Date:", y, font="Helvetica-Bold", size=10)
    y = _paragraph(c, _date_text(job.get("report_date")), y, size=10)
    y -= 10
    y = _paragraph(c, "To whom it may concern,", y, size=10)
    y -= 6

    address = f"{job.get('address', '')}, {job.get('city', '')}, {job.get('state', '')} {job.get('zip', '')}"
    paragraphs = [
        f"Mold Testing and Removal was hired to conduct a mold assessment at the property located at {address}. The purpose of this assessment was to evaluate the indoor air quality, identify potential sources of fungal growth, and provide recommendations for remediation.",
        "The assessment included a visual inspection, moisture mapping using a Protimeter Moisture Meter, and the collection of bioaerosol (air) and surface (swab) samples. Samples were collected from the interior of the property and the exterior for control purposes.",
        "The samples were sent to PRO-LAB, an accredited laboratory, for viable mold/fungi analysis.",
    ]
    for p in paragraphs:
        y = _paragraph(c, p, y, size=10, leading=13)
        y -= 7

    outcome = job.get("report_outcome", "Pending consultant review")
    if outcome == "Mold remediation required":
        y = _paragraph(c, "Based on the laboratory results and visual inspection, active mold growth was confirmed in the following areas:", y, size=10)
        for area in job.get("areas", []):
            y = _paragraph(c, f"- {area.get('name', '')} - {_letter_finding(area)}", y, x=LEFT + 12, size=10)
        y -= 5
        y = _paragraph(
            c,
            "This letter serves as official notification that professional mold remediation is required to return the property to a normal fungal ecology (Condition 1). The property should be remediated by a State Licensed Mold Remediation Contractor (MRC) in accordance with the Texas Mold Assessment and Remediation Rules (TMARR).",
            y,
            font="Helvetica-Bold",
            size=10,
        )
    else:
        y = _paragraph(
            c,
            "DRAFT - CONSULTANT REVIEW REQUIRED. Laboratory data has been imported automatically. The licensed Mold Assessment Consultant must review inspection observations, moisture conditions, photographs, and the final report conclusion before release.",
            y,
            font="Helvetica-Bold",
            size=10,
        )

    y -= 18
    y = _paragraph(c, "Sincerely,", y, size=10)
    sig = ASSET_DIR / "Signature.png"
    if sig.exists():
        _draw_contain(c, str(sig), LEFT, y - 55, 90, 48)
        y -= 58
    y = _paragraph(c, "Azeem Iqbal", y, font="Helvetica-Bold", size=10)
    y = _paragraph(c, "State of Texas Licensed Mold Assessment Consultant", y, size=9)
    _paragraph(c, "TDLR MAC #2189 (Exp. 10/24/2027)", y, size=9)
    _footer(c, page_no)


def _draw_outdoor_page(c: canvas.Canvas, job: dict, photos: dict, page_no: int):
    _header(c)
    y = PAGE_H - 165
    y = _section_title(c, "Outdoor Control Sample", y, 15)
    y = _paragraph(c, "An air sample is taken outside to serve as a baseline for all other air samples to be compared against.", y, size=10)
    y -= 12

    outdoor = _outdoor_sample(job)
    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(TEXT)
    c.drawCentredString(PAGE_W / 2, y, "OUTDOOR CONTROL SAMPLE")
    y -= 15
    c.setFont("Helvetica-BoldOblique", 10)
    c.drawCentredString(PAGE_W / 2, y, f"COC / LINE #: {_sample_label(outdoor)}")
    y -= 10

    outdoor_photos = _photos(photos.get("outdoor"))
    y, _ = _photo_grid(c, outdoor_photos[:3], y, cols=3, cell_h=140, max_rows=1)
    y -= 20

    y = _section_title(c, "Visual Observations & Moisture Readings", y, 14)
    humidity = job.get("humidity")
    if humidity is None:
        env = "Environmental Conditions: Indoor relative humidity (rH) was not entered. Consultant review required."
    else:
        status = "within the recommended range (30-50%)" if humidity <= 50 else "above the recommended range (30-50%)"
        env = f"Environmental Conditions: The indoor relative humidity (rH) was recorded at {humidity}%, which is {status}."
    y = _paragraph(c, env, y, size=10)

    environment_photos = _photos(photos.get("environment"))
    if environment_photos:
        _draw_contain(c, environment_photos[0].get("content"), 236, max(80, y - 155), 140, 145)
    _footer(c, page_no)


def _area_photo_groups(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    sampling, inspection, thermal = [], [], []
    for entry in entries:
        kind = (entry.get("kind") or "inspection").lower()
        if kind == "thermal":
            thermal.append(entry)
        elif kind == "sampling":
            sampling.append(entry)
        else:
            inspection.append(entry)
    return sampling, inspection, thermal


def _draw_area_pages(c: canvas.Canvas, job: dict, photos: dict, page_no: int) -> int:
    for area in job.get("areas", []):
        entries = _photos(photos.get(area.get("id")))
        sampling, inspection, thermal = _area_photo_groups(entries)
        sample = _sample_for_area(job, area.get("id"))

        page_no += 1
        _header(c)
        y = PAGE_H - 165
        y = _section_title(c, area.get("name", "Inspection Area"), y, 15)
        if area.get("lab_summary"):
            y = _paragraph(c, area["lab_summary"], y, size=10)
        if area.get("description"):
            y = _paragraph(c, f"Visual Observations: {area['description']}", y, size=10)
        y -= 8
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(PAGE_W / 2, y, _safe_text(area.get("name", "")).upper())
        y -= 15
        c.setFont("Helvetica-BoldOblique", 10)
        c.drawCentredString(PAGE_W / 2, y, f"COC / LINE #: {_sample_label(sample)}")
        y -= 8

        if sampling:
            y, _ = _photo_grid(c, sampling[:3], y, cols=3, cell_h=140, max_rows=1)
            y -= 15

        moisture = area.get("moisture_notes") or "Moisture assessment not entered. Consultant review required."
        y = _paragraph(c, f"Moisture Assessment: {moisture}", y, size=10)
        y -= 10

        remaining = inspection[:]
        y, used = _photo_grid(c, remaining, y, cols=3, cell_h=140, max_rows=2)
        remaining = remaining[used:]
        _footer(c, page_no)

        while remaining:
            c.showPage()
            page_no += 1
            _header(c)
            y = PAGE_H - 145
            y, used = _photo_grid(c, remaining, y, cols=3, cell_h=140, max_rows=4)
            remaining = remaining[used:]
            _footer(c, page_no)

        if thermal:
            c.showPage()
            page_no += 1
            _header(c)
            y = PAGE_H - 165
            y = _paragraph(
                c,
                area.get("thermal_notes") or "Thermal Imaging: Consultant review required before final release.",
                y,
                size=10,
            )
            y -= 12
            _photo_grid(c, thermal, y, cols=2, cell_h=230, max_rows=2)
            _footer(c, page_no)

        c.showPage()
    return page_no


def _air_summary_rows(job: dict) -> list[tuple[str, str, str, str]]:
    samples = {s["id"]: s for s in job.get("samples", [])}
    rows = []
    # Match the customer-report summary: one useful comparison row per
    # sample/fungal type, prioritizing Penicillium/Aspergillus.
    for sample in job.get("samples", []):
        if sample.get("type") != "Air Sample":
            continue
        sample_rows = [r for r in job.get("air_lab_rows", []) if r.get("sample_id") == sample.get("id")]
        preferred = next((r for r in sample_rows if r.get("fungal_type") == "Penicillium/Aspergillus"), None)
        row = preferred or (sample_rows[0] if sample_rows else None)
        if not row:
            continue
        location = "Outdoor Control" if sample.get("outdoor_control") else (sample.get("location") or sample.get("name") or "")
        rows.append((location, row.get("fungal_type", ""), str(row.get("spore_count", "")), row.get("interpretation", "")))
    return rows


def _draw_table(c: canvas.Canvas, x: float, y: float, widths: list[float], headers: list[str], rows: list[tuple], row_h: float = 18) -> float:
    total_w = sum(widths)
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.6)
    c.setFillColor(LIGHT_BLUE)
    c.rect(x, y - row_h, total_w, row_h, fill=1, stroke=1)
    cx = x
    for idx, (header, width) in enumerate(zip(headers, widths)):
        if idx:
            c.line(cx, y, cx, y - row_h * (len(rows) + 1))
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(cx + 4, y - 12, header)
        cx += width

    current_y = y - row_h
    for row in rows:
        current_y -= row_h
        c.setFillColor(colors.white)
        c.rect(x, current_y, total_w, row_h, fill=1, stroke=1)
        cx = x
        for idx, (value, width) in enumerate(zip(row, widths)):
            if idx:
                c.line(cx, current_y + row_h, cx, current_y)
            c.setFillColor(RED if "UNUSUAL" in _safe_text(value).upper() else TEXT)
            c.setFont("Helvetica-Bold" if "UNUSUAL" in _safe_text(value).upper() else "Helvetica", 8.5)
            c.drawString(cx + 4, current_y + 5, _safe_text(value)[:44])
            cx += width
    return current_y


def _draw_mold_entry(
    c: canvas.Canvas,
    mold_type: str,
    description: str,
    dangerous: bool,
    y: float,
) -> float:
    size = 8.6
    leading = 11
    label = f"{mold_type}: "
    c.setFont("Helvetica-Bold", size)
    c.setFillColor(RED if dangerous else TEXT)
    c.drawString(LEFT, y, label)
    label_w = c.stringWidth(label, "Helvetica-Bold", size)

    words = _safe_text(description).split()
    lines: list[str] = []
    current = ""
    first_limit = RIGHT - (LEFT + label_w)
    full_limit = RIGHT - LEFT

    for word in words:
        trial = word if not current else current + " " + word
        limit = first_limit if not lines else full_limit
        if c.stringWidth(trial, "Helvetica", size) <= limit:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    c.setFillColor(TEXT)
    c.setFont("Helvetica", size)
    if lines:
        c.drawString(LEFT + label_w, y, lines[0])
        for line in lines[1:]:
            y -= leading
            c.drawString(LEFT, y, line)
    return y - leading


def _draw_lab_page(c: canvas.Canvas, job: dict, page_no: int):
    _header(c)
    y = PAGE_H - 165
    y = _section_title(c, "Laboratory Results Analysis", y, 15)
    y = _paragraph(c, "Samples were submitted to PRO-LAB (an accredited laboratory) for analysis. The following summarizes the findings compared to the outdoor control sample.", y, size=10)
    y -= 10

    y = _section_title(c, "Air Sample Comparison (Bioaerosol)", y, 13)
    rows = _air_summary_rows(job)
    y = _draw_table(c, LEFT, y, [105, 190, 75, 115], ["Location", "Fungal Type", "Spores/m³", "Interpretation"], rows)
    y -= 28

    surface_rows = []
    samples = {s["id"]: s for s in job.get("samples", [])}
    for row in job.get("surface_lab_rows", []):
        sample = samples.get(row.get("sample_id"), {})
        surface_rows.append((
            sample.get("location") or sample.get("name") or "",
            "Swab",
            row.get("result", ""),
        ))
    if surface_rows:
        y = _section_title(c, "Surface Sample Results (Swab)", y, 13)
        y = _draw_table(c, LEFT, y, [175, 140, 170], ["Location", "Sample Type", "Result"], surface_rows)
        y -= 26

    y = _section_title(c, "Mold Types Identified", y, 13)
    for mold_type in job.get("mold_types", []):
        description, dangerous = MOLD_DESCRIPTIONS.get(mold_type, ("", False))
        if not description:
            continue
        y = _draw_mold_entry(c, mold_type, description, dangerous, y)
        y -= 4
        if y < 70:
            break
    _footer(c, page_no)


def _draw_conclusions(c: canvas.Canvas, job: dict, page_no: int):
    _header(c)
    y = PAGE_H - 165
    y = _section_title(c, "Conclusions", y, 15)
    y = _paragraph(c, "Based on the visual inspection, moisture readings, and laboratory results, the following conclusions are made:", y, size=10)
    y -= 8
    for idx, area in enumerate(job.get("areas", []), 1):
        y = _paragraph(c, f"{idx}. {area.get('name', '')}: {area.get('finding', '')}.", y, size=10)
        y -= 6

    y -= 12
    y = _section_title(c, "Recommendations", y, 15)
    outcome = job.get("report_outcome", "Pending consultant review")
    if outcome == "Mold remediation required":
        y = _paragraph(c, "To return the property to a normal fungal ecology (Condition 1), the following remediation steps are recommended:", y, size=10)
        recs = [
            ("Professional Remediation", "Hire a State Licensed Mold Remediation Contractor (MRC) to prepare a work plan based on a Mold Remediation Protocol prepared by a TDLR Mold Assessment Consultant."),
            ("Containment", "Establish critical barriers (polyethylene sheeting) around affected areas to prevent spore dispersion. Establish negative air pressure."),
            ("Removal", "Remove and discard affected drywall and materials. Continue removal 2 feet beyond visible growth."),
            ("Cleaning", "HEPA vacuum and damp-wipe all remaining structural surfaces within the containment."),
            ("Humidity Control", "Dehumidification is required to lower the indoor RH to between 30-50%."),
            ("Clearance Testing", "After remediation, a Post-Remediation Assessment (clearance test) must be performed by a TDLR Mold Assessment Consultant."),
        ]
    else:
        recs = [("Consultant Review", "Final remediation recommendations are withheld until the licensed consultant completes review.")]
    y -= 6
    for title, body in recs:
        y = _paragraph(c, f"- {title}: {body}", y, x=LEFT + 4, size=9.5, leading=12, width_chars=96)
        y -= 4

    y -= 8
    _paragraph(c, "This report is generated in accordance with the Texas Mold Assessment and Remediation Rules (TMARR). Limitations: This inspection is limited to the areas accessible at the time of inspection.", y, font="Helvetica-Oblique", size=8.5)
    _footer(c, page_no)


def _draw_terms(c: canvas.Canvas, page_no: int):
    _header(c)
    y = PAGE_H - 165
    y = _section_title(c, "Terms and Conditions", y, 15)
    terms = [
        ("Inspection Limitation", "This inspection and the information set forth in the report is provided solely for the purpose of verifying that certain structural or physical characteristics exist at the Location Address listed. The undersigned and company representative does not make a health or safety certification or warranty, express or implied, of any kind."),
        ("Limitation of Liability", "The Client agrees that Inspector's liability for errors and/or omissions shall be limited to the maximum of a full refund of the fee paid for the inspection. The Client agrees to assume all risk of loss which exceeds the fee paid."),
        ("Sampling Limitations", "Mold spore sampling results represent conditions at the time and location of sampling only. Conditions can change rapidly due to environmental factors, occupant activities, and remediation efforts."),
        ("Health Disclaimer", "This report does not constitute medical advice. Individuals with health concerns related to potential mold exposure should consult with a qualified healthcare professional."),
        ("Report Usage", "This report is prepared exclusively for the named client and may not be reproduced or distributed to third parties without written consent from Mold Testing and Removal."),
    ]
    for title, body in terms:
        y = _paragraph(c, f"{title}: {body}", y, size=9, leading=12, width_chars=98)
        y -= 8

    y -= 16
    c.setFont("Helvetica-BoldOblique", 10)
    c.drawCentredString(PAGE_W / 2, y, "- Laboratory Report Attached -")
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawCentredString(PAGE_W / 2, y, "PRO-LAB Certificate of Mold Analysis follows this page")
    y -= 50
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(PAGE_W / 2, y, "Mold Testing and Removal")
    y -= 16
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 9)
    c.drawCentredString(PAGE_W / 2, y, "2031 John West Rd. #119 | Dallas, TX 75228")
    y -= 13
    c.drawCentredString(PAGE_W / 2, y, "(817) 718-5086 | help@moldtestingandremoval.com")
    _footer(c, page_no)


def create_customer_pdf(job: dict, photos: dict | None = None) -> BytesIO:
    """Create a deterministic customer-facing assessment PDF.

    The layout follows the Scarlet customer report: cover, sample summary,
    consultant letter, outdoor/environment page, inspection-area/photo pages,
    thermal pages, lab analysis, conclusions/recommendations, and terms.
    """
    photos = photos or {}
    output = BytesIO()
    c = canvas.Canvas(output, pagesize=letter, pageCompression=1)

    page_no = 1
    _draw_cover(c, job, photos, page_no)
    c.showPage()

    page_no += 1
    _draw_samples_page(c, job, page_no)
    c.showPage()

    page_no += 1
    _draw_letter_page(c, job, page_no)
    c.showPage()

    page_no += 1
    _draw_outdoor_page(c, job, photos, page_no)
    c.showPage()

    # Area renderer manages its own page endings.
    page_no = _draw_area_pages(c, job, photos, page_no)

    page_no += 1
    _draw_lab_page(c, job, page_no)
    c.showPage()

    page_no += 1
    _draw_conclusions(c, job, page_no)
    c.showPage()

    page_no += 1
    _draw_terms(c, page_no)

    c.save()
    output.seek(0)
    return output
