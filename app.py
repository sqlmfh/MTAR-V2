"""MTAR Streamlit app: assessments, PRO-LAB reports, and report review.

All workflow rules live in ``mtar_services``; this file only renders pages
and passes user input to those services.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
import threading
from zoneinfo import ZoneInfo

import fitz
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import mtar_services as svc
from workflow import ALLOWED_TRANSITIONS

st.set_page_config(page_title="MTAR", page_icon="🦠", layout="wide")

# Colors here are translucent or inherit the text color, so they read well in
# both the light and dark themes defined in .streamlit/config.toml.
st.markdown(
    """
<style>
    .mtar-muted {opacity: 0.7; font-size: 0.9rem;}
    .mtar-pill {display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
                background: rgba(100, 116, 139, 0.18); font-size: 0.8rem; font-weight: 600;}
    .mtar-pill-red {background: rgba(220, 38, 38, 0.16); color: #E5484D;}
    .mtar-pill-green {background: rgba(22, 163, 74, 0.16); color: #22A55B;}
    .mtar-source {opacity: 0.7; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;}
</style>
""",
    unsafe_allow_html=True,
)


# Times are stored in UTC and shown in the office's local time.
LOCAL_TZ = ZoneInfo(os.environ.get("MTAR_TIMEZONE", "America/Chicago"))


def local_time(value) -> str:
    """Format a UTC datetime or ISO string as e.g. "Oct 02, 3:32 PM CDT"."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(LOCAL_TZ)
    return f"{local:%b %d}, {local:%I:%M %p}".replace(", 0", ", ") + f" {local:%Z}"


IMAGE_TYPES = ["jpg", "jpeg", "png", "webp", "heic", "heif"]


# ---------------------------------------------------------------------------
# Background Gmail polling (one thread per server process)
# ---------------------------------------------------------------------------


@st.cache_resource
def _start_gmail_poller() -> threading.Thread | None:
    if not (svc.gmail_client.configured() or svc.drive_client.configured()):
        return None
    thread = threading.Thread(target=svc.gmail_poll_loop, name="mtar-gmail-poller", daemon=True)
    thread.start()
    return thread


_start_gmail_poller()


# ---------------------------------------------------------------------------
# Navigation and messages
# ---------------------------------------------------------------------------


def open_assessment(job_id: str | None) -> None:
    st.query_params.clear()
    if job_id:
        st.query_params["assessment"] = job_id


def open_lab_reports() -> None:
    st.query_params.clear()
    st.query_params["page"] = "lab-reports"


def flash(kind: str, message: str) -> None:
    st.session_state.setdefault("flash", []).append((kind, message))


def show_flash() -> None:
    for kind, message in st.session_state.pop("flash", []):
        getattr(st, kind)(message)


def run_action(action, *args, success: str | None = None, **kwargs):
    """Call a service, turning a ValueError into a visible message."""
    try:
        result = action(*args, **kwargs)
    except ValueError as exc:
        flash("error", str(exc))
        return None
    except Exception as exc:  # unexpected failure: still show it, never hide it
        flash("error", f"Something went wrong: {exc}")
        return None
    if success:
        flash("success", success)
    return result


def as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def next_step(job: dict) -> str:
    status = job.get("status")
    if status in {"draft", "inspection"}:
        return "Inspection in progress"
    if status == "awaiting_lab":
        return "Waiting for PRO-LAB results"
    if status == "lab_received":
        return "Lab received, confirm sample matching"
    if status == "report_review":
        issues = svc.final_issues(job)
        if issues:
            return f"Waiting for inspection information ({len(issues)} item{'s' if len(issues) != 1 else ''})"
        return "Report ready for review"
    if status == "ready_to_send":
        return "Final report generated"
    return svc.status_label(status)


def download_stored(job: dict, category: str, filename: str | None, label: str, key: str) -> None:
    if not filename:
        return
    content = svc.documents.read_bytes(job["id"], category, filename)
    if content is None:
        return
    mime = svc.DOCX_MIME if filename.endswith(".docx") else "application/pdf"
    st.download_button(label, content, file_name=filename, mime=mime, key=key, width="stretch")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## MTAR")
        st.caption("Mold Testing & Removal · assessment reports")
        if st.button("Dashboard", icon=":material/dashboard:", width="stretch"):
            open_assessment(None)
            st.rerun()
        if st.button("PRO-LAB Reports", icon=":material/science:", width="stretch"):
            open_lab_reports()
            st.rerun()
        if st.button("New Assessment", icon=":material/add:", width="stretch", type="primary"):
            job = svc.save_job(svc.create_field_job())
            open_assessment(job["id"])
            st.rerun()
        st.caption("Dark mode: open the ⋮ menu at the top right and pick Dark (or System to follow your computer).")

        st.divider()
        st.markdown("**PRO-LAB Gmail intake**")
        if svc.gmail_client.configured():
            last = svc.LAST_GMAIL_CHECK
            if last.get("status") == "ok":
                st.caption(
                    f"Last check {local_time(last['at'])}: {last['checked']} PDFs, "
                    f"{last.get('waiting', 0)} new lab reports waiting, {last['created']} assessments created, "
                    f"{last['ambiguous']} need review."
                )
            elif last.get("status") == "error":
                st.caption(f":red[Last check failed: {last.get('error')}]")
            st.caption(f"Checks automatically every {svc.GMAIL_POLL_SECONDS // 60} minute(s).")
            if st.button("Check Gmail Now", icon=":material/mail:", width="stretch"):
                with st.spinner("Checking Gmail…"):
                    result = run_action(svc.check_gmail_now)
                if result:
                    flash(
                        "warning" if result["ambiguous"] else "success",
                        f"Checked {result['checked']} PDF attachment(s): {result.get('waiting', 0)} new lab report(s) "
                        f"waiting on the PRO-LAB Reports page, {result['created']} assessment(s) created, "
                        f"{len(result['ambiguous'])} need review, ignored {result['ignored']}.",
                    )
                st.rerun()
        else:
            st.caption(
                ":orange[Gmail is not configured on this deployment.] Set GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN in the host's environment variables. "
                "You can still upload a PRO-LAB PDF on the PRO-LAB Reports page."
            )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


DASHBOARD_VIEWS = ["Active", "Completed", "All"]


def _job_address(job: dict) -> str:
    return ", ".join(p for p in [job.get("address"), job.get("city")] if p)


def _job_label(job: dict) -> str:
    return f"{job.get('client_name') or 'New Assessment'} · {_job_address(job) or 'no address'}"


def _in_view(job: dict, view: str) -> bool:
    if view == "Active":
        return job.get("status") != "closed"
    if view == "Completed":
        return job.get("status") == "closed"
    return True


def _matches_search(job: dict, query: str) -> bool:
    haystack = " ".join(
        str(part or "") for part in [
            job.get("client_name"), job.get("address"), job.get("city"), job.get("zip"), svc.lab_report_number(job),
        ]
    ).lower()
    return all(word in haystack for word in query.lower().split())


def _dashboard_step(job: dict) -> str:
    if job.get("status") == "closed":
        return "Finished report attached" if job.get("finished_report_filename") else "Done"
    return next_step(job)


def _table_step(job: dict) -> str:
    """A shorter next step that fits the dashboard table."""
    if job.get("status") == "report_review":
        issues = svc.final_issues(job)
        if issues:
            return f"Needs inspection info ({len(issues)})"
    return _dashboard_step(job)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _reset_selection() -> None:
    """Clear the table's ticked rows after anything changes the list."""
    st.session_state["dash_version"] = st.session_state.get("dash_version", 0) + 1


def _complete_jobs(jobs: list[dict]) -> None:
    for job in jobs:
        svc.complete_assessment(job)
    _reset_selection()
    names = ", ".join(job.get("client_name") or "New Assessment" for job in jobs)
    flash("success", f"Marked completed: {names}. Find them under **Completed**.")


def _reopen_jobs(jobs: list[dict]) -> None:
    for job in jobs:
        svc.reopen_assessment(job)
    _reset_selection()
    flash("success", f"Reopened {_plural(len(jobs), 'assessment')}.")


def render_delete_confirmation() -> bool:
    """Ask once more before deleting. Returns True while the question is open."""
    pending = st.session_state.get("dash_delete")
    if not pending:
        return False
    jobs = [job for job_id in pending if (job := svc.store.get(job_id))]
    if not jobs:
        st.session_state.pop("dash_delete", None)
        return False
    with st.container(border=True):
        count = _plural(len(jobs), "assessment")
        st.warning(f"Delete {count} with all their photos, COCs, lab PDFs and reports? This can't be undone.")
        for job in jobs:
            st.markdown(f"- {_job_label(job)}")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button(f"Yes, delete {count}", type="primary", key="dash_delete_yes", width="stretch"):
            deleted = sum(1 for job in jobs if run_action(svc.delete_assessment, job["id"]))
            st.session_state.pop("dash_delete", None)
            _reset_selection()
            flash("success", f"Deleted {_plural(deleted, 'assessment')}.")
            st.rerun()
        if c2.button("Cancel", key="dash_delete_no", width="stretch"):
            st.session_state.pop("dash_delete", None)
            st.rerun()
    return True


def render_selected_job(job: dict) -> None:
    """Summary and actions for the one assessment ticked in the table."""
    closed = job.get("status") == "closed"
    files = svc.documents.list_files(job["id"])
    facts = [
        svc.status_label(job.get("status")),
        f"Lab report #{svc.lab_report_number(job)}" if svc.lab_report_number(job) else "No lab report yet",
        _plural(len(job.get("samples", [])), "sample"),
        _plural(len(job.get("photos", [])), "photo"),
        _plural(len(files), "file"),
    ]
    if job.get("latest_final_pdf_filename"):
        facts.append("final PDF made")
    with st.container(border=True):
        st.markdown(f"**{job.get('client_name') or 'New Assessment'}** · {_job_address(job) or 'Property address not entered'}")
        st.caption(" · ".join(facts))
        c = st.columns(4)
        if c[0].button("Open", key="dash_open", type="primary", icon=":material/open_in_new:", width="stretch"):
            open_assessment(job["id"])
            st.rerun()
        if closed:
            if c[1].button("Reopen", key="dash_reopen", icon=":material/undo:", width="stretch"):
                _reopen_jobs([job])
                st.rerun()
        elif c[1].button("Mark completed", key="dash_complete", icon=":material/task_alt:", width="stretch"):
            _complete_jobs([job])
            st.rerun()
        with c[2].popover("Attach report", icon=":material/upload_file:", width="stretch"):
            with st.form(f"dash_attach_{job['id']}", clear_on_submit=True, border=False):
                upload = st.file_uploader("Your finished report (PDF or Word)", type=["pdf", "docx"])
                complete = st.checkbox("Mark this assessment completed", value=not closed, disabled=closed)
                if st.form_submit_button("Attach", type="primary") and upload:
                    if run_action(svc.attach_finished_report, job, upload.getvalue(), upload.name, complete=complete):
                        _reset_selection()
                        flash("success", f"Attached {upload.name} to {job.get('client_name') or 'the assessment'}.")
                    st.rerun()
        if c[3].button("Delete", key="dash_delete_one", icon=":material/delete:", width="stretch"):
            st.session_state["dash_delete"] = [job["id"]]
            st.rerun()
        download_stored(
            job, "finished", job.get("finished_report_filename"),
            f"Your finished report: {job.get('finished_report_filename')}", "dash_finished_dl",
        )


def render_bulk_actions(jobs: list[dict]) -> None:
    open_jobs = [job for job in jobs if job.get("status") != "closed"]
    closed_jobs = [job for job in jobs if job.get("status") == "closed"]
    with st.container(border=True):
        st.markdown(f"**{len(jobs)} assessments selected**")
        c = st.columns(3)
        if open_jobs and c[0].button(f"Mark {len(open_jobs)} completed", key="dash_bulk_complete", icon=":material/task_alt:", width="stretch"):
            _complete_jobs(open_jobs)
            st.rerun()
        if closed_jobs and c[1].button(f"Reopen {len(closed_jobs)}", key="dash_bulk_reopen", icon=":material/undo:", width="stretch"):
            _reopen_jobs(closed_jobs)
            st.rerun()
        if c[2].button(f"Delete {len(jobs)}", key="dash_bulk_delete", icon=":material/delete:", width="stretch"):
            st.session_state["dash_delete"] = [job["id"] for job in jobs]
            st.rerun()


def render_finished_report_import() -> None:
    with st.expander("Attach reports you already finished", icon=":material/task_alt:"):
        st.caption(
            "Add the reports you finished outside MTAR (PDF or Word). MTAR finds each one's assessment by "
            "the property address in the report, attaches it, and marks the assessment completed."
        )
        with st.form("finished_report_import", clear_on_submit=True):
            uploads = st.file_uploader("Finished reports", type=["pdf", "docx"], accept_multiple_files=True)
            if st.form_submit_button("Attach and complete", type="primary") and uploads:
                with st.spinner("Matching reports to assessments…"):
                    results = run_action(svc.import_finished_reports, [(u.name, u.getvalue()) for u in uploads]) or []
                matched = [r for r in results if r["job_id"]]
                missed = [r for r in results if not r["job_id"]]
                if matched:
                    _reset_selection()
                    flash("success", "Attached and completed: " + "; ".join(
                        f"{r['filename']} → {r['client_name'] or 'assessment'}" for r in matched
                    ))
                if missed:
                    flash("warning", "Not attached: " + "; ".join(f"{r['filename']} ({r['reason']})" for r in missed)
                          + ". To attach a report by hand, tick its assessment in the table and use **Attach report**.")
                st.rerun()


def render_manual_lab_import() -> None:
    with st.expander("Import a PRO-LAB PDF by hand", icon=":material/upload_file:"):
        st.caption(
            "Same as an email from PRO-LAB: MTAR reads the report, creates or finds the customer, "
            "property and assessment, imports the samples and builds a draft report."
        )
        with st.form("manual_lab_import", clear_on_submit=True):
            upload = st.file_uploader("PRO-LAB Certificate of Mold Analysis", type=["pdf"])
            if st.form_submit_button("Import", type="primary") and upload:
                with st.spinner("Reading the lab report and building the draft…"):
                    result = run_action(svc.create_from_lab_upload, upload.getvalue(), upload.name)
                if result:
                    if result["status"] == "invalid":
                        flash("error", f"Not imported: {result['reason']}")
                    elif result["status"] == "duplicate":
                        flash("info", "This PRO-LAB report is already imported. Opening the existing assessment.")
                        open_assessment(result["job_id"])
                    else:
                        flash("success", "Lab report imported and draft report generated.")
                        open_assessment(result["job_id"])
                st.rerun()


def render_dashboard() -> None:
    st.title("Assessments")
    with st.container():
        show_flash()

    summaries = svc.store.list(limit=500)
    jobs = [job for summary in summaries if (job := svc.store.get(summary.id))]

    waiting = [entry for entry in svc.lab_reports() if entry["status"] in {"new", "review"}]
    if waiting:
        notice, go = st.columns([4, 1], vertical_alignment="center")
        notice.info(f"{_plural(len(waiting), 'PRO-LAB report')} waiting for you to create an assessment.",
                    icon=":material/science:")
        if go.button("See lab reports", key="dash_lab_reports", width="stretch"):
            open_lab_reports()
            st.rerun()

    if not jobs:
        st.info("No assessments yet. Start one with **New Assessment**, or create one from a PRO-LAB report.")
        render_finished_report_import()
        return

    top = st.columns([2, 3])
    view = top[0].segmented_control(
        "Show", DASHBOARD_VIEWS, default="Active", required=True, key="dash_view", label_visibility="collapsed",
    ) or "Active"
    query = top[1].text_input(
        "Search", placeholder="Search name, address or lab report #", key="dash_search",
        label_visibility="collapsed", icon=":material/search:",
    ).strip()

    rows = [job for job in jobs if _in_view(job, view) and _matches_search(job, query)]
    rows.sort(key=lambda job: (as_date(job.get("inspection_date")) or date.min, job.get("created_at", "")), reverse=True)
    active = sum(1 for job in jobs if job.get("status") != "closed")
    st.caption(f"{active} active · {len(jobs) - active} completed · showing {len(rows)}")

    if not rows:
        st.info("Nothing matches." if query else f"No {view.lower()} assessments.")
        selected = []
    else:
        table = pd.DataFrame(
            [
                {
                    "Customer": job.get("client_name") or "New Assessment",
                    "Address": _job_address(job) or "Not entered",
                    "Inspected": as_date(job.get("inspection_date")),
                    "Status": svc.status_label(job.get("status")),
                    "Lab report #": svc.lab_report_number(job),
                    "Next step": _table_step(job),
                }
                for job in rows
            ]
        )
        event = st.dataframe(
            table,
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key=f"dash_table_{view}_{query}_{st.session_state.get('dash_version', 0)}",
            column_config={"Inspected": st.column_config.DateColumn(format="MMM D, YYYY")},
        )
        selected = [rows[i] for i in event.selection.rows if i < len(rows)]

    if not render_delete_confirmation():
        if len(selected) == 1:
            render_selected_job(selected[0])
        elif selected:
            render_bulk_actions(selected)
        elif rows:
            st.caption("Tick the box at the start of a row to open, complete, attach a report to, or delete it.")

    render_finished_report_import()


# ---------------------------------------------------------------------------
# PRO-LAB Reports page
# ---------------------------------------------------------------------------

LAB_VIEWS = ["Waiting", "All"]


def _reset_lab_selection() -> None:
    st.session_state["lab_version"] = st.session_state.get("lab_version", 0) + 1


def _report_title(entry: dict) -> str:
    return f"{entry['customer'] or 'Customer unknown'} · {entry['location'] or 'test location unknown'}"


def _report_matches(entry: dict, query: str) -> bool:
    haystack = " ".join([entry["customer"], entry["location"], entry["report_number"], entry["filename"]]).lower()
    return all(word in haystack for word in query.lower().split())


def _create_from_reports(entries: list[dict]) -> None:
    created = []
    for entry in entries:
        job = run_action(svc.create_assessment_from_report, entry)
        if job:
            created.append(job)
    _reset_lab_selection()
    if len(created) == 1:
        flash("success", f"Assessment created for {created[0].get('client_name') or 'this lab report'}.")
        open_assessment(created[0]["id"])
    elif created:
        flash("success", f"Created {_plural(len(created), 'assessment')}: "
              + ", ".join(job.get("client_name") or "New Assessment" for job in created) + ".")


def render_selected_report(entry: dict, jobs: list[dict]) -> None:
    status = entry["status"]
    facts = [svc.LAB_REPORT_STATUSES[status]]
    if entry["report_number"]:
        facts.append(f"Report #{entry['report_number']}")
    if entry["report_date"]:
        facts.append(f"{entry['report_date']:%b %d, %Y}")
    facts.append(entry["filename"])
    pdf = svc.lab_report_pdf(entry)
    with st.container(border=True):
        st.markdown(f"**{entry['customer'] or 'Customer unknown'}** · {entry['location'] or 'Test location unknown'}")
        st.caption(" · ".join(facts))
        if status == "review":
            st.caption(f"MTAR could not read this report with confidence: {entry['reason']}")
        c = st.columns(4)
        if status == "linked":
            if c[0].button("Open assessment", key="lab_open", type="primary", icon=":material/open_in_new:", width="stretch"):
                open_assessment(entry["job_id"])
                st.rerun()
        elif c[0].button("Create assessment", key="lab_create", type="primary", icon=":material/add:", width="stretch"):
            _create_from_reports([entry])
            st.rerun()
        if pdf:
            c[1].download_button(
                "Download PDF", pdf, file_name=entry["filename"], mime="application/pdf", key="lab_download",
                icon=":material/download:", width="stretch",
            )
        if status in {"new", "review"}:
            with c[2].popover("Add to existing", icon=":material/link:", width="stretch"):
                options = {job["id"]: _job_label(job) for job in jobs}
                target = st.selectbox("Assessment", list(options), format_func=options.get, index=None, key="lab_target")
                if st.button("Attach lab report", key="lab_attach", type="primary", disabled=not target):
                    job = run_action(svc.attach_inbox_item_to_job, entry["message_id"], target, success="Lab report attached.")
                    _reset_lab_selection()
                    if job:
                        open_assessment(job["id"])
                    st.rerun()
            if c[3].button("Dismiss", key="lab_dismiss", icon=":material/visibility_off:", width="stretch",
                           help="Hide it from Waiting. You can still find it under All."):
                svc.ignore_inbox_item(entry["message_id"])
                _reset_lab_selection()
                flash("info", f"Dismissed {entry['filename']}.")
                st.rerun()
        if pdf:
            _pdf_preview(pdf, entry["key"], per_row=2, dpi=100)


def render_lab_reports() -> None:
    st.title("PRO-LAB Reports")
    with st.container():
        show_flash()

    auto = st.toggle(
        "Create assessments automatically when a new PRO-LAB report arrives",
        value=svc.auto_create_enabled(),
        key="lab_auto_create",
        help="When this is off, new lab reports wait here until you create an assessment. "
        "A lab report for an assessment that is Awaiting Lab is always attached to it automatically.",
    )
    if auto != svc.auto_create_enabled():
        svc.set_auto_create(auto)

    jobs = [job for summary in svc.store.list(limit=500) if (job := svc.store.get(summary.id))]
    reports = svc.lab_reports()
    top = st.columns([2, 3])
    view = top[0].segmented_control(
        "Show", LAB_VIEWS, default="Waiting", required=True, key="lab_view", label_visibility="collapsed",
    ) or "Waiting"
    query = top[1].text_input(
        "Search", placeholder="Search customer, address or report #", key="lab_search",
        label_visibility="collapsed", icon=":material/search:",
    ).strip()

    waiting = [e for e in reports if e["status"] in {"new", "review"}]
    rows = [e for e in (waiting if view == "Waiting" else reports) if _report_matches(e, query)]
    st.caption(f"{len(waiting)} waiting · {len(reports)} lab reports in all · showing {len(rows)}")

    selected = []
    if not rows:
        st.info("Nothing matches." if query else (
            "No lab reports are waiting. New PRO-LAB emails show up here." if view == "Waiting" else "No lab reports yet."
        ))
    else:
        table = pd.DataFrame(
            [
                {
                    "Customer": entry["customer"] or "Unknown",
                    "Test location": entry["location"] or "Unknown",
                    "Report #": entry["report_number"],
                    "Report date": entry["report_date"],
                    "Status": svc.LAB_REPORT_STATUSES[entry["status"]],
                }
                for entry in rows
            ]
        )
        event = st.dataframe(
            table,
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key=f"lab_table_{view}_{query}_{st.session_state.get('lab_version', 0)}",
            column_config={"Report date": st.column_config.DateColumn(format="MMM D, YYYY")},
        )
        selected = [rows[i] for i in event.selection.rows if i < len(rows)]

    if len(selected) == 1:
        render_selected_report(selected[0], jobs)
    elif selected:
        creatable = [e for e in selected if e["status"] != "linked"]
        dismissable = [e for e in selected if e["status"] in {"new", "review"}]
        with st.container(border=True):
            st.markdown(f"**{len(selected)} lab reports selected**")
            c = st.columns(3)
            if creatable and c[0].button(f"Create {_plural(len(creatable), 'assessment')}", key="lab_bulk_create",
                                         type="primary", icon=":material/add:", width="stretch"):
                _create_from_reports(creatable)
                st.rerun()
            if dismissable and c[1].button(f"Dismiss {len(dismissable)}", key="lab_bulk_dismiss",
                                           icon=":material/visibility_off:", width="stretch"):
                for entry in dismissable:
                    svc.ignore_inbox_item(entry["message_id"])
                _reset_lab_selection()
                flash("info", f"Dismissed {_plural(len(dismissable), 'lab report')}.")
                st.rerun()
    elif rows:
        st.caption("Tick a report to see it and create an assessment from it.")

    render_manual_lab_import()


# ---------------------------------------------------------------------------
# Assessment tabs
# ---------------------------------------------------------------------------


def render_overview(job: dict) -> None:
    if job.get("automation_source"):
        source = job.get("gmail_source") or {}
        st.info(
            f"Created automatically from PRO-LAB report #{(job.get('lab_metadata') or {}).get('report_number', '')}"
            + (f" (email: {source.get('subject')})" if source.get("subject") else "")
            + ". Customer, property, samples and lab results came from the lab report; "
            "inspection facts below still need the inspector."
        )

    with st.form(f"overview_{job['id']}"):
        st.markdown("#### Client & property")
        c1, c2 = st.columns(2)
        client = c1.text_input("Client name", job.get("client_name", ""))
        address = c2.text_input("Property address", job.get("address", ""))
        c3, c4, c5 = st.columns([3, 1, 2])
        city = c3.text_input("City", job.get("city", ""))
        state = c4.text_input("State", job.get("state", "TX"), max_chars=2)
        zip_code = c5.text_input("ZIP", job.get("zip", ""))

        st.markdown("#### Inspection conditions")
        d1, d2, d3, d4 = st.columns(4)
        inspection_date = d1.date_input("Assessment date", as_date(job.get("inspection_date")) or date.today())
        report_date = d2.date_input("Report date", as_date(job.get("report_date")) or date.today())
        humidity = d3.number_input(
            "Indoor RH (%)", min_value=0.0, max_value=100.0, value=None if job.get("humidity") is None else float(job["humidity"]),
            placeholder="Required for the final report",
        )
        temperature = d4.number_input("Temperature (°F)", value=None if job.get("temperature") is None else float(job["temperature"]), placeholder="Optional")

        general = st.text_area("General observations", job.get("general_observations", ""))
        if st.form_submit_button("Save", type="primary"):
            job.update(
                {
                    "client_name": client.strip(),
                    "address": address.strip(),
                    "city": city.strip(),
                    "state": state.strip().upper(),
                    "zip": zip_code.strip(),
                    "inspection_date": inspection_date,
                    "report_date": report_date,
                    "humidity": humidity,
                    "temperature": temperature,
                    "general_observations": general,
                }
            )
            svc.save_job(job)
            flash("success", "Saved.")
            st.rerun()


def render_areas(job: dict) -> None:
    top = st.columns([4, 1])
    top[0].caption(
        "Inspection areas are the rooms you inspected. Samples are separate and are assigned to areas on the Samples tab."
    )
    if top[1].button("Add area", icon=":material/add:", key=f"add_area_{job['id']}", width="stretch"):
        svc.add_area(job)
        st.rerun()

    areas = job.get("areas", [])
    if not areas:
        st.info("No inspection areas yet.")
        return

    for index, area in enumerate(areas, 1):
        with st.container(border=True):
            head = st.columns([5, 1])
            head[0].markdown(f"**Area {index}: {area.get('name') or 'Unnamed'}**")
            if head[1].button("Remove", key=f"rm_area_{area['id']}", width="stretch"):
                svc.remove_area(job, area["id"])
                flash("warning", "Area removed. Its samples are now unassigned and its photos were deleted.")
                st.rerun()

            if area.get("source") == "PRO-LAB" and not str(area.get("description", "")).strip():
                st.warning(
                    "Created from the PRO-LAB sample location. Field inspection information is still missing for this area."
                )
            if area.get("lab_summary"):
                st.markdown("<div class='mtar-source'>Lab result summary · source: PRO-LAB</div>", unsafe_allow_html=True)
                st.write(area["lab_summary"])

            with st.form(f"area_{area['id']}"):
                c1, c2 = st.columns(2)
                name = c1.text_input("Area name", area.get("name", ""))
                current = area.get("finding", "Needs consultant review")
                options = svc.FINDING_OPTIONS if current in svc.FINDING_OPTIONS else svc.FINDING_OPTIONS + [current]
                finding = c2.selectbox("Consultant finding", options, index=options.index(current))
                description = st.text_area("Visual observations (inspector)", area.get("description", ""))

                st.markdown("**Moisture assessment**")
                m1, m2 = st.columns([3, 1])
                states = list(svc.MOISTURE_STATES)
                state = m1.radio(
                    "Moisture assessment", states, format_func=svc.MOISTURE_STATES.get, horizontal=True,
                    index=states.index(area["moisture_state"]) if area.get("moisture_state") in states else None,
                    label_visibility="collapsed",
                )
                highest = m2.number_input(
                    "Highest moisture content (%)", min_value=0.0, max_value=100.0, step=1.0,
                    value=None if area.get("moisture_highest") is None else float(area["moisture_highest"]),
                    format="%.1f", placeholder="e.g. 14",
                )
                # Notes typed before the template existed carry over as the extra note.
                legacy = "" if area.get("moisture_state") or "moisture_extra" in area else area.get("moisture_notes", "")
                extra = st.text_input("Extra moisture note (optional)", area.get("moisture_extra", legacy))
                if area.get("moisture_notes"):
                    st.caption(f"In the report: {area['moisture_notes']}")

                thermal = st.text_area("Thermal imaging notes", area.get("thermal_notes", ""))
                if st.form_submit_button("Save area", type="primary"):
                    area.update(
                        {
                            "name": name.strip(),
                            "finding": finding,
                            "description": description,
                            "moisture_state": state,
                            "moisture_highest": highest,
                            "moisture_extra": extra.strip(),
                            "moisture_notes": svc.moisture_sentence(state, highest, extra),
                            "thermal_notes": thermal,
                        }
                    )
                    svc.save_job(job)
                    flash("success", f"Saved {name or 'area'}.")
                    if state and highest is None:
                        flash("warning", f"Add the highest moisture reading for {name or 'this area'}.")
                    st.rerun()

            _moisture_photos(job, area)


def _moisture_photos(job: dict, area: dict) -> None:
    """Small previews of the area's moisture photos (from Drive or uploaded)."""
    photos = [
        p for p in job.get("photos", [])
        if p.get("role") == "area" and p.get("area_id") == area["id"] and p.get("kind", "inspection") == "inspection"
    ]
    if not photos:
        st.caption(
            "No moisture photos yet. Photos in this area's Drive folder (or a Moisture subfolder inside it) "
            "show up here once Drive is linked on the Photos tab."
        )
        return
    from_drive = sum(1 for p in photos if p.get("drive_file_id"))
    st.caption(
        f"Moisture assessment photos · {len(photos)}"
        + (f" ({from_drive} from Google Drive)" if from_drive else "")
        + " · they print under Moisture Assessment in the report. Manage them on the Photos tab."
    )
    columns = st.columns(8)
    for i, photo in enumerate(photos):
        content = svc.photo_bytes(job, photo)
        if content:
            columns[i % 8].image(content, width="stretch")


def render_samples(job: dict) -> None:
    top = st.columns([2, 1, 1, 1])
    top[0].caption("Enter the actual field sampling values. They fill the PRO-LAB COC.")
    if top[1].button("Add air sample", icon=":material/air:", key=f"add_air_{job['id']}", width="stretch"):
        svc.add_sample(job, "Air Sample")
        st.rerun()
    if top[2].button("Add surface sample", icon=":material/science:", key=f"add_surf_{job['id']}", width="stretch"):
        svc.add_sample(job, "Surface Sample")
        st.rerun()
    if top[3].button("Generate COC", icon=":material/description:", key=f"coc_{job['id']}", width="stretch", type="primary"):
        result = run_action(svc.generate_coc, job, success="PRO-LAB COC generated.")
        st.rerun()

    if job.get("latest_coc_filename"):
        download_stored(job, "coc", job["latest_coc_filename"], f"Download {job['latest_coc_filename']}", f"dl_coc_{job['id']}")

    if not svc.coc_template_path():
        with st.container(border=True):
            st.markdown("**PRO-LAB COC template needed**")
            st.caption("Upload BLANK_COC.pdf once. MTAR reuses it for every assessment.")
            with st.form("coc_template", clear_on_submit=True):
                template = st.file_uploader("Blank PRO-LAB COC", type=["pdf"])
                if st.form_submit_button("Save template") and template:
                    svc.save_coc_template(template.getvalue())
                    flash("success", "COC template saved.")
                    st.rerun()

    area_options = {None: "Unassigned"} | {
        area["id"]: area.get("name") or f"Inspection Area {i}" for i, area in enumerate(job.get("areas", []), 1)
    }

    for index, sample in enumerate(job.get("samples", []), 1):
        protected = sample.get("outdoor_control", False)
        with st.container(border=True):
            head = st.columns([4, 2, 1])
            head[0].markdown(f"**{'Outdoor control' if protected else f'Sample {index}'}** · {sample.get('type', '')}")
            determination = sample.get("lab_determination")
            if determination:
                red = any(word in determination.upper() for word in ("UNUSUAL", "ELEVATED")) and "NOT ELEVATED" not in determination.upper()
                head[1].markdown(
                    f"<span class='mtar-pill {'mtar-pill-red' if red else ''}'>{determination}</span>",
                    unsafe_allow_html=True,
                )
            if not protected and head[2].button("Remove", key=f"rm_sample_{sample['id']}", width="stretch"):
                run_action(svc.remove_sample, job, sample["id"])
                st.rerun()

            with st.form(f"sample_{sample['id']}"):
                c = st.columns(4)
                name = c[0].text_input("Sample name / location", sample.get("name", ""))
                coc_line = c[1].text_input(
                    "COC / Line #", sample.get("lab_coc_line", ""), placeholder="From the lab report",
                    help="The COC and line number PRO-LAB printed for this sample, e.g. 2033849-2. It prints under the sample in the report.",
                )
                code = c[2].text_input("Sample type code", sample.get("sample_type_code", ""))
                if protected:
                    c[3].text_input("Assigned area", "Outdoor Control", disabled=True)
                    area_id = None
                else:
                    keys = list(area_options)
                    current = sample.get("area_id") if sample.get("area_id") in area_options else None
                    area_id = c[3].selectbox("Assigned area", keys, index=keys.index(current), format_func=area_options.get)
                flow = minutes = None
                if sample.get("type") == "Air Sample":
                    f = st.columns(4)
                    flow = f[0].number_input("Flow rate (L/min)", min_value=0.0, value=float(sample.get("flow_rate_liters") or 0))
                    minutes = f[1].number_input("Sampling minutes", min_value=0.0, value=float(sample.get("flow_rate_minutes") or 0))
                    f[2].metric("Total volume", f"{flow * minutes:g} L")
                if st.form_submit_button("Save sample", type="primary"):
                    sample.update({"name": name.strip(), "lab_coc_line": coc_line.strip(), "sample_type_code": code.strip().upper()})
                    if not protected:
                        sample["area_id"] = area_id
                    if flow is not None:
                        sample["flow_rate_liters"] = int(flow) if float(flow).is_integer() else flow
                        sample["flow_rate_minutes"] = int(minutes) if float(minutes).is_integer() else minutes
                    svc.save_job(job)
                    flash("success", "Sample saved.")
                    st.rerun()


def _update_photo(job_id: str, photo_id: str, field: str, widget_key: str) -> None:
    job = svc.store.get(job_id)
    for photo in job.get("photos", []):
        if photo.get("id") == photo_id:
            photo[field] = st.session_state[widget_key]
    svc.save_job(job)


SHORT_KINDS = {"sampling": "Sample", "inspection": "Moisture", "thermal": "Thermal"}


def _photo_grid(job: dict, photos: list[dict], *, with_kind: bool = False, per_row: int = 8) -> None:
    """Small previews (about a third of the old size), with no caption field."""
    columns = st.columns(per_row)
    for i, photo in enumerate(photos):
        with columns[i % per_row]:
            content = svc.photo_bytes(job, photo)
            if content:
                st.image(content, width="stretch")
            if with_kind:
                kind_key = f"kind_{photo['id']}"
                kinds = list(svc.PHOTO_KINDS)
                st.selectbox(
                    "Photo type", kinds, index=kinds.index(photo.get("kind", "inspection")),
                    format_func=SHORT_KINDS.get, key=kind_key, label_visibility="collapsed",
                    on_change=_update_photo, args=(job["id"], photo["id"], "kind", kind_key),
                )
            if st.button("Delete", key=f"del_{photo['id']}", icon=":material/delete:", width="stretch"):
                svc.delete_photo(job, photo["id"])
                st.rerun()


def _photo_upload_form(job: dict, form_key: str, label: str, role: str, *, area_id=None, multiple=True, kind_choice=False, kind="inspection"):
    with st.form(form_key, clear_on_submit=True):
        files = st.file_uploader(label, type=IMAGE_TYPES, accept_multiple_files=multiple)
        if kind_choice:
            kinds = list(svc.PHOTO_KINDS)
            kind = st.radio("These photos are", kinds, format_func=svc.PHOTO_KINDS.get, horizontal=True, index=1)
        if st.form_submit_button("Upload"):
            files = files if isinstance(files, list) else ([files] if files else [])
            added = 0
            for upload in files:
                if run_action(svc.add_photo, job, upload.getvalue(), upload.name, role, area_id=area_id, kind=kind) is not None:
                    added += 1
            if added:
                flash("success", f"Added {added} photo(s).")
            st.rerun()


def render_drive_photos(job: dict) -> None:
    with st.container(border=True):
        st.markdown("**Google Drive photos**")
        if not svc.drive_client.configured():
            st.caption(
                "Not connected. Add the Google Drive read-only permission to the Google sign-in to pull "
                "photos straight from the inspector's Drive folder."
            )
            return

        folder_id = job.get("drive_folder_id")
        if folder_id:
            folder_name = job.get("drive_folder_name")
            st.caption(
                f"Linked to [{folder_name or 'this Drive folder'}]({svc.folder_link(folder_id)}). New photos are imported "
                f"automatically every {svc.GMAIL_POLL_SECONDS // 60} minute(s). Subfolders decide placement: "
                "Property, Outdoor, RH, and one folder per area (with optional Sampling, Moisture and Thermal inside). "
                "Loose photos land in Unsorted below for you to place."
            )
            c1, c2 = st.columns([1, 1])
            if c1.button("Import from Drive now", icon=":material/cloud_download:", key=f"drive_sync_{job['id']}", width="stretch"):
                with st.spinner("Downloading photos from Drive…"):
                    result = run_action(svc.sync_drive_photos, job)
                if result:
                    flash("success", f"Imported {result[1]['imported']} new photo(s) from Drive.")
                st.rerun()
            if c2.button("Unlink folder", key=f"drive_unlink_{job['id']}", width="stretch"):
                svc.unlink_drive_folder(job)
                flash("success", "Drive folder unlinked. Photos already imported stay on this assessment.")
                st.rerun()
        else:
            folders, error = [], ""
            if svc.DRIVE_PHOTOS_FOLDER:
                try:
                    folders = svc.drive_folder_choices(job)
                except Exception as exc:  # Drive unreachable: the paste box below still works
                    error = str(exc)
            if folders:
                st.caption(
                    "MTAR links the job folder by itself when only one folder in your Jobs folder fits this "
                    "assessment by customer name, house number or lab report # (for example \"5302 Scarlet\"). "
                    "Otherwise pick it here; the likeliest folders are at the top."
                )
                best = folders[0] if svc.folder_match_score(job, folders[0].get("name", "")) >= 2 else None
                with st.form(f"drive_pick_{job['id']}", border=False):
                    choice = st.selectbox(
                        "Job folder in Drive", folders, format_func=lambda f: f.get("name", ""),
                        index=0 if best else None, placeholder="Pick this assessment's folder",
                    )
                    if st.form_submit_button("Link folder and import photos", type="primary") and choice:
                        if run_action(svc.use_drive_folder, job, choice, success=f"Linked {choice.get('name', 'the folder')}."):
                            with st.spinner("Downloading photos from Drive…"):
                                run_action(svc.sync_drive_photos, svc.store.get(job["id"]))
                        st.rerun()
            elif error:
                st.caption(f":orange[Could not read the Jobs folder in Drive: {error}]")
            elif not svc.DRIVE_PHOTOS_FOLDER:
                st.caption(
                    "Set DRIVE_PHOTOS_FOLDER to the link of your Jobs folder in the host's settings, and MTAR "
                    "finds each assessment's folder by itself."
                )
            with st.expander("Or paste a folder link"):
                with st.form(f"drive_link_{job['id']}", clear_on_submit=True, border=False):
                    link = st.text_input("Drive folder link")
                    if st.form_submit_button("Link folder") and link:
                        if run_action(svc.link_drive_folder, job, link, success="Drive folder linked."):
                            run_action(svc.sync_drive_photos, svc.store.get(job["id"]))
                        st.rerun()

        last = job.get("drive_last_sync")
        if last:
            parts = [f"Last import {local_time(last.get('at', ''))}: {last.get('imported', 0)} new"]
            if last.get("areas_created"):
                parts.append("new areas from folders: " + ", ".join(last["areas_created"]))
            st.caption(" · ".join(parts))
            if last.get("unplaced"):
                st.warning("Not imported: " + ", ".join(last["unplaced"]))
            for error in last.get("errors", []):
                st.error(error)


def render_unsorted_photos(job: dict, photos: list[dict]) -> None:
    if not photos:
        return
    targets = {"property": "Property exterior (cover)", "outdoor": "Outdoor control", "environment": "RH meter"}
    for area in job.get("areas", []):
        targets[f"area:{area['id']}"] = f"Area: {area.get('name') or 'Unnamed area'}"
    with st.container(border=True):
        st.markdown(f"**Unsorted photos from Drive · {len(photos)}**")
        st.caption(
            "These were loose in the Drive folder, so MTAR doesn't know where they go. "
            "Pick a section for each one. Unsorted photos are left out of the report."
        )
        columns = st.columns(6)
        for i, photo in enumerate(photos):
            with columns[i % 6]:
                content = svc.photo_bytes(job, photo)
                if content:
                    st.image(content, width="stretch")
                if photo.get("drive_path"):
                    st.caption(f"From {photo['drive_path']}/")
                choice = st.selectbox("Goes in", list(targets), format_func=targets.get, key=f"assign_{photo['id']}")
                if st.button("Move", key=f"move_{photo['id']}", type="primary", width="stretch"):
                    role, _, area_id = choice.partition(":")
                    run_action(svc.assign_photo, job, photo["id"], role, area_id or None)
                    st.rerun()
                if st.button("Delete", key=f"del_{photo['id']}", icon=":material/delete:", width="stretch"):
                    svc.delete_photo(job, photo["id"])
                    st.rerun()


def render_photos(job: dict) -> None:
    photos = job.get("photos", [])
    st.caption(
        "Photos are placed in the report by role. Area photos print under their area: sampling photos under the "
        "sample, moisture photos under Moisture Assessment, then thermal."
    )
    render_drive_photos(job)
    render_unsorted_photos(job, [p for p in photos if p.get("role") == "unsorted"])

    with st.container(border=True):
        st.markdown("**Property exterior (report cover)**")
        st.caption("Uploading a new property photo replaces the current one.")
        _photo_grid(job, [p for p in photos if p.get("role") == "property"])
        _photo_upload_form(job, f"up_property_{job['id']}", "Property photo", "property", multiple=False)

    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**Outdoor control sampling**")
        _photo_grid(job, [p for p in photos if p.get("role") == "outdoor"], per_row=4)
        _photo_upload_form(job, f"up_outdoor_{job['id']}", "Outdoor control photos", "outdoor", kind="sampling")
    with c2, st.container(border=True):
        st.markdown("**Environmental / RH meter**")
        _photo_grid(job, [p for p in photos if p.get("role") == "environment"], per_row=4)
        _photo_upload_form(job, f"up_env_{job['id']}", "RH meter photo", "environment", multiple=False, kind="environment")

    st.markdown("#### Inspection area photos")
    if not job.get("areas"):
        st.info("Add inspection areas before adding area photos.")
    for area in job.get("areas", []):
        area_photos = [p for p in photos if p.get("role") == "area" and p.get("area_id") == area["id"]]
        with st.expander(f"{area.get('name') or 'Unnamed area'} · {len(area_photos)} photo(s)", expanded=not area_photos):
            _photo_grid(job, area_photos, with_kind=True)
            _photo_upload_form(job, f"up_area_{area['id']}", "Area photos", "area", area_id=area["id"], kind_choice=True)


def render_lab(job: dict) -> None:
    with st.form(f"lab_upload_{job['id']}", clear_on_submit=True):
        upload = st.file_uploader("Upload PRO-LAB Certificate of Mold Analysis", type=["pdf"])
        if st.form_submit_button("Import lab PDF") and upload:
            updated = run_action(svc.attach_lab_pdf, job, upload.getvalue(), upload.name)
            if updated:
                flash("success", f"Imported {len(updated['lab_parsed']['samples'])} PRO-LAB samples. Confirm the matches below.")
            st.rerun()

    parsed = job.get("lab_parsed") or {}
    lab_samples = parsed.get("samples", [])
    if not lab_samples:
        st.info("No lab report yet. It arrives by Gmail automatically, or upload it above.")
        return

    metadata = parsed.get("metadata", {})
    m = st.columns(4)
    for column, label, value in (
        (m[0], "Report #", metadata.get("report_number")),
        (m[1], "Project", metadata.get("project_name")),
        (m[2], "Samples", len(lab_samples)),
        (m[3], "Report date", metadata.get("report_date")),
    ):
        column.caption(label)
        column.markdown(f"**{value or '—'}**")
    download_stored(job, "lab", job.get("lab_filename"), "Download original PRO-LAB PDF", f"dl_lab_{job['id']}")

    st.markdown("#### Sample matching")
    st.caption("MTAR prefers exact serial-number matches. Confirm every match, then apply the results.")
    sample_options = {s["id"]: f"{svc.sample_name(s)} · {s.get('serial_number') or s.get('lab_serial_number') or 'no serial'}" for s in job.get("samples", [])}
    mapping = job.get("lab_mapping") or {}
    with st.form(f"mapping_{job['id']}"):
        choices = {}
        for lab in lab_samples:
            c = st.columns([3, 2, 2, 3])
            c[0].markdown(f"**{lab.get('location') or 'Unnamed'}**  \n{lab.get('coc_line') or ''}")
            c[1].markdown(f"Serial  \n{lab.get('serial_number') or '—'}")
            determination = lab.get("determination") or "—"
            color = "red" if ("UNUSUAL" in determination or determination == "ELEVATED") else "gray"
            c[2].markdown(f"Determination  \n:{color}[**{determination}**]")
            keys = list(sample_options)
            current = mapping.get(lab["key"])
            choices[lab["key"]] = c[3].selectbox(
                "MTAR sample", keys, index=keys.index(current) if current in keys else None,
                format_func=sample_options.get, key=f"map_{job['id']}_{lab['key']}",
            )
        if st.form_submit_button("Apply lab results", type="primary"):
            job["lab_mapping"] = {key: value for key, value in choices.items() if value}
            if run_action(svc.apply_lab, job, success="Lab results applied."):
                pass
            else:
                svc.save_job(job)  # keep the chosen matches even when apply was refused
            st.rerun()

    applied = [s for s in job.get("samples", []) if s.get("lab_determination")]
    if applied:
        st.markdown("#### Applied results")
        for sample in applied:
            with st.container(border=True):
                st.markdown(f"**{svc.sample_name(sample)}** · {sample['lab_determination']}")
                if sample.get("lab_total_spores") is not None:
                    st.caption(f"Total spores: {sample['lab_total_spores']:,} spores/m³ · volume {sample.get('lab_volume') or '—'}")
                fungi = sample.get("lab_fungi") or {}
                if fungi:
                    st.dataframe(
                        pd.DataFrame([{"Organism": k, "Spores/m³ or result": str(v)} for k, v in fungi.items()]),
                        hide_index=True, width="stretch",
                    )
                if sample.get("lab_observations"):
                    st.caption(f"Lab observation: {sample['lab_observations']}")


def _pdf_preview(content: bytes, key: str, *, per_row: int = 3, dpi: int = 60) -> None:
    with fitz.open(stream=content, filetype="pdf") as doc:
        st.caption(f"{doc.page_count} pages")
        columns = st.columns(per_row)
        for i, page in enumerate(doc):
            columns[i % per_row].image(page.get_pixmap(dpi=dpi).tobytes("png"), caption=f"Page {i + 1}", width="stretch")


def render_report(job: dict) -> None:
    suggestion = job.get("suggested_report_outcome")
    if suggestion:
        st.info(
            f"**Lab-derived suggestion:** {suggestion}  \n"
            f"{job.get('suggested_report_outcome_reason', '')} The licensed consultant makes the final decision."
        )

    with st.form(f"outcome_{job['id']}"):
        current = job.get("report_outcome", "Pending consultant review")
        options = svc.REPORT_OUTCOMES if current in svc.REPORT_OUTCOMES else svc.REPORT_OUTCOMES + [current]
        outcome = st.selectbox(
            "Consultant report outcome", options, index=options.index(current),
            help="This one choice controls the consultant letter, conclusions and recommendations.",
        )
        if st.form_submit_button("Save outcome", type="primary"):
            job["report_outcome"] = outcome
            svc.save_job(job)
            flash("success", "Outcome saved.")
            st.rerun()

    issues = svc.final_issues(job)
    with st.container(border=True):
        if issues:
            st.markdown("**The final report is blocked until these are complete:**")
            for issue in issues:
                st.markdown(f"- {issue}")
        else:
            st.success("Final report validation passed.")

    st.markdown("#### Generate")
    g = st.columns(4)
    if g[0].button("Review DOCX", key=f"gen_rdocx_{job['id']}", width="stretch"):
        run_action(svc.generate_review_docx, job, success="Review DOCX generated.")
        st.rerun()
    if g[1].button("Review PDF", key=f"gen_rpdf_{job['id']}", width="stretch"):
        with st.spinner("Rendering PDF…"):
            run_action(svc.generate_review_pdf, job, success="Review PDF generated.")
        st.rerun()
    if g[2].button("Final DOCX", key=f"gen_fdocx_{job['id']}", disabled=bool(issues), width="stretch"):
        run_action(svc.generate_final_docx, job, success="Final DOCX generated.")
        st.rerun()
    if g[3].button("Final PDF", key=f"gen_fpdf_{job['id']}", disabled=bool(issues), type="primary", width="stretch"):
        with st.spinner("Rendering final PDF…"):
            run_action(svc.generate_final_pdf, job, success="Final customer PDF generated.")
        st.rerun()

    st.markdown("#### Latest files")
    latest = [
        ("latest_final_pdf_filename", "Final PDF"),
        ("latest_final_filename", "Final DOCX"),
        ("latest_pdf_draft_filename", "Draft PDF"),
        ("latest_draft_filename", "Draft DOCX"),
    ]
    present = [(field, label) for field, label in latest if job.get(field)]
    if not present:
        st.caption("Nothing generated yet.")
    for field, label in present:
        download_stored(job, "reports", job[field], f"{label}: {job[field]}", f"dl_{field}_{job['id']}")
    download_stored(
        job, "finished", job.get("finished_report_filename"),
        f"Your finished report: {job.get('finished_report_filename')}", f"dl_finished_{job['id']}",
    )
    with st.expander("Attach a report you finished outside MTAR", icon=":material/upload_file:"):
        with st.form(f"finished_{job['id']}", clear_on_submit=True, border=False):
            upload = st.file_uploader("Your finished report (PDF or Word)", type=["pdf", "docx"])
            closed = job.get("status") == "closed"
            complete = st.checkbox("Mark this assessment completed", value=not closed, disabled=closed)
            if st.form_submit_button("Attach", type="primary") and upload:
                run_action(svc.attach_finished_report, job, upload.getvalue(), upload.name, complete=complete,
                           success=f"Attached {upload.name}.")
                st.rerun()

    preview_name = job.get("latest_final_pdf_filename") or job.get("latest_pdf_draft_filename")
    if preview_name and st.toggle("Preview PDF pages", key=f"preview_{job['id']}"):
        content = svc.documents.read_bytes(job["id"], "reports", preview_name)
        if content:
            _pdf_preview(content, preview_name)


def render_documents(job: dict) -> None:
    files = svc.documents.list_files(job["id"])
    if not files:
        st.info("COCs, lab PDFs, photos and generated reports appear here.")
        return
    for item in files:
        c = st.columns([5, 1, 2])
        c[0].markdown(f"**{item['name']}**")
        c[1].caption(item["category"])
        with open(item["path"], "rb") as handle:
            c[2].download_button("Download", handle.read(), file_name=item["name"], key=f"doc_{item['path']}", width="stretch")


def render_assessment(job_id: str) -> None:
    job = svc.store.get(job_id)
    if not job:
        st.error("This assessment no longer exists or the link is invalid.")
        if st.button("Back to dashboard"):
            open_assessment(None)
            st.rerun()
        return

    st.title(job.get("client_name") or "New Assessment")
    address = ", ".join(p for p in [job.get("address"), job.get("city"), " ".join(filter(None, [job.get("state"), job.get("zip")]))] if p)
    st.caption(address or "Property address pending")
    # Messages go in one fixed container so the tabs below keep their place on
    # the page. When a message pushed the tabs down, Streamlit left the old tab
    # bar on screen and every Save added another copy of the form.
    with st.container():
        show_flash()
        if job.get("automatic_draft_error"):
            st.warning(f"The automatic draft could not be generated: {job['automatic_draft_error']}")

    status = job.get("status", "draft")
    allowed = [status] + sorted(ALLOWED_TRANSITIONS.get(status, set()))
    s1, s2, s3 = st.columns([2, 3.4, 1.6], vertical_alignment="bottom")
    new_status = s1.selectbox("Workflow status", allowed, format_func=svc.status_label, key=f"status_{job_id}_{status}")
    if new_status != status:
        try:
            svc.save_job(svc.transition_job(job, new_status))
            flash("success", f"Status changed to {svc.status_label(new_status)}.")
        except ValueError as exc:
            flash("error", str(exc))
        st.rerun()
    s2.markdown(f"<span class='mtar-muted'>{_dashboard_step(job)}</span>", unsafe_allow_html=True)
    if status == "closed":
        if s3.button("Reopen", key=f"reopen_{job_id}", icon=":material/undo:", width="stretch"):
            svc.reopen_assessment(job)
            flash("success", "Assessment reopened.")
            st.rerun()
    elif s3.button("Mark completed", key=f"complete_{job_id}", icon=":material/task_alt:", width="stretch",
                   help="For an assessment you already finished, for example outside MTAR."):
        svc.complete_assessment(job)
        flash("success", "Marked completed.")
        st.rerun()

    # The key keeps the open tab across reruns; without it, any message shown
    # above the tabs after a button click sent the user back to Overview.
    tab_key = f"tabs_{job_id}"
    sections = [
        ("Overview", render_overview),
        ("Inspection Areas", render_areas),
        ("Samples", render_samples),
        ("Photos", render_photos),
        ("Lab Results", render_lab),
        ("Report", render_report),
        ("Documents", render_documents),
    ]
    tabs = st.tabs([label for label, _ in sections], key=tab_key, on_change="rerun")
    for index, (tab, (_, render)) in enumerate(zip(tabs, sections)):
        with tab:
            render(job)
            if index + 1 < len(sections):
                _next_tab_button(tab_key, sections[index + 1][0])
    _scroll_to_top_if_asked()


def _go_to_tab(tab_key: str, label: str) -> None:
    st.session_state[tab_key] = label
    st.session_state["scroll_to_top"] = st.session_state.get("scroll_to_top", 0) + 1


def _next_tab_button(tab_key: str, label: str) -> None:
    """A Next button at the end of each tab, so nobody has to scroll back up."""
    st.divider()
    _, right = st.columns([3, 1])
    right.button(
        f"Next: {label}", key=f"next_{tab_key}_{label}", on_click=_go_to_tab, args=(tab_key, label),
        icon=":material/arrow_forward:", icon_position="right", width="stretch",
    )


def _scroll_to_top_if_asked() -> None:
    """After Next, show the new tab from its top instead of where the last one ended."""
    count = st.session_state.get("scroll_to_top", 0)
    if count == st.session_state.get("scrolled_to_top", 0):
        return
    st.session_state["scrolled_to_top"] = count
    # A new count makes a new frame, so the browser runs the script every time.
    components.html(
        f"""<script>/* {count} */
        const page = window.parent;
        const toTop = () => {{
            page.scrollTo(0, 0);
            page.document.querySelectorAll('[data-testid="stMain"], [data-testid="stAppViewContainer"]')
                .forEach((el) => el.scrollTo(0, 0));
        }};
        toTop(); [100, 300, 600, 1000].forEach((ms) => setTimeout(toTop, ms));
        </script>""",
        height=0,
    )


render_sidebar()
current = st.query_params.get("assessment")
if current:
    render_assessment(current)
elif st.query_params.get("page") == "lab-reports":
    render_lab_reports()
else:
    render_dashboard()
