from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from report_builder import MOLD_DESCRIPTIONS

BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "assets"

PAGE_W, PAGE_H = letter
# Measurements below are taken from the completed Scarlet customer report
# (US Letter, points, measured from the top-left corner of the page).
LEFT = 90
RIGHT = 522
TOP = PAGE_H - 72
BOTTOM = 52

NAVY = colors.HexColor("#184058")
LIGHT_BLUE = colors.HexColor("#D5E8F0")
RED = colors.HexColor("#DC3545")
UNUSUAL_FILL = colors.HexColor("#FFCCCC")
TEXT = colors.HexColor("#000000")

# Liberation Sans shares Arial's metrics, so lines wrap exactly where the
# reference report (set in Arial) wraps. Helvetica is the fallback.
BODY, BOLD, ITALIC, BOLD_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"
_liberation = {
    "Arial": "LiberationSans-Regular.ttf",
    "Arial-Bold": "LiberationSans-Bold.ttf",
    "Arial-Italic": "LiberationSans-Italic.ttf",
    "Arial-BoldItalic": "LiberationSans-BoldItalic.ttf",
}
try:
    for _name, _file in _liberation.items():
        pdfmetrics.registerFont(TTFont(_name, str(ASSET_DIR / "fonts" / _file)))
    BODY, BOLD, ITALIC, BOLD_ITALIC = _liberation
except Exception:
    pass
BODY_SIZE = 11
LEADING = 14.6
PARAGRAPH_GAP = 10

HEADING_FONT = "Helvetica-Bold"
_bebas = ASSET_DIR / "fonts" / "BebasNeue-Regular.ttf"
if _bebas.exists():
    try:
        pdfmetrics.registerFont(TTFont("BebasNeue", str(_bebas)))
        HEADING_FONT = "BebasNeue"
    except Exception:
        pass


def _date_text(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%B %d, %Y")
    return str(value or "")


def _safe_text(value) -> str:
    # Helvetica covers the em dash; only normalise characters it lacks.
    return str(value or "").replace("\u2013", "-").replace("\u200b", "")


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


def _y(top: float, size: float = BODY_SIZE) -> float:
    """Baseline for text whose glyph box starts ``top`` points below the page top."""
    return PAGE_H - top - 0.86 * size


def _tdlr_mark(c: canvas.Canvas):
    mark = ASSET_DIR / "Azeem_TDLR_Signature.png"
    if mark.exists():
        _draw_contain(c, str(mark), 554, PAGE_H - 57, 50, 49)


def _footer(c: canvas.Canvas, page_no: int):
    c.setFillColor(TEXT)
    c.setFont(BODY, BODY_SIZE)
    c.drawCentredString(PAGE_W / 2, _y(742), str(page_no))


def _heading(c: canvas.Canvas, text: str, top: float, size: float = 17, *, center: bool = False) -> float:
    """Draw a navy section heading; returns the top of the next line."""
    c.setFillColor(NAVY)
    c.setFont(HEADING_FONT, size)
    text = _safe_text(text)
    if HEADING_FONT != "BebasNeue":
        text = text.upper()
    x = (PAGE_W - c.stringWidth(text, HEADING_FONT, size)) / 2 if center else LEFT
    # Fill plus a thin outline gives Bebas the heavier weight of the reference.
    c.saveState()
    c.setStrokeColor(NAVY)
    c.setLineWidth(size * 0.035)
    heading = c.beginText(x, _y(top, size))
    heading.setFont(HEADING_FONT, size)
    heading.setTextRenderMode(2)
    heading.textOut(text)
    c.drawText(heading)
    c.restoreState()
    return top + size + 7


def _rich_lines(c: canvas.Canvas, runs: list[tuple[str, str]], width: float, size: float) -> list[list[tuple[str, str]]]:
    """Greedy word wrap of (text, font) runs into lines of runs."""
    words: list[tuple[str, str]] = []
    for text, font in runs:
        for index, word in enumerate(_safe_text(text).split(" ")):
            if word == "" and index:
                continue
            words.append((word, font))
    lines: list[list[tuple[str, str]]] = [[]]
    used = 0.0
    for word, font in words:
        if not word:
            continue
        space = c.stringWidth(" ", BODY, size) if lines[-1] else 0
        word_w = c.stringWidth(word, font, size)
        if lines[-1] and used + space + word_w > width:
            lines.append([])
            used, space = 0.0, 0
        lines[-1].append((word, font))
        used += space + word_w
    return lines


def _rich(
    c: canvas.Canvas,
    runs: list[tuple[str, str]] | str,
    top: float,
    *,
    x: float = LEFT,
    width: float | None = None,
    size: float = BODY_SIZE,
    leading: float = LEADING,
    color=TEXT,
    colors_by_font: dict | None = None,
) -> float:
    """Draw wrapped text made of differently styled runs; returns the next top."""
    if isinstance(runs, str):
        runs = [(runs, BODY)]
    # Half a point of slack reproduces the reference's line breaks exactly.
    width = width if width is not None else RIGHT - x + 0.5
    for line in _rich_lines(c, runs, width, size):
        cursor = x
        baseline = _y(top, size)
        for index, (word, font) in enumerate(line):
            if index:
                cursor += c.stringWidth(" ", BODY, size)
            c.setFillColor((colors_by_font or {}).get(font, color))
            c.setFont(font, size)
            c.drawString(cursor, baseline, word)
            cursor += c.stringWidth(word, font, size)
        top += leading
    return top


def _labeled(text: str, *, label_font: str = BOLD) -> list[tuple[str, str]]:
    """Split "Label: body" into a bold label run and a regular body run."""
    text = _safe_text(text)
    label, sep, rest = text.partition(":")
    if sep and len(label) <= 40 and rest:
        return [(label + ":", label_font), (rest, BODY)]
    return [(text, BODY)]


def _bullet(c: canvas.Canvas, runs, top: float, *, size: float = BODY_SIZE) -> float:
    c.setFillColor(TEXT)
    c.circle(LEFT + 3.2, _y(top, size) + size * 0.32, 2.6, stroke=0, fill=1)
    return _rich(c, runs, top, x=LEFT + 18, size=size)


def _new_page(c: canvas.Canvas, page_no: int, *, header: bool = False) -> tuple[int, float]:
    if page_no:
        _footer(c, page_no)
        c.showPage()
    page_no += 1
    if header:
        _tdlr_mark(c)
    return page_no, TOP


PHOTO = 140      # square photo cell
PHOTO_GAP = 4    # horizontal gap between photos
ROW_PITCH = 146  # vertical distance between photo rows
THERMAL_W, THERMAL_H = 208, 279


def _photo_row(c: canvas.Canvas, entries: list[dict], top: float, *, size: float = PHOTO) -> float:
    """Draw up to three photos centred as a row; returns the bottom (top coords)."""
    entries = entries[:3]
    if not entries:
        return top
    total = len(entries) * size + (len(entries) - 1) * PHOTO_GAP
    x = (PAGE_W - total) / 2 if len(entries) < 3 else 92
    for entry in entries:
        _draw_contain(c, entry.get("content"), x, PAGE_H - top - size, size, size)
        x += size + PHOTO_GAP
    return top + size


def _photo_grid(c: canvas.Canvas, entries: list[dict], top: float, *, max_rows: int) -> tuple[float, int]:
    """Three-column grid starting at ``top``; returns (bottom, photos used)."""
    count = min(len(entries), 3 * max_rows)
    for index, entry in enumerate(entries[:count]):
        row, col = divmod(index, 3)
        x = 92 + col * (PHOTO + PHOTO_GAP)
        _draw_contain(c, entry.get("content"), x, PAGE_H - top - row * ROW_PITCH - PHOTO, PHOTO, PHOTO)
    rows = (count + 2) // 3
    return top + rows * ROW_PITCH - (ROW_PITCH - PHOTO if rows else 0), count


def _sample_for_area(job: dict, area_id: str) -> dict | None:
    return next((s for s in job.get("samples", []) if s.get("area_id") == area_id), None)


def _outdoor_sample(job: dict) -> dict | None:
    return next((s for s in job.get("samples", []) if s.get("outdoor_control")), None)


def _sample_label(sample: dict | None) -> str:
    if not sample:
        return ""
    line = sample.get("lab_coc_line") or ""
    if line:
        # The customer report spaces the COC line as "2030805 - 1".
        return " - ".join(part.strip() for part in line.split("-", 1))
    return sample.get("name") or ""


def _sample_caption(c: canvas.Canvas, title: str, sample: dict | None, top: float) -> float:
    c.setFillColor(TEXT)
    c.setFont(BOLD, BODY_SIZE)
    c.drawCentredString(PAGE_W / 2, _y(top), _safe_text(title).upper())
    top += LEADING
    label, value = "COC / LINE #: ", _sample_label(sample)
    total = c.stringWidth(label, ITALIC, BODY_SIZE) + c.stringWidth(value, BOLD_ITALIC, BODY_SIZE)
    x = (PAGE_W - total) / 2
    c.setFont(ITALIC, BODY_SIZE)
    c.drawString(x, _y(top), label)
    c.setFont(BOLD_ITALIC, BODY_SIZE)
    c.drawString(x + c.stringWidth(label, ITALIC, BODY_SIZE), _y(top), value)
    return top + LEADING + 2


def _letter_finding(area: dict) -> str:
    finding = _safe_text(area.get("finding", ""))
    if finding == "Mold levels not elevated":
        return "No mold detected"
    return finding


def _draw_cover(c: canvas.Canvas, job: dict, photos: dict, page_no: int):
    logo = ASSET_DIR / "MTAR_logo.png"
    if logo.exists():
        _draw_contain(c, str(logo), 72, PAGE_H - 135, 210, 81)
    _tdlr_mark(c)

    c.setFillColor(TEXT)
    c.setFont(BODY, BODY_SIZE)
    top = 72
    for line in [
        "Mold Testing and Removal",
        "2031 John West Rd. #119",
        "Dallas, TX 75228",
        "(817) 718-5086",
        "help@moldtestingandremoval.com",
    ]:
        c.drawRightString(525, _y(top), line)
        top += LEADING

    _heading(c, "Mold Assessment Report", 158, 30, center=True)

    property_photos = _photos(photos.get("property"))
    if property_photos:
        _draw_contain(c, property_photos[0].get("content"), 126, PAGE_H - 577, 360, 360)

    _heading(c, "Client & Property:", 595, 15)
    c.setFillColor(TEXT)
    c.setFont(BODY, BODY_SIZE)
    top = 617
    for line in [
        job.get("client_name", ""),
        job.get("address", ""),
        f"{job.get('city', '')}, {job.get('state', '')} {job.get('zip', '')}".strip(),
    ]:
        c.drawString(LEFT, _y(top), _safe_text(line))
        top += LEADING

    for label, value, label_top in (
        ("Assessment Date:", job.get("inspection_date"), 674),
        ("Report Date:", job.get("report_date"), 695),
    ):
        _heading(c, label, label_top, 15)
        shown = label if HEADING_FONT == "BebasNeue" else label.upper()
        value_x = LEFT + c.stringWidth(shown, HEADING_FONT, 15) + 4
        c.setFillColor(TEXT)
        c.setFont(BODY, BODY_SIZE)
        c.drawString(value_x, _y(label_top + 4), _date_text(value))
    _footer(c, page_no)


def _draw_samples_page(c: canvas.Canvas, job: dict, page_no: int):
    _tdlr_mark(c)
    top = _heading(c, "Samples Taken:", 72, 15) - 1
    indoor_num = 0
    for sample in job.get("samples", []):
        if sample.get("outdoor_control"):
            line = "Exterior control sample (outdoor air)"
        else:
            indoor_num += 1
            media = "Air Sample" if sample.get("type") == "Air Sample" else "Swab"
            location = sample.get("location") or sample.get("lab_location") or sample.get("name") or "Interior"
            line = f"Sample {indoor_num}: {media} taken at {location}"
        top = _rich(c, line, top)
    _footer(c, page_no)


def _draw_letter_page(c: canvas.Canvas, job: dict, page_no: int):
    _tdlr_mark(c)
    top = 72
    top = _rich(c, [("State Licensed Mold Assessment Consultant:", BOLD)], top)
    top = _rich(c, "Azeem Iqbal — TDLR MAC #2189", top)
    top += LEADING
    top = _rich(c, [("Report Date:", BOLD)], top)
    top = _rich(c, _date_text(job.get("report_date")), top)
    top += LEADING
    top = _rich(c, "To whom it may concern,", top) + PARAGRAPH_GAP

    address = f"{job.get('address', '')}, {job.get('city', '')}, {job.get('state', '')} {job.get('zip', '')}"
    paragraphs = [
        f"Mold Testing and Removal was hired to conduct a mold assessment at the property located at {address}. The purpose of this assessment was to evaluate the indoor air quality, identify potential sources of fungal growth, and provide recommendations for remediation.",
        "The assessment included a visual inspection, moisture mapping using a Protimeter Moisture Meter, and the collection of bioaerosol (air) and surface (swab) samples. Samples were collected from the interior of the property and the exterior for control purposes.",
        "The samples were sent to PRO-LAB, an accredited laboratory, for viable mold/fungi analysis.",
    ]
    for paragraph in paragraphs:
        top = _rich(c, paragraph, top) + PARAGRAPH_GAP

    outcome = job.get("report_outcome", "Pending consultant review")
    if outcome == "Mold remediation required":
        top = _rich(
            c,
            [
                ("Based on the laboratory results and visual inspection,", BODY),
                ("active mold growth was confirmed", BOLD_ITALIC),
                ("in the following areas:", BODY),
            ],
            top,
        ) + PARAGRAPH_GAP
        for area in job.get("areas", []):
            top = _bullet(c, f"{area.get('name', '')} — {_letter_finding(area)}", top)
        top += PARAGRAPH_GAP
        top = _rich(
            c,
            [("This letter serves as official notification that professional mold remediation is required to return the property to a normal fungal ecology (Condition 1). The property should be remediated by a State Licensed Mold Remediation Contractor (MRC) in accordance with the Texas Mold Assessment and Remediation Rules (TMARR).", BOLD)],
            top,
        )
    else:
        top = _rich(
            c,
            [("DRAFT - CONSULTANT REVIEW REQUIRED. Laboratory data has been imported automatically. The licensed Mold Assessment Consultant must review inspection observations, moisture conditions, photographs, and the final report conclusion before release.", BOLD)],
            top,
        )

    top += 2 * LEADING + 5
    top = _rich(c, "Sincerely,", top) + PARAGRAPH_GAP
    top = _rich(c, [("Azeem Iqbal", BOLD)], top, size=12)
    sig = ASSET_DIR / "Signature.png"
    if sig.exists():
        _draw_contain(c, str(sig), 92, PAGE_H - top - 48, 64, 45)
    top += 52
    top = _rich(c, "State of Texas Licensed Mold Assessment Consultant", top)
    _rich(c, "TDLR MAC #2189 (Exp. 10/24/2027)", top)
    _footer(c, page_no)


def _draw_outdoor_page(c: canvas.Canvas, job: dict, photos: dict, page_no: int):
    _tdlr_mark(c)
    top = _heading(c, "Outdoor Control Sample", 96)
    top = _rich(c, "An air sample is taken outside to serve as a baseline for all other air samples to be compared against.", top)
    top = _sample_caption(c, "Outdoor Control Sample", _outdoor_sample(job), top + PARAGRAPH_GAP)

    top = _photo_row(c, _photos(photos.get("outdoor"))[:3], top + 2)
    top = _heading(c, "Visual Observations & Moisture Readings", top + 13)

    humidity = job.get("humidity")
    if humidity is None:
        runs = [("Environmental Conditions:", BOLD), ("Indoor relative humidity (rH) was not entered. Consultant review required.", BODY)]
    else:
        value = f"{humidity:g}" if isinstance(humidity, (int, float)) else str(humidity)
        status = "within the recommended range (30-50%)" if float(humidity) <= 50 else "above the recommended range (30-50%)"
        runs = [
            ("Environmental Conditions:", BOLD),
            ("The indoor relative humidity (rH) was recorded at", BODY),
            (f"{value}%,", BOLD),
            (f"which is {status}.", BODY),
        ]
    top = _rich(c, runs, top)

    environment_photos = _photos(photos.get("environment"))
    if environment_photos:
        _photo_row(c, environment_photos[:1], top + 12)
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
        _tdlr_mark(c)
        top = _heading(c, area.get("name", "Inspection Area"), 72)
        if area.get("lab_summary"):
            top = _rich(c, _labeled(area["lab_summary"]), top)
        if area.get("description"):
            top = _rich(c, [("Visual Observations:", BOLD), (area["description"], BODY)], top)
        top = _sample_caption(c, area.get("name", ""), sample, top + PARAGRAPH_GAP)

        if sampling:
            top = _photo_row(c, sampling[:3], top + 2) + 14

        moisture = area.get("moisture_notes") or "Moisture assessment not entered. Consultant review required."
        top = _rich(c, [("Moisture Assessment:", BOLD), (moisture, BODY)], top) + 12

        rows_left = max(0, int((PAGE_H - BOTTOM - 40 - top + (ROW_PITCH - PHOTO)) // ROW_PITCH))
        _, used = _photo_grid(c, inspection, top, max_rows=min(rows_left, 4))
        remaining = inspection[used:]
        _footer(c, page_no)

        while remaining:
            c.showPage()
            page_no += 1
            _tdlr_mark(c)
            _, used = _photo_grid(c, remaining, 74, max_rows=4)
            remaining = remaining[used:]
            _footer(c, page_no)

        if thermal:
            c.showPage()
            page_no += 1
            _tdlr_mark(c)
            notes = area.get("thermal_notes") or "Thermal Imaging: Consultant review required before final release."
            _rich(c, _labeled(notes), 72)
            for index, entry in enumerate(thermal[:4]):
                row, col = divmod(index, 2)
                x = 92 + col * (THERMAL_W + 3)
                y_top = 113 + row * (THERMAL_H + 5)
                _draw_contain(c, entry.get("content"), x, PAGE_H - y_top - THERMAL_H, THERMAL_W, THERMAL_H)
            _footer(c, page_no)

        c.showPage()
    return page_no


def _air_summary_rows(job: dict) -> list[tuple[str, str, str, str]]:
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


def _draw_table(c: canvas.Canvas, top: float, widths: list[float], headers: list[str], rows: list[tuple]) -> float:
    """Bordered table in the reference style; returns the bottom (top coords)."""
    x0, pad, line_h = 84, 6, 12.5
    edges = [x0]
    for width in widths:
        edges.append(edges[-1] + width)

    def cell_lines(text: str, width: float, font: str) -> list[str]:
        return [" ".join(w for w, _ in line) for line in _rich_lines(c, [(text, font)], width - 2 * pad, BODY_SIZE)] or [""]

    c.setLineWidth(0.6)
    c.setStrokeColor(colors.black)
    header_h = 14
    c.setFillColor(LIGHT_BLUE)
    c.rect(x0, PAGE_H - top - header_h, edges[-1] - x0, header_h, fill=1, stroke=0)
    for header, left in zip(headers, edges):
        c.setFillColor(TEXT)
        c.setFont(BOLD, BODY_SIZE)
        c.drawString(left + pad, _y(top + 1), header)
    row_top = top + header_h

    for row in rows:
        styled = []
        for value, width in zip(row, widths):
            text = _safe_text(value)
            unusual = "UNUSUAL" in text.upper() or "MOLD PRESENT" in text.upper()
            font = BOLD if unusual else BODY
            styled.append((cell_lines(text, width, font), font, unusual))
        height = max(len(lines) for lines, _, _ in styled) * line_h + 1.5
        for (lines, font, unusual), left, right in zip(styled, edges, edges[1:]):
            if unusual:
                c.setFillColor(UNUSUAL_FILL)
                c.rect(left, PAGE_H - row_top - height, right - left, height, fill=1, stroke=0)
            c.setFillColor(RED if unusual else TEXT)
            c.setFont(font, BODY_SIZE)
            for index, line in enumerate(lines):
                c.drawString(left + pad, _y(row_top + index * line_h), line)
        c.line(x0, PAGE_H - row_top, edges[-1], PAGE_H - row_top)
        row_top += height

    # Outer frame, column rules and the closing rule.
    c.setStrokeColor(colors.black)
    c.line(x0, PAGE_H - top, edges[-1], PAGE_H - top)
    c.line(x0, PAGE_H - row_top, edges[-1], PAGE_H - row_top)
    for edge in edges:
        c.line(edge, PAGE_H - top, edge, PAGE_H - row_top)
    return row_top


def _draw_lab_page(c: canvas.Canvas, job: dict, page_no: int):
    _tdlr_mark(c)
    top = _heading(c, "Laboratory Results Analysis", 72)
    top = _rich(c, "Samples were submitted to PRO-LAB (an accredited laboratory) for analysis. The following summarizes the findings compared to the outdoor control sample.", top)

    top = _heading(c, "Air Sample Comparison (Bioaerosol)", top + 9)
    top = _draw_table(c, top, [109, 133, 83, 108], ["Location", "Fungal Type", "Spores/m³", "Interpretation"], _air_summary_rows(job))

    surface_rows = []
    samples = {s["id"]: s for s in job.get("samples", [])}
    for row in job.get("surface_lab_rows", []):
        sample = samples.get(row.get("sample_id"), {})
        surface_rows.append((sample.get("location") or sample.get("name") or "", "Swab", row.get("result", "")))
    if surface_rows:
        top = _heading(c, "Surface Sample Results (Swab)", top + 25)
        top = _draw_table(c, top, [144, 144, 150], ["Location", "Sample Type", "Result"], surface_rows)

    top = _heading(c, "Mold Types Identified", top + 25)
    for mold_type in job.get("mold_types", []):
        description, dangerous = MOLD_DESCRIPTIONS.get(mold_type, ("", False))
        if not description:
            continue
        if top > PAGE_H - BOTTOM - 3 * LEADING:
            break
        top = _rich(
            c,
            [(f"{mold_type}:", BOLD), (description, BODY)],
            top,
            colors_by_font={BOLD: RED} if dangerous else None,
        ) + PARAGRAPH_GAP
    _footer(c, page_no)


def _draw_conclusions(c: canvas.Canvas, job: dict, page_no: int):
    _tdlr_mark(c)
    top = _heading(c, "Conclusions", 72)
    top = _rich(c, "Based on the visual inspection, moisture readings, and laboratory results, the following conclusions are made:", top) + PARAGRAPH_GAP
    for idx, area in enumerate(job.get("areas", []), 1):
        top = _rich(c, [(f"{idx}. {area.get('name', '')}:", BOLD), (f"{area.get('finding', '')}.", BODY)], top) + PARAGRAPH_GAP

    top = _heading(c, "Recommendations", top + 24)
    outcome = job.get("report_outcome", "Pending consultant review")
    if outcome == "Mold remediation required":
        top = _rich(c, "To return the property to a normal fungal ecology (Condition 1), the following remediation steps are recommended:", top) + PARAGRAPH_GAP
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
    for title, body in recs:
        top = _bullet(c, [(f"{title}:", BOLD), (body, BODY)], top)

    top += 3 * LEADING - 7
    top = _rich(c, [("This report is generated in accordance with the Texas Mold Assessment and Remediation Rules (TMARR).", ITALIC)], top, size=9, leading=12)
    _rich(c, [("Limitations: This inspection is limited to the areas accessible at the time of inspection.", ITALIC)], top, size=9, leading=12)
    _footer(c, page_no)


def _draw_terms(c: canvas.Canvas, page_no: int):
    _tdlr_mark(c)
    top = _heading(c, "Terms and Conditions", 72)
    terms = [
        ("Inspection Limitation", "This inspection and the information set forth in the report is provided solely for the purpose of verifying that certain structural or physical characteristics exist at the Location Address listed. The undersigned and company representative does not make a health or safety certification or warranty, express or implied, of any kind."),
        ("Limitation of Liability", "The Client agrees that Inspector's liability for errors and/or omissions shall be limited to the maximum of a full refund of the fee paid for the inspection. The Client agrees to assume all risk of loss which exceeds the fee paid."),
        ("Sampling Limitations", "Mold spore sampling results represent conditions at the time and location of sampling only. Conditions can change rapidly due to environmental factors, occupant activities, and remediation efforts."),
        ("Health Disclaimer", "This report does not constitute medical advice. Individuals with health concerns related to potential mold exposure should consult with a qualified healthcare professional."),
        ("Report Usage", "This report is prepared exclusively for the named client and may not be reproduced or distributed to third parties without written consent from Mold Testing and Removal."),
    ]
    for title, body in terms:
        top = _rich(c, [(f"{title}:", BOLD), (body, BODY)], top, size=10, leading=13) + 10

    top += 26
    c.setFillColor(TEXT)
    c.setFont(BOLD_ITALIC, BODY_SIZE)
    c.drawCentredString(PAGE_W / 2, _y(top), "— Laboratory Report Attached —")
    top += 24
    c.setFont(BODY, BODY_SIZE)
    c.drawCentredString(PAGE_W / 2, _y(top), "PRO-LAB Certificate of Mold Analysis follows this page")
    top = _heading(c, "Mold Testing and Removal", top + 48, 20, center=True)
    c.setFillColor(TEXT)
    c.setFont(BODY, BODY_SIZE)
    c.drawCentredString(PAGE_W / 2, _y(top + 2), "2031 John West Rd. #119 | Dallas, TX 75228")
    c.drawCentredString(PAGE_W / 2, _y(top + 2 + LEADING), "(817) 718-5086 | help@moldtestingandremoval.com")
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
