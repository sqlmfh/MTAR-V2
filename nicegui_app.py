from __future__ import annotations

from datetime import date
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from nicegui import app, run, ui

from coc_builder import build_coc_payload, fill_coc_pdf, validate_coc_payload
from document_store import FileDocumentStore
from gmail_intake import GmailApiClient, confident_job_match, inspect_attachment
from job_store import SQLiteJobStore, new_persistent_job
from models import new_area, new_sample
from prolab_parser import apply_prolab_results, build_automated_job_from_prolab, parse_prolab_pdf, suggested_mapping
from photo_service import normalize_report_photo, photo_data_url
from pdf_report_builder import create_customer_pdf
from report_builder import MOLD_DESCRIPTIONS, create_report
from workflow import (
    ALLOWED_TRANSITIONS,
    final_report_issues,
    transition_job,
)


RAILWAY_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DEFAULT_DATA_ROOT = Path(RAILWAY_VOLUME) if RAILWAY_VOLUME else Path(".")
DB_PATH = Path(os.environ.get("MTAR_DB_PATH", str(DEFAULT_DATA_ROOT / "mtar_jobs.sqlite3")))
DOCUMENT_ROOT = Path(os.environ.get("MTAR_DOCUMENT_ROOT", str(DEFAULT_DATA_ROOT / "mtar_data")))
COC_TEMPLATE = Path(os.environ.get("MTAR_COC_TEMPLATE", "assets/BLANK_COC.pdf"))
UPLOADED_COC_TEMPLATE = DOCUMENT_ROOT / "templates" / "BLANK_COC.pdf"

store = SQLiteJobStore(DB_PATH)
documents = FileDocumentStore(DOCUMENT_ROOT)
gmail_client = GmailApiClient()
GMAIL_POLL_SECONDS = max(60, int(os.environ.get("GMAIL_POLL_SECONDS", "300")))
LAST_GMAIL_CHECK = {
    "status": "not_run",
    "checked": 0,
    "imported": 0,
    "created": 0,
    "ambiguous": 0,
    "ignored": 0,
}

STATUS_LABELS = {
    "draft": "Draft",
    "inspection": "Inspection",
    "awaiting_lab": "Awaiting Lab",
    "lab_received": "Lab Received",
    "report_review": "Report Review",
    "ready_to_send": "Ready to Send",
    "sent": "Sent",
    "closed": "Closed",
}

FINDING_OPTIONS = [
    "Needs consultant review",
    "Active mold growth confirmed",
    "Elevated spore counts",
    "Mold levels not elevated",
    "Visual mold present",
    "No mold detected",
]

REPORT_OUTCOMES = [
    "Pending consultant review",
    "Mold remediation required",
    "No significant mold contamination identified",
]


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").title())


def _date_text(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _parse_date(value: str, fallback: date) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return fallback


def _safe_filename(value: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in (value or "")).strip("._")
    return cleaned or fallback


def sample_name(sample: dict) -> str:
    if sample.get("outdoor_control"):
        return sample.get("name") or "Outdoor Control"
    return sample.get("name") or sample.get("location") or "Unnamed Sample"


def coc_template_path() -> Path | None:
    if COC_TEMPLATE.exists():
        return COC_TEMPLATE
    if UPLOADED_COC_TEMPLATE.exists():
        return UPLOADED_COC_TEMPLATE
    return None


def create_field_job() -> dict:
    """Create a clean inspection-first job for the NiceGUI workflow."""
    job = new_persistent_job()
    job["areas"] = []
    outdoor = new_sample(
        sample_type="Air Sample",
        location="Outdoor Control",
        outdoor_control=True,
    )
    outdoor.update(
        {
            "name": "Outdoor Control",
            "serial_number": "",
            "sample_type_code": "P15",
            "flow_rate_liters": 15,
            "flow_rate_minutes": 5,
            "mold_analysis": True,
        }
    )
    job["samples"] = [outdoor]
    job["air_lab_rows"] = []
    job["surface_lab_rows"] = []
    job["mold_types"] = []
    return job


def save_job(job: dict, *, notify: bool = True) -> dict:
    saved = store.save(job)
    job.clear()
    job.update(saved)
    if notify:
        ui.notify("Assessment saved", type="positive")
    return job


def run_gmail_intake() -> dict:
    """Check Gmail once and turn valid PRO-LAB PDFs into MTAR assessments.

    High-confidence matches attach to an existing Awaiting Lab assessment.
    Otherwise, MTAR creates a new assessment directly from the lab report so
    Gmail can be the trigger for the workflow rather than requiring a record to
    exist first.
    """
    if not gmail_client.configured():
        return {
            "configured": False,
            "checked": 0,
            "imported": 0,
            "created": 0,
            "ambiguous": [],
            "ignored": 0,
            "skipped": 0,
        }

    all_summaries = store.list(limit=500)
    all_jobs = [job for summary in all_summaries if (job := store.get(summary.id))]
    processed_ids = {
        message_id
        for job in all_jobs
        for message_id in job.get("gmail_message_ids", [])
    }
    awaiting_jobs = [job for job in all_jobs if job.get("status") == "awaiting_lab"]

    result = {
        "configured": True,
        "checked": 0,
        "imported": 0,
        "created": 0,
        "ambiguous": [],
        "ignored": 0,
        "skipped": 0,
    }

    attachments = gmail_client.search_pdf_attachments()
    result["checked"] = len(attachments)

    for attachment in attachments:
        if attachment.message_id in processed_ids:
            result["skipped"] += 1
            continue

        inspected = inspect_attachment(attachment)
        parsed = inspected["parsed"]
        if not parsed.get("samples"):
            result["ignored"] += 1
            continue

        match = confident_job_match(parsed, awaiting_jobs)
        target = None
        created_from_gmail = False
        match_score = 0
        match_reasons = ["created from PRO-LAB report"]

        if match:
            target = next((job for job in awaiting_jobs if job.get("id") == match["job_id"]), None)
            if target:
                match_score = match["score"]
                match_reasons = match["reasons"]

        if target is None:
            target = new_persistent_job()
            mapping = build_automated_job_from_prolab(
                target,
                parsed,
                MOLD_DESCRIPTIONS.keys(),
            )
            target["lab_mapping"] = mapping
            target["status"] = "report_review"
            created_from_gmail = True
            result["created"] += 1
        else:
            mapping = suggested_mapping(parsed, target)
            target["lab_mapping"] = mapping
            parsed_keys = {sample.get("key") for sample in parsed.get("samples", [])}
            mapped_keys = {key for key, value in mapping.items() if value}
            if parsed_keys and parsed_keys == mapped_keys and len(set(mapping.values())) == len(mapping):
                apply_prolab_results(target, parsed, mapping, MOLD_DESCRIPTIONS.keys())
                target["status"] = "report_review"
            else:
                try:
                    target = transition_job(target, "lab_received")
                except ValueError:
                    target["status"] = "lab_received"

        # Create/reuse persistent customer and property profiles from the
        # PRO-LAB project and test-location metadata, then link the assessment.
        try:
            customer = store.ensure_customer(target.get("client_name", ""))
            property_record = store.ensure_property(
                customer["id"],
                target.get("address", ""),
                target.get("city", ""),
                target.get("state", ""),
                target.get("zip", ""),
            )
            target["customer_id"] = customer["id"]
            target["property_id"] = property_record["id"]
        except ValueError:
            # A valid report can still be retained for review if a future
            # laboratory format omits a customer or address.
            pass

        filename = _safe_filename(attachment.filename, "PROLAB_Result.pdf")
        documents.save_bytes(target["id"], "lab", filename, attachment.content)
        target["lab_filename"] = filename
        target["lab_parsed"] = parsed
        target.setdefault("gmail_message_ids", []).append(attachment.message_id)
        target["gmail_source"] = {
            "message_id": attachment.message_id,
            "thread_id": attachment.thread_id,
            "sender": attachment.sender,
            "subject": attachment.subject,
            "filename": filename,
            "match_score": match_score,
            "match_reasons": match_reasons,
            "created_assessment": created_from_gmail,
        }

        # Generate review documents automatically. Lab-only drafts contain
        # explicit review-required placeholders for inspection facts that are
        # not available in the PRO-LAB PDF.
        try:
            report = create_report(target, {}, attachment.content)
            draft_filename = (
                f"{_safe_filename(target.get('client_name', ''), 'Client')}"
                "_Mold_Assessment_AUTO_DRAFT.docx"
            )
            documents.save_bytes(target["id"], "reports", draft_filename, report.getvalue())
            target["latest_draft_filename"] = draft_filename

            pdf = create_customer_pdf(target, {})
            pdf_filename = (
                f"{_safe_filename(target.get('client_name', ''), 'Client')}"
                "_Mold_Assessment_AUTO_DRAFT.pdf"
            )
            documents.save_bytes(target["id"], "reports", pdf_filename, pdf.getvalue())
            target["latest_pdf_draft_filename"] = pdf_filename
        except Exception as exc:
            target["automatic_draft_error"] = str(exc)

        store.save(target)
        processed_ids.add(attachment.message_id)
        awaiting_jobs = [job for job in awaiting_jobs if job.get("id") != target.get("id")]
        result["imported"] += 1

    return result


async def gmail_monitor():
    """Run Gmail intake continuously while the NiceGUI server is online."""
    global LAST_GMAIL_CHECK
    while True:
        if gmail_client.configured():
            try:
                result = await run.io_bound(run_gmail_intake)
                LAST_GMAIL_CHECK = {
                    "status": "ok",
                    "checked": result.get("checked", 0),
                    "imported": result.get("imported", 0),
                    "created": result.get("created", 0),
                    "ambiguous": len(result.get("ambiguous", [])),
                    "ignored": result.get("ignored", 0),
                }
            except Exception as exc:
                LAST_GMAIL_CHECK = {
                    "status": "error",
                    "error": str(exc),
                    "checked": 0,
                    "imported": 0,
                    "created": 0,
                    "ambiguous": 0,
                    "ignored": 0,
                }
        await asyncio.sleep(GMAIL_POLL_SECONDS)


app.on_startup(gmail_monitor)


def page_shell(title: str, subtitle: str | None = None):
    with ui.header().classes("bg-slate-900 text-white items-center px-6 h-16"):
        ui.label("MTAR").classes("text-xl font-bold tracking-wide")
        ui.space()
        ui.button("Dashboard", icon="dashboard", on_click=lambda: ui.navigate.to("/")).props("flat color=white")
    with ui.column().classes("w-full max-w-7xl mx-auto px-5 py-6 gap-4"):
        ui.label(title).classes("text-3xl font-bold text-slate-800")
        if subtitle:
            ui.label(subtitle).classes("text-slate-500")


def empty_state(title: str, body: str, action_text: str | None = None, action=None):
    with ui.card().classes("w-full p-8 border border-dashed border-slate-300 shadow-none items-center"):
        ui.icon("inbox").classes("text-5xl text-slate-300")
        ui.label(title).classes("text-lg font-semibold text-slate-700")
        ui.label(body).classes("text-slate-500 text-center")
        if action_text and action:
            ui.button(action_text, on_click=action).props("unelevated color=primary")


@ui.page("/")
def dashboard_page():
    page_shell(
        "MTAR Dashboard",
        "Automated assessment workflow from inspection and PRO-LAB results to customer report.",
    )

    jobs = store.list(limit=200)

    def create_job():
        job = create_field_job()
        save_job(job, notify=False)
        ui.navigate.to(f"/jobs/{job['id']}")

    @ui.refreshable
    def gmail_panel():
        with ui.card().classes("w-full p-4 mb-3 shadow-sm border border-slate-200"):
            with ui.row().classes("w-full items-center gap-3"):
                with ui.column().classes("gap-0 grow"):
                    ui.label("PRO-LAB Gmail Intake").classes("font-semibold text-slate-800")
                    if gmail_client.configured():
                        ui.label(
                            "Connected by server-side Gmail API credentials. High-confidence reports can be matched to Awaiting Lab jobs automatically."
                        ).classes("text-sm text-slate-500")
                    else:
                        ui.label(
                            "Gmail API credentials are not configured on this deployment yet."
                        ).classes("text-sm text-amber-700")
                    if gmail_client.configured():
                        if LAST_GMAIL_CHECK.get("status") == "ok":
                            ui.label(
                                f"Last check: {LAST_GMAIL_CHECK.get('checked', 0)} PDFs · "
                                f"{LAST_GMAIL_CHECK.get('imported', 0)} imported · "
                                f"{LAST_GMAIL_CHECK.get('created', 0)} created · "
                                f"{LAST_GMAIL_CHECK.get('ambiguous', 0)} need review"
                            ).classes("text-xs text-slate-400")
                        elif LAST_GMAIL_CHECK.get("status") == "error":
                            ui.label("Last automatic check failed.").classes("text-xs text-red-500")
                        ui.label(
                            f"Automatic polling every {GMAIL_POLL_SECONDS // 60} minute(s)."
                        ).classes("text-xs text-slate-400")

                async def check_now():
                    global LAST_GMAIL_CHECK
                    try:
                        result = await run.io_bound(run_gmail_intake)
                    except Exception as exc:
                        ui.notify(f"Gmail check failed: {exc}", type="negative", multi_line=True)
                        return
                    if not result["configured"]:
                        ui.notify(
                            "Configure GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, and GMAIL_REFRESH_TOKEN on the NiceGUI host.",
                            type="warning",
                            multi_line=True,
                        )
                        return
                    LAST_GMAIL_CHECK = {
                        "status": "ok",
                        "checked": result.get("checked", 0),
                        "imported": result.get("imported", 0),
                        "created": result.get("created", 0),
                        "ambiguous": len(result.get("ambiguous", [])),
                        "ignored": result.get("ignored", 0),
                    }
                    gmail_panel.refresh()
                    summary = (
                        f"Checked {result['checked']} PDF attachment(s); "
                        f"imported {result['imported']}; "
                        f"created {result.get('created', 0)} assessment(s); "
                        f"needs review {len(result['ambiguous'])}; "
                        f"ignored {result['ignored']}."
                    )
                    ui.notify(summary, type="positive" if not result["ambiguous"] else "warning", multi_line=True)
                    if result["ambiguous"]:
                        for item in result["ambiguous"][:5]:
                            ui.notify(
                                f"Needs review: {item['project_name'] or item['subject']} · report {item['report_number'] or 'unknown'}",
                                type="warning",
                            )
                    ui.navigate.to("/")

                ui.button("Check Gmail Now", icon="mail", on_click=check_now).props(
                    "unelevated color=primary" if gmail_client.configured() else "outline color=primary"
                )

    gmail_panel()

    with ui.row().classes("w-full items-center justify-between mb-2"):
        with ui.row().classes("gap-3"):
            active = [j for j in jobs if j.status != "closed"]
            ui.label(f"{len(active)} Active").classes("px-3 py-1 rounded-full bg-blue-50 text-blue-700 font-medium")
            ui.label(f"{len(jobs)} Total").classes("px-3 py-1 rounded-full bg-slate-100 text-slate-700 font-medium")
        ui.button("New Assessment", icon="add", on_click=create_job).props("unelevated color=primary")

    if not jobs:
        empty_state(
            "No assessments yet",
            "Create the first assessment manually, or let a PRO-LAB Gmail result create one automatically.",
            "Create First Assessment",
            create_job,
        )
        return

    with ui.grid(columns=1).classes("w-full gap-3"):
        for summary in jobs:
            with ui.card().classes("w-full p-4 shadow-sm border border-slate-200"):
                with ui.row().classes("w-full items-center gap-4"):
                    with ui.column().classes("gap-0 grow"):
                        name = summary.client_name or "New Assessment"
                        ui.label(name).classes("text-lg font-semibold text-slate-800")
                        ui.label(summary.property_address or "Property address not entered").classes("text-sm text-slate-500")
                    ui.label(status_label(summary.status)).classes(
                        "px-3 py-1 rounded-full bg-slate-100 text-slate-700 text-sm font-medium"
                    )
                    ui.button(
                        "Open",
                        icon="arrow_forward",
                        on_click=lambda job_id=summary.id: ui.navigate.to(f"/jobs/{job_id}"),
                    ).props("flat color=primary")


@ui.page("/jobs/{job_id}")
def job_page(job_id: str):
    job = store.get(job_id)
    if not job:
        page_shell("Assessment not found")
        ui.label("This assessment no longer exists or the link is invalid.").classes("text-red-600")
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        return

    page_shell(
        job.get("client_name") or "New Assessment",
        f"Assessment {job.get('id')} · {job.get('address') or 'Property address pending'}",
    )

    def persist():
        save_job(job)

    def advance_status_if(current_status: str, target_status: str):
        if job.get("status") != current_status:
            return
        try:
            updated = transition_job(job, target_status)
        except ValueError:
            return
        job.clear()
        job.update(store.save(updated))

    def change_status(new_status: str):
        if not new_status or new_status == job.get("status"):
            return
        try:
            updated = transition_job(job, new_status)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            status_section.refresh()
            return
        job.clear()
        job.update(store.save(updated))
        ui.notify(f"Status changed to {status_label(new_status)}", type="positive")
        status_section.refresh()

    def photo_records() -> list[dict]:
        return job.setdefault("photos", [])

    def report_photos() -> dict:
        result: dict = {}
        for photo in photo_records():
            content = documents.read_bytes(job["id"], "photos", photo.get("filename", ""))
            if not content:
                continue
            entry = {
                "content": __import__("io").BytesIO(content),
                "caption": photo.get("caption", ""),
                "kind": photo.get("kind", "inspection"),
            }
            if photo.get("role") == "property":
                result.setdefault("property", []).append(entry)
            elif photo.get("role") == "outdoor":
                result.setdefault("outdoor", []).append(entry)
            elif photo.get("role") == "environment":
                result.setdefault("environment", []).append(entry)
            elif photo.get("role") == "area" and photo.get("area_id"):
                result.setdefault(photo["area_id"], []).append(entry)
        return result

    @ui.refreshable
    def status_section():
        current = job.get("status", "draft")
        allowed = [current] + sorted(ALLOWED_TRANSITIONS.get(current, set()))
        with ui.row().classes("w-full items-center gap-4"):
            ui.label("Workflow Status").classes("font-semibold text-slate-700")
            ui.select(
                {status: status_label(status) for status in allowed},
                value=current,
                on_change=lambda e: change_status(e.value),
            ).props("outlined dense").classes("w-56")
            ui.space()
            ui.button("Save Job", icon="save", on_click=persist).props("unelevated color=primary")

    status_section()

    with ui.tabs().classes("w-full") as tabs:
        overview_tab = ui.tab("Overview", icon="home")
        areas_tab = ui.tab("Inspection Areas", icon="meeting_room")
        samples_tab = ui.tab("Samples", icon="science")
        photos_tab = ui.tab("Photos", icon="photo_camera")
        lab_tab = ui.tab("Lab Results", icon="biotech")
        report_tab = ui.tab("Report", icon="description")
        docs_tab = ui.tab("Documents", icon="folder")

    with ui.tab_panels(tabs, value=overview_tab).classes("w-full bg-transparent"):
        with ui.tab_panel(overview_tab).classes("px-0"):
            with ui.card().classes("w-full p-5 shadow-sm border border-slate-200"):
                ui.label("Client & Property").classes("text-xl font-semibold text-slate-800 mb-2")
                with ui.grid(columns=2).classes("w-full gap-4"):
                    ui.input(
                        "Client Name",
                        value=job.get("client_name", ""),
                        on_change=lambda e: job.__setitem__("client_name", e.value),
                    ).props("outlined").classes("w-full")
                    ui.input(
                        "Property Address",
                        value=job.get("address", ""),
                        on_change=lambda e: job.__setitem__("address", e.value),
                    ).props("outlined").classes("w-full")
                    ui.input(
                        "City",
                        value=job.get("city", ""),
                        on_change=lambda e: job.__setitem__("city", e.value),
                    ).props("outlined").classes("w-full")
                    with ui.row().classes("w-full gap-3"):
                        ui.input(
                            "State",
                            value=job.get("state", "TX"),
                            on_change=lambda e: job.__setitem__("state", e.value),
                        ).props("outlined maxlength=2").classes("w-28")
                        ui.input(
                            "ZIP",
                            value=job.get("zip", ""),
                            on_change=lambda e: job.__setitem__("zip", e.value),
                        ).props("outlined").classes("grow")

                ui.separator().classes("my-4")
                ui.label("Inspection Conditions").classes("text-xl font-semibold text-slate-800 mb-2")
                with ui.grid(columns=4).classes("w-full gap-4"):
                    assessment_input = ui.input(
                        "Assessment Date",
                        value=_date_text(job.get("inspection_date")),
                    ).props("outlined type=date").classes("w-full")
                    assessment_input.on(
                        "change",
                        lambda e: job.__setitem__(
                            "inspection_date",
                            _parse_date(e.args, job.get("inspection_date") or date.today()),
                        ),
                    )
                    report_input = ui.input(
                        "Report Date",
                        value=_date_text(job.get("report_date")),
                    ).props("outlined type=date").classes("w-full")
                    report_input.on(
                        "change",
                        lambda e: job.__setitem__(
                            "report_date",
                            _parse_date(e.args, job.get("report_date") or date.today()),
                        ),
                    )
                    ui.number(
                        "Indoor RH (%)",
                        value=job.get("humidity"),
                        min=0,
                        max=100,
                        on_change=lambda e: job.__setitem__("humidity", e.value),
                    ).props("outlined").classes("w-full")
                    ui.number(
                        "Temperature (°F)",
                        value=job.get("temperature"),
                        on_change=lambda e: job.__setitem__("temperature", e.value),
                    ).props("outlined").classes("w-full")

                ui.textarea(
                    "General Observations",
                    value=job.get("general_observations", ""),
                    on_change=lambda e: job.__setitem__("general_observations", e.value),
                ).props("outlined autogrow").classes("w-full mt-4")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Save Overview", icon="save", on_click=persist).props("unelevated color=primary")

        with ui.tab_panel(areas_tab).classes("px-0"):
            def add_area():
                area = new_area(f"Inspection Area {len(job.get('areas', [])) + 1}")
                job.setdefault("areas", []).append(area)
                save_job(job, notify=False)
                areas_section.refresh()
                samples_section.refresh()
                report_section.refresh()
                ui.notify("Inspection area added", type="positive")

            def remove_area(area_id: str):
                job["areas"] = [area for area in job.get("areas", []) if area.get("id") != area_id]
                for sample in job.get("samples", []):
                    if sample.get("area_id") == area_id:
                        sample["area_id"] = None
                retained_photos = []
                for photo in job.get("photos", []):
                    if photo.get("area_id") == area_id:
                        documents.delete(job["id"], "photos", photo.get("filename", ""))
                    else:
                        retained_photos.append(photo)
                job["photos"] = retained_photos
                save_job(job, notify=False)
                areas_section.refresh()
                samples_section.refresh()
                photos_section.refresh()
                report_section.refresh()
                documents_section.refresh()
                ui.notify("Inspection area removed; linked samples are now unassigned and its photos were removed", type="warning")

            @ui.refreshable
            def areas_section():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Inspection Areas").classes("text-xl font-semibold text-slate-800")
                    ui.button("Add Area", icon="add", on_click=add_area).props("unelevated color=primary")

                areas = job.get("areas", [])
                if not areas:
                    empty_state(
                        "No inspection areas",
                        "Add the actual rooms or areas inspected. Samples are separate records and can be assigned to these areas.",
                        "Add Inspection Area",
                        add_area,
                    )
                    return

                for index, area in enumerate(areas, 1):
                    with ui.card().classes("w-full p-5 mt-3 shadow-sm border border-slate-200"):
                        with ui.row().classes("w-full items-center"):
                            ui.label(f"Area {index}").classes("text-sm font-medium text-slate-500")
                            ui.space()
                            ui.button(
                                icon="delete",
                                on_click=lambda area_id=area["id"]: remove_area(area_id),
                            ).props("flat round color=negative")
                        with ui.grid(columns=2).classes("w-full gap-4"):
                            ui.input(
                                "Area Name",
                                value=area.get("name", ""),
                                on_change=lambda e, target=area: target.__setitem__("name", e.value),
                            ).props("outlined").classes("w-full")
                            ui.select(
                                FINDING_OPTIONS,
                                value=area.get("finding", "Needs consultant review"),
                                label="Consultant Finding",
                                on_change=lambda e, target=area: target.__setitem__("finding", e.value),
                            ).props("outlined").classes("w-full")
                        ui.textarea(
                            "Visual Observations",
                            value=area.get("description", ""),
                            on_change=lambda e, target=area: target.__setitem__("description", e.value),
                        ).props("outlined autogrow").classes("w-full mt-3")
                        ui.textarea(
                            "Moisture Assessment",
                            value=area.get("moisture_notes", ""),
                            on_change=lambda e, target=area: target.__setitem__("moisture_notes", e.value),
                        ).props("outlined autogrow").classes("w-full mt-3")
                        ui.textarea(
                            "Thermal Imaging Notes",
                            value=area.get("thermal_notes", ""),
                            on_change=lambda e, target=area: target.__setitem__("thermal_notes", e.value),
                        ).props("outlined autogrow").classes("w-full mt-3")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("Save Areas", icon="save", on_click=persist).props("unelevated color=primary")

            areas_section()

        with ui.tab_panel(samples_tab).classes("px-0"):
            def add_sample(sample_type: str):
                area_id = job.get("areas", [{}])[0].get("id") if job.get("areas") else None
                sample = new_sample(sample_type=sample_type, area_id=area_id)
                if sample_type == "Air Sample":
                    sample.update(
                        {
                            "sample_type_code": "P15",
                            "flow_rate_liters": 15,
                            "flow_rate_minutes": 5,
                        }
                    )
                else:
                    sample["sample_type_code"] = "SW"
                sample.update({"serial_number": "", "mold_analysis": True})
                job.setdefault("samples", []).append(sample)
                save_job(job, notify=False)
                samples_section.refresh()
                lab_section.refresh()
                report_section.refresh()
                ui.notify(f"{sample_type} added", type="positive")

            def remove_sample(sample_id: str):
                if sample_id == "sample_outdoor_control":
                    ui.notify("The outdoor control sample is kept as a protected baseline.", type="warning")
                    return
                job["samples"] = [sample for sample in job.get("samples", []) if sample.get("id") != sample_id]
                job["air_lab_rows"] = [
                    row for row in job.get("air_lab_rows", []) if row.get("sample_id") != sample_id
                ]
                job["surface_lab_rows"] = [
                    row for row in job.get("surface_lab_rows", []) if row.get("sample_id") != sample_id
                ]
                save_job(job, notify=False)
                samples_section.refresh()
                lab_section.refresh()
                report_section.refresh()
                ui.notify("Sample removed", type="positive")

            async def upload_coc_template(e):
                try:
                    template_bytes = await e.file.read()
                    UPLOADED_COC_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
                    UPLOADED_COC_TEMPLATE.write_bytes(template_bytes)
                except Exception as exc:
                    ui.notify(f"Could not save COC template: {exc}", type="negative", multi_line=True)
                    return
                samples_section.refresh()
                ui.notify("PRO-LAB COC template saved", type="positive")

            def generate_coc():
                payload = build_coc_payload(job)
                issues = validate_coc_payload(payload)
                if issues:
                    ui.notify("COC needs: " + "; ".join(issues), type="negative", multi_line=True)
                    return
                template_path = coc_template_path()
                if not template_path:
                    ui.notify(
                        "Upload the blank PRO-LAB COC template before generating a COC.",
                        type="negative",
                    )
                    return
                try:
                    output = fill_coc_pdf(template_path.read_bytes(), payload)
                except Exception as exc:
                    ui.notify(f"Could not generate COC: {exc}", type="negative", multi_line=True)
                    return
                filename = f"{_safe_filename(job.get('client_name', ''), 'Client')}_PROLAB_COC.pdf"
                documents.save_bytes(job["id"], "coc", filename, output)
                job["latest_coc_filename"] = filename
                save_job(job, notify=False)
                documents_section.refresh()
                ui.download(output, filename=filename, media_type="application/pdf")
                ui.notify("PRO-LAB COC generated", type="positive")

            @ui.refreshable
            def samples_section():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Samples").classes("text-xl font-semibold text-slate-800")
                    with ui.row().classes("gap-2"):
                        ui.button("Generate COC", icon="description", on_click=generate_coc).props("outline color=primary")
                        ui.button(
                            "Add Air Sample",
                            icon="air",
                            on_click=lambda: add_sample("Air Sample"),
                        ).props("outline color=primary")
                        ui.button(
                            "Add Surface Sample",
                            icon="science",
                            on_click=lambda: add_sample("Surface Sample"),
                        ).props("unelevated color=primary")

                if not coc_template_path():
                    with ui.card().classes("w-full p-4 mt-3 border border-amber-200 bg-amber-50 shadow-none"):
                        ui.label("PRO-LAB COC template required").classes("font-semibold text-amber-800")
                        ui.label(
                            "Upload BLANK_COC.pdf once on this deployment. MTAR will reuse it for every job."
                        ).classes("text-sm text-amber-700")
                        ui.upload(
                            label="Upload blank PRO-LAB COC",
                            on_upload=upload_coc_template,
                            auto_upload=True,
                            max_files=1,
                        ).props("accept=.pdf").classes("w-full mt-2")

                area_options = {None: "Unassigned"}
                area_options.update(
                    {
                        area["id"]: area.get("name") or f"Inspection Area {index}"
                        for index, area in enumerate(job.get("areas", []), 1)
                    }
                )

                for index, sample in enumerate(job.get("samples", []), 1):
                    protected = sample.get("outdoor_control", False)
                    with ui.card().classes("w-full p-5 mt-3 shadow-sm border border-slate-200"):
                        with ui.row().classes("w-full items-center"):
                            ui.label("Outdoor Control" if protected else f"Sample {index}").classes(
                                "text-sm font-medium text-slate-500"
                            )
                            ui.label(sample.get("type", "")).classes(
                                "px-2 py-1 rounded bg-slate-100 text-slate-600 text-xs"
                            )
                            ui.space()
                            if sample.get("lab_determination"):
                                ui.label(sample["lab_determination"]).classes(
                                    "px-2 py-1 rounded bg-blue-50 text-blue-700 text-xs font-medium"
                                )
                            if not protected:
                                ui.button(
                                    icon="delete",
                                    on_click=lambda sample_id=sample["id"]: remove_sample(sample_id),
                                ).props("flat round color=negative")

                        with ui.grid(columns=4).classes("w-full gap-3"):
                            ui.input(
                                "Sample Name / Location",
                                value=sample.get("name", ""),
                                on_change=lambda e, target=sample: target.__setitem__("name", e.value),
                            ).props("outlined").classes("w-full")
                            ui.input(
                                "Serial Number",
                                value=sample.get("serial_number") or sample.get("lab_serial_number", ""),
                                on_change=lambda e, target=sample: target.__setitem__("serial_number", e.value),
                            ).props("outlined").classes("w-full")
                            ui.input(
                                "Sample Type Code",
                                value=sample.get("sample_type_code", ""),
                                on_change=lambda e, target=sample: target.__setitem__("sample_type_code", e.value),
                            ).props("outlined").classes("w-full")
                            if protected:
                                ui.input("Assigned Area", value="Outdoor Control").props("outlined disable").classes("w-full")
                            else:
                                ui.select(
                                    area_options,
                                    value=sample.get("area_id"),
                                    label="Assigned Area",
                                    on_change=lambda e, target=sample: target.__setitem__("area_id", e.value),
                                ).props("outlined").classes("w-full")

                        if sample.get("type") == "Air Sample":
                            with ui.grid(columns=2).classes("w-full gap-3 mt-3 max-w-lg"):
                                ui.number(
                                    "Flow Rate (Liters)",
                                    value=sample.get("flow_rate_liters", 15),
                                    min=0,
                                    on_change=lambda e, target=sample: target.__setitem__("flow_rate_liters", e.value),
                                ).props("outlined").classes("w-full")
                                ui.number(
                                    "Sampling Minutes",
                                    value=sample.get("flow_rate_minutes", 10),
                                    min=0,
                                    on_change=lambda e, target=sample: target.__setitem__("flow_rate_minutes", e.value),
                                ).props("outlined").classes("w-full")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("Save Samples", icon="save", on_click=persist).props("unelevated color=primary")

            samples_section()

        with ui.tab_panel(photos_tab).classes("px-0"):
            async def handle_photo_upload(
                e,
                role: str,
                area_id: str | None = None,
                kind: str = "inspection",
            ):
                try:
                    raw = await e.file.read()
                    normalized = normalize_report_photo(raw)
                except Exception as exc:
                    ui.notify(f"Could not process photo: {exc}", type="negative", multi_line=True)
                    return

                if role == "property":
                    retained = []
                    for existing in photo_records():
                        if existing.get("role") == "property":
                            documents.delete(job["id"], "photos", existing.get("filename", ""))
                        else:
                            retained.append(existing)
                    job["photos"] = retained

                token = uuid4().hex[:8]
                sequence = len(photo_records()) + 1
                source_name = _safe_filename(e.file.name, f"photo_{sequence}.jpg")
                stem = Path(source_name).stem
                prefix = role if role in {"property", "outdoor", "environment"} else f"area_{area_id}"
                filename = f"{prefix}_{token}_{stem}.jpg"
                documents.save_bytes(job["id"], "photos", filename, normalized)

                photo_records().append(
                    {
                        "id": f"photo_{token}",
                        "filename": filename,
                        "role": role,
                        "area_id": area_id,
                        "caption": "",
                        "kind": kind,
                    }
                )
                save_job(job, notify=False)
                photos_section.refresh()
                documents_section.refresh()
                ui.notify("Photo uploaded", type="positive")

            def delete_photo(photo_id: str):
                target = next((p for p in photo_records() if p.get("id") == photo_id), None)
                if not target:
                    return
                documents.delete(job["id"], "photos", target.get("filename", ""))
                job["photos"] = [p for p in photo_records() if p.get("id") != photo_id]
                save_job(job, notify=False)
                photos_section.refresh()
                documents_section.refresh()
                ui.notify("Photo removed", type="positive")

            @ui.refreshable
            def photos_section():
                ui.label("Inspection Photos").classes("text-xl font-semibold text-slate-800")
                ui.label(
                    "The first property photo is used on the report cover. Area photos are inserted under their inspection area in upload order."
                ).classes("text-sm text-slate-500")

                property_photos = [p for p in photo_records() if p.get("role") == "property"]
                with ui.card().classes("w-full p-5 mt-4 shadow-sm border border-slate-200"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label("Property Exterior").classes("text-lg font-semibold text-slate-800")
                            ui.label("Upload the exterior/property photo used on the report cover. Uploading a new one replaces it.").classes("text-sm text-slate-500")
                        ui.upload(
                            label="Add Property Photo",
                            on_upload=lambda e: handle_photo_upload(e, "property"),
                            auto_upload=True,
                            max_files=1,
                        ).props("accept=image/jpeg,image/png,image/webp").classes("w-72")

                    if property_photos:
                        with ui.grid(columns=3).classes("w-full gap-4 mt-4"):
                            for photo in property_photos:
                                content = documents.read_bytes(job["id"], "photos", photo.get("filename", ""))
                                if not content:
                                    continue
                                with ui.card().classes("w-full p-3 shadow-none border border-slate-200"):
                                    ui.image(photo_data_url(content)).classes("w-full h-44 object-cover rounded")
                                    ui.input(
                                        "Caption",
                                        value=photo.get("caption", ""),
                                        on_change=lambda e, target=photo: target.__setitem__("caption", e.value),
                                    ).props("outlined dense").classes("w-full mt-2")
                                    with ui.row().classes("w-full justify-end"):
                                        ui.button(
                                            icon="delete",
                                            on_click=lambda photo_id=photo["id"]: delete_photo(photo_id),
                                        ).props("flat round color=negative")

                outdoor_photos = [p for p in photo_records() if p.get("role") == "outdoor"]
                environment_photos = [p for p in photo_records() if p.get("role") == "environment"]

                with ui.grid(columns=2).classes("w-full gap-4 mt-4"):
                    with ui.card().classes("w-full p-5 shadow-sm border border-slate-200"):
                        ui.label("Outdoor Control Sampling").classes("text-lg font-semibold text-slate-800")
                        ui.label(
                            "Upload pump/cassette/control photos for the Outdoor Control Sample page."
                        ).classes("text-sm text-slate-500")
                        ui.upload(
                            label="Add Outdoor Control Photos",
                            on_upload=lambda e: handle_photo_upload(e, "outdoor", kind="sampling"),
                            auto_upload=True,
                            multiple=True,
                        ).props("accept=image/jpeg,image/png,image/webp").classes("w-full mt-3")
                        if outdoor_photos:
                            ui.label(f"{len(outdoor_photos)} photo(s) uploaded").classes("text-xs text-slate-500 mt-2")

                    with ui.card().classes("w-full p-5 shadow-sm border border-slate-200"):
                        ui.label("Environmental / RH Photo").classes("text-lg font-semibold text-slate-800")
                        ui.label(
                            "Upload the indoor RH / environmental-condition meter photo used on the Outdoor Control page."
                        ).classes("text-sm text-slate-500")
                        ui.upload(
                            label="Add Environmental Photo",
                            on_upload=lambda e: handle_photo_upload(e, "environment", kind="environment"),
                            auto_upload=True,
                            max_files=1,
                        ).props("accept=image/jpeg,image/png,image/webp").classes("w-full mt-3")
                        if environment_photos:
                            ui.label(f"{len(environment_photos)} photo(s) uploaded").classes("text-xs text-slate-500 mt-2")

                ui.label("Inspection Area Photos").classes("text-lg font-semibold text-slate-800 mt-6")
                areas = job.get("areas", [])
                if not areas:
                    ui.label("Create inspection areas before adding area photos.").classes("text-sm text-slate-500")
                for area in areas:
                    area_photos = [
                        p for p in photo_records()
                        if p.get("role") == "area" and p.get("area_id") == area.get("id")
                    ]
                    with ui.card().classes("w-full p-5 mt-3 shadow-sm border border-slate-200"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0"):
                                ui.label(area.get("name") or "Unnamed Inspection Area").classes("text-lg font-semibold")
                                ui.label(f"{len(area_photos)} photo(s)").classes("text-xs text-slate-500")
                            ui.upload(
                                label="Add Area Photos",
                                on_upload=lambda e, area_id=area["id"]: handle_photo_upload(e, "area", area_id),
                                auto_upload=True,
                                multiple=True,
                            ).props("accept=image/jpeg,image/png,image/webp").classes("w-72")

                        if area_photos:
                            with ui.grid(columns=3).classes("w-full gap-4 mt-4"):
                                for photo in area_photos:
                                    content = documents.read_bytes(job["id"], "photos", photo.get("filename", ""))
                                    if not content:
                                        continue
                                    with ui.card().classes("w-full p-3 shadow-none border border-slate-200"):
                                        ui.image(photo_data_url(content)).classes("w-full h-44 object-cover rounded")
                                        ui.input(
                                            "Caption",
                                            value=photo.get("caption", ""),
                                            on_change=lambda e, target=photo: target.__setitem__("caption", e.value),
                                        ).props("outlined dense").classes("w-full mt-2")
                                        if photo.get("role") == "area":
                                            ui.select(
                                                {
                                                    "sampling": "Sampling",
                                                    "inspection": "Inspection / Moisture",
                                                    "thermal": "Thermal Imaging",
                                                },
                                                value=photo.get("kind", "inspection"),
                                                label="Photo Type",
                                                on_change=lambda e, target=photo: target.__setitem__("kind", e.value),
                                            ).props("outlined dense").classes("w-full mt-2")
                                        with ui.row().classes("w-full justify-end"):
                                            ui.button(
                                                icon="delete",
                                                on_click=lambda photo_id=photo["id"]: delete_photo(photo_id),
                                            ).props("flat round color=negative")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button(
                        "Save Photo Details",
                        icon="save",
                        on_click=lambda: save_job(job),
                    ).props("unelevated color=primary")

            photos_section()

        with ui.tab_panel(lab_tab).classes("px-0"):
            async def handle_lab_upload(e):
                try:
                    pdf_bytes = await e.file.read()
                    parsed = parse_prolab_pdf(pdf_bytes)
                except Exception as exc:
                    ui.notify(f"Could not read lab PDF: {exc}", type="negative", multi_line=True)
                    return

                if not parsed.get("samples"):
                    warning = "; ".join(parsed.get("warnings", [])) or "No structured PRO-LAB result table was found."
                    ui.notify(warning, type="negative", multi_line=True)
                    return

                filename = _safe_filename(e.file.name, "PROLAB_Result.pdf")
                documents.save_bytes(job["id"], "lab", filename, pdf_bytes)
                job["lab_filename"] = filename
                job["lab_parsed"] = parsed
                job["lab_mapping"] = suggested_mapping(parsed, job)

                advance_status_if("awaiting_lab", "lab_received")
                save_job(job, notify=False)
                status_section.refresh()
                lab_section.refresh()
                documents_section.refresh()
                ui.notify(f"Imported {len(parsed['samples'])} PRO-LAB samples", type="positive")

            def set_mapping(lab_key: str, sample_id: str | None):
                mapping = job.setdefault("lab_mapping", {})
                if sample_id:
                    mapping[lab_key] = sample_id
                else:
                    mapping.pop(lab_key, None)

            def apply_lab():
                parsed = job.get("lab_parsed") or {}
                parsed_samples = parsed.get("samples", [])
                mapping = job.get("lab_mapping") or {}

                missing = [sample.get("key") for sample in parsed_samples if not mapping.get(sample.get("key"))]
                mapped_values = [mapping.get(sample.get("key")) for sample in parsed_samples if mapping.get(sample.get("key"))]
                if missing:
                    ui.notify("Map every lab sample before applying results.", type="negative")
                    return
                if len(mapped_values) != len(set(mapped_values)):
                    ui.notify("Each lab sample must map to a different MTAR sample.", type="negative")
                    return

                apply_prolab_results(job, parsed, mapping, MOLD_DESCRIPTIONS.keys())
                advance_status_if("lab_received", "report_review")
                save_job(job, notify=False)
                status_section.refresh()
                samples_section.refresh()
                lab_section.refresh()
                report_section.refresh()
                ui.notify("Lab results applied to this job", type="positive")

            @ui.refreshable
            def lab_section():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("PRO-LAB Results").classes("text-xl font-semibold text-slate-800")
                    if job.get("lab_filename"):
                        ui.label(job["lab_filename"]).classes("text-sm text-slate-500")

                ui.upload(
                    label="Upload PRO-LAB Certificate of Mold Analysis",
                    on_upload=handle_lab_upload,
                    auto_upload=True,
                    max_files=1,
                ).props("accept=.pdf").classes("w-full mt-3")

                parsed = job.get("lab_parsed") or {}
                parsed_samples = parsed.get("samples", [])
                if not parsed_samples:
                    empty_state(
                        "No lab report imported",
                        "Upload the PRO-LAB PDF after sampling. MTAR will parse the result table and suggest sample matches.",
                    )
                    return

                metadata = parsed.get("metadata", {})
                with ui.card().classes("w-full p-4 mt-4 shadow-sm border border-slate-200"):
                    with ui.grid(columns=3).classes("w-full gap-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("Report #").classes("text-xs text-slate-500")
                            ui.label(metadata.get("report_number") or "—").classes("font-semibold")
                        with ui.column().classes("gap-0"):
                            ui.label("Project").classes("text-xs text-slate-500")
                            ui.label(metadata.get("project_name") or "—").classes("font-semibold")
                        with ui.column().classes("gap-0"):
                            ui.label("Samples").classes("text-xs text-slate-500")
                            ui.label(str(len(parsed_samples))).classes("font-semibold")

                options = {
                    sample["id"]: f"{sample_name(sample)} · {sample.get('serial_number') or sample.get('lab_serial_number') or 'no serial'}"
                    for sample in job.get("samples", [])
                }
                mapping = job.setdefault("lab_mapping", {})

                ui.label("Review Sample Matching").classes("text-lg font-semibold text-slate-800 mt-5")
                ui.label(
                    "MTAR prefers exact serial-number matches. Confirm every mapping before applying laboratory results."
                ).classes("text-sm text-slate-500")

                for lab in parsed_samples:
                    with ui.card().classes("w-full p-4 mt-3 shadow-sm border border-slate-200"):
                        with ui.grid(columns=4).classes("w-full gap-3 items-center"):
                            with ui.column().classes("gap-0"):
                                ui.label(lab.get("location") or "Unnamed Lab Sample").classes("font-semibold")
                                ui.label(lab.get("coc_line") or "—").classes("text-xs text-slate-500")
                            with ui.column().classes("gap-0"):
                                ui.label("Lab Serial").classes("text-xs text-slate-500")
                                ui.label(lab.get("serial_number") or "—")
                            with ui.column().classes("gap-0"):
                                ui.label("Determination").classes("text-xs text-slate-500")
                                determination = lab.get("determination") or "—"
                                ui.label(determination).classes(
                                    "font-semibold text-red-700" if "UNUSUAL" in determination or "ELEVATED" in determination else "font-semibold"
                                )
                            ui.select(
                                options,
                                value=mapping.get(lab.get("key")),
                                label="MTAR Sample",
                                on_change=lambda e, key=lab.get("key"): set_mapping(key, e.value),
                            ).props("outlined clearable").classes("w-full")

                        fungi = lab.get("fungi", {})
                        if fungi:
                            summary = ", ".join(f"{name}: {value}" for name, value in fungi.items())
                            ui.label(summary).classes("text-xs text-slate-500 mt-2")
                        if lab.get("observations"):
                            ui.label(lab["observations"]).classes("text-sm text-slate-700 mt-2")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("Apply Lab Results", icon="check_circle", on_click=apply_lab).props("unelevated color=primary")

                if job.get("air_lab_rows") or job.get("surface_lab_rows"):
                    ui.separator().classes("my-5")
                    ui.label("Applied Results").classes("text-lg font-semibold text-slate-800")

                    samples_by_id = {sample["id"]: sample for sample in job.get("samples", [])}
                    for sample in job.get("samples", []):
                        if not sample.get("lab_determination"):
                            continue
                        with ui.card().classes("w-full p-4 mt-3 shadow-none border border-slate-200"):
                            with ui.row().classes("w-full items-center"):
                                ui.label(sample_name(sample)).classes("font-semibold")
                                ui.space()
                                ui.label(sample.get("lab_determination", "")).classes(
                                    "px-2 py-1 rounded bg-slate-100 text-slate-700 text-xs font-medium"
                                )
                            if sample.get("lab_total_spores") is not None:
                                ui.label(f"Total spores: {sample['lab_total_spores']} spores/m³").classes("text-sm text-slate-600")
                            if sample.get("lab_fungi"):
                                ui.label(
                                    ", ".join(f"{name}: {value}" for name, value in sample["lab_fungi"].items())
                                ).classes("text-xs text-slate-500")

            lab_section()

        with ui.tab_panel(report_tab).classes("px-0"):
            def lab_pdf_bytes() -> bytes | None:
                filename = job.get("lab_filename")
                if not filename:
                    return None
                return documents.read_bytes(job["id"], "lab", filename)

            def generate_review_draft():
                try:
                    report = create_report(job, report_photos(), lab_pdf_bytes())
                    output = report.getvalue()
                except Exception as exc:
                    ui.notify(f"Could not generate draft: {exc}", type="negative", multi_line=True)
                    return
                filename = f"{_safe_filename(job.get('client_name', ''), 'Client')}_Mold_Assessment_DRAFT.docx"
                documents.save_bytes(job["id"], "reports", filename, output)
                job["latest_draft_filename"] = filename
                save_job(job, notify=False)
                documents_section.refresh()
                ui.download(
                    output,
                    filename=filename,
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                ui.notify("Review draft generated", type="positive")

            def generate_review_pdf():
                try:
                    report = create_customer_pdf(job, report_photos())
                    output = report.getvalue()
                except Exception as exc:
                    ui.notify(f"Could not generate PDF draft: {exc}", type="negative", multi_line=True)
                    return
                filename = f"{_safe_filename(job.get('client_name', ''), 'Client')}_Mold_Assessment_DRAFT.pdf"
                documents.save_bytes(job["id"], "reports", filename, output)
                job["latest_pdf_draft_filename"] = filename
                save_job(job, notify=False)
                documents_section.refresh()
                ui.download(output, filename=filename, media_type="application/pdf")
                ui.notify("Scarlet-style PDF draft generated", type="positive")

            def generate_final_pdf():
                lab_bytes = lab_pdf_bytes()
                issues = final_report_issues(job, lab_pdf_present=lab_bytes is not None)
                if issues:
                    ui.notify("Final PDF blocked: " + "; ".join(issues), type="negative", multi_line=True)
                    report_section.refresh()
                    return
                try:
                    report = create_customer_pdf(job, report_photos())
                    output = report.getvalue()
                except Exception as exc:
                    ui.notify(f"Could not generate final PDF: {exc}", type="negative", multi_line=True)
                    return
                filename = f"{_safe_filename(job.get('client_name', ''), 'Client')}_Mold_Assessment_FINAL.pdf"
                documents.save_bytes(job["id"], "reports", filename, output)
                job["latest_final_pdf_filename"] = filename
                advance_status_if("report_review", "ready_to_send")
                save_job(job, notify=False)
                status_section.refresh()
                documents_section.refresh()
                report_section.refresh()
                ui.download(output, filename=filename, media_type="application/pdf")
                ui.notify("Final customer PDF generated", type="positive")

            def generate_final_report():
                lab_bytes = lab_pdf_bytes()
                issues = final_report_issues(job, lab_pdf_present=lab_bytes is not None)
                if issues:
                    ui.notify("Final report blocked: " + "; ".join(issues), type="negative", multi_line=True)
                    report_section.refresh()
                    return
                try:
                    report = create_report(job, report_photos(), lab_bytes)
                    output = report.getvalue()
                except Exception as exc:
                    ui.notify(f"Could not generate final report: {exc}", type="negative", multi_line=True)
                    return

                filename = f"{_safe_filename(job.get('client_name', ''), 'Client')}_Mold_Assessment_FINAL.docx"
                documents.save_bytes(job["id"], "reports", filename, output)
                job["latest_final_filename"] = filename
                advance_status_if("report_review", "ready_to_send")
                save_job(job, notify=False)
                status_section.refresh()
                documents_section.refresh()
                report_section.refresh()
                ui.download(
                    output,
                    filename=filename,
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                ui.notify("Final report generated", type="positive")

            @ui.refreshable
            def report_section():
                ui.label("Report Review").classes("text-xl font-semibold text-slate-800")
                suggestion = job.get("suggested_report_outcome")
                if suggestion:
                    with ui.card().classes("w-full max-w-xl p-3 mb-3 bg-blue-50 border border-blue-200 shadow-none"):
                        ui.label("Lab-derived suggestion").classes("font-semibold text-blue-800")
                        ui.label(suggestion).classes("text-blue-900")
                        ui.label(
                            job.get("suggested_report_outcome_reason", "Licensed consultant review required.")
                        ).classes("text-xs text-blue-700")

                ui.select(
                    REPORT_OUTCOMES,
                    value=job.get("report_outcome", "Pending consultant review"),
                    label="Consultant Report Outcome",
                    on_change=lambda e: job.__setitem__("report_outcome", e.value),
                ).props("outlined").classes("w-full max-w-xl mt-3")

                lab_present = bool(job.get("lab_filename")) and documents.exists(
                    job["id"], "lab", job.get("lab_filename", "")
                )
                issues = final_report_issues(job, lab_pdf_present=lab_present)

                with ui.card().classes("w-full p-4 mt-4 shadow-sm border border-slate-200"):
                    if issues:
                        ui.label("Final report is not ready").classes("font-semibold text-amber-700")
                        for issue in issues:
                            ui.label(f"• {issue}").classes("text-sm text-slate-600")
                    else:
                        ui.label("Final report validation passed").classes("font-semibold text-green-700")
                        ui.label(
                            "The job has the required fields, sample assignments, lab PDF, reviewed area findings, and consultant outcome."
                        ).classes("text-sm text-slate-600")

                with ui.row().classes("w-full justify-end gap-2 mt-4"):
                    ui.button("Save Outcome", icon="save", on_click=persist).props("outline color=primary")
                    ui.button("Generate Review DOCX", icon="description", on_click=generate_review_draft).props("outline color=primary")
                    ui.button("Generate Review PDF", icon="picture_as_pdf", on_click=generate_review_pdf).props("outline color=primary")
                    ui.button("Generate Final DOCX", icon="description", on_click=generate_final_report).props("outline color=primary")
                    ui.button("Generate Final PDF", icon="picture_as_pdf", on_click=generate_final_pdf).props(
                        "unelevated color=primary"
                    )

            report_section()

        with ui.tab_panel(docs_tab).classes("px-0"):
            @ui.refreshable
            def documents_section():
                ui.label("Documents").classes("text-xl font-semibold text-slate-800")
                files = documents.list_files(job["id"])
                if not files:
                    empty_state(
                        "No stored documents",
                        "Generated COCs, uploaded lab PDFs, inspection photos, and generated assessment reports will appear here.",
                    )
                    return
                for item in files:
                    path = Path(item["path"])
                    with ui.card().classes("w-full p-4 mt-3 shadow-sm border border-slate-200"):
                        with ui.row().classes("w-full items-center gap-3"):
                            ui.icon("description").classes("text-slate-400")
                            with ui.column().classes("gap-0 grow"):
                                ui.label(item["name"]).classes("font-semibold text-slate-800")
                                ui.label(f"{item['category']} · {item['size'] / 1024:.1f} KB").classes(
                                    "text-xs text-slate-500"
                                )
                            ui.button(
                                "Download",
                                icon="download",
                                on_click=lambda p=path, name=item["name"]: ui.download(p, filename=name),
                            ).props("flat color=primary")

            documents_section()


ui.add_head_html(
    """
    <style>
      body { background: #f8fafc; }
      .q-tab__label { font-weight: 600; }
      .q-field--outlined .q-field__control { border-radius: 10px; }
      .q-card { border-radius: 14px; }
    </style>
    """,
    shared=True,
)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="MTAR",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=os.environ.get("MTAR_RELOAD", "1") == "1",
    )
