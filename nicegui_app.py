from __future__ import annotations

from datetime import date
import os
from pathlib import Path

from nicegui import ui

from job_store import SQLiteJobStore, new_persistent_job
from models import new_area, new_sample
from workflow import ALLOWED_TRANSITIONS, JOB_STATUSES, transition_job


DB_PATH = Path(os.environ.get("MTAR_DB_PATH", "mtar_jobs.sqlite3"))
store = SQLiteJobStore(DB_PATH)

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
    "Visual mold present",
    "No mold detected",
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
    except ValueError:
        return fallback


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
            "flow_rate_minutes": 10,
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
        ui.notify("Job saved", type="positive")
    return job


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
        "Persistent job workflow for inspections, samples, lab results, and final reports.",
    )

    jobs = store.list(limit=200)

    def create_job():
        job = create_field_job()
        save_job(job, notify=False)
        ui.navigate.to(f"/jobs/{job['id']}")

    with ui.row().classes("w-full items-center justify-between mb-2"):
        with ui.row().classes("gap-3"):
            active = [j for j in jobs if j.status != "closed"]
            ui.label(f"{len(active)} Active").classes("px-3 py-1 rounded-full bg-blue-50 text-blue-700 font-medium")
            ui.label(f"{len(jobs)} Total").classes("px-3 py-1 rounded-full bg-slate-100 text-slate-700 font-medium")
        ui.button("New Job", icon="add", on_click=create_job).props("unelevated color=primary")

    if not jobs:
        empty_state(
            "No jobs yet",
            "Create the first assessment job. It will be stored persistently instead of living only in browser session state.",
            "Create First Job",
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
        page_shell("Job not found")
        ui.label("This job no longer exists or the link is invalid.").classes("text-red-600")
        ui.button("Back to Dashboard", on_click=lambda: ui.navigate.to("/"))
        return

    page_shell(
        job.get("client_name") or "New Assessment",
        f"Job {job.get('id')} · {job.get('address') or 'Property address pending'}",
    )

    def persist():
        save_job(job)

    def change_status(new_status: str):
        nonlocal job
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
                ui.notify("Inspection area added", type="positive")

            def remove_area(area_id: str):
                job["areas"] = [area for area in job.get("areas", []) if area.get("id") != area_id]
                for sample in job.get("samples", []):
                    if sample.get("area_id") == area_id:
                        sample["area_id"] = None
                save_job(job, notify=False)
                areas_section.refresh()
                samples_section.refresh()
                ui.notify("Inspection area removed; linked samples are now unassigned", type="warning")

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
                            "flow_rate_minutes": 10,
                        }
                    )
                else:
                    sample["sample_type_code"] = "SW"
                sample.update({"serial_number": "", "mold_analysis": True})
                job.setdefault("samples", []).append(sample)
                save_job(job, notify=False)
                samples_section.refresh()
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
                ui.notify("Sample removed", type="positive")

            @ui.refreshable
            def samples_section():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Samples").classes("text-xl font-semibold text-slate-800")
                    with ui.row().classes("gap-2"):
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
                            ui.label(
                                "Outdoor Control" if protected else f"Sample {index}"
                            ).classes("text-sm font-medium text-slate-500")
                            ui.label(sample.get("type", "")).classes(
                                "px-2 py-1 rounded bg-slate-100 text-slate-600 text-xs"
                            )
                            ui.space()
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
                                value=sample.get("serial_number", ""),
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

        with ui.tab_panel(lab_tab).classes("px-0"):
            empty_state(
                "Lab automation is next",
                "This tab will receive PRO-LAB PDFs automatically, match them to this job, parse sample results, and flag exceptions for review.",
            )

        with ui.tab_panel(report_tab).classes("px-0"):
            empty_state(
                "Report workflow is next",
                "The existing report builder will be connected here after persistent job editing is stable. Final generation will use the shared validation gate.",
            )

        with ui.tab_panel(docs_tab).classes("px-0"):
            empty_state(
                "Document center is next",
                "COCs, lab PDFs, photos, DOCX drafts, and final PDFs will be organized here under the job record.",
            )


ui.add_head_html(
    """
    <style>
      body { background: #f8fafc; }
      .q-tab__label { font-weight: 600; }
      .q-field--outlined .q-field__control { border-radius: 10px; }
      .q-card { border-radius: 14px; }
    </style>
    """
)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="MTAR",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=os.environ.get("MTAR_RELOAD", "1") == "1",
    )
