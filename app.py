"""MTAR Streamlit app: Lab Inbox, assessments, and report review.

All workflow rules live in ``mtar_services``; this file only renders pages
and passes user input to those services.
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone
import os
import threading
from zoneinfo import ZoneInfo

import fitz
import pandas as pd
import streamlit as st

import mtar_services as svc
from workflow import ALLOWED_TRANSITIONS

st.set_page_config(page_title="MTAR", page_icon="🦠", layout="wide")

st.markdown(
    """
<style>
    h1, h2, h3 {color: #184058;}
    .mtar-muted {color: #64748b; font-size: 0.9rem;}
    .mtar-pill {display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
                background: #e2e8f0; color: #1e293b; font-size: 0.8rem; font-weight: 600;}
    .mtar-pill-red {background: #fde2e4; color: #b42318;}
    .mtar-pill-green {background: #dcfce7; color: #166534;}
    .mtar-source {color: #64748b; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;}
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
    if job_id:
        st.query_params["assessment"] = job_id
    else:
        st.query_params.clear()


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


def as_time(value) -> dt_time | None:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
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
        if st.button("New Assessment", icon=":material/add:", width="stretch", type="primary"):
            job = svc.save_job(svc.create_field_job())
            open_assessment(job["id"])
            st.rerun()

        st.divider()
        st.markdown("**PRO-LAB Gmail intake**")
        if svc.gmail_client.configured():
            last = svc.LAST_GMAIL_CHECK
            if last.get("status") == "ok":
                st.caption(
                    f"Last check {local_time(last['at'])}: {last['checked']} PDFs, "
                    f"{last['imported']} imported, {last['created']} new, {last['ambiguous']} need review."
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
                        f"Checked {result['checked']} PDF attachment(s): imported {result['imported']}, "
                        f"created {result['created']} assessment(s), {len(result['ambiguous'])} need review, "
                        f"ignored {result['ignored']}.",
                    )
                st.rerun()
        else:
            st.caption(
                ":orange[Gmail is not configured on this deployment.] Set GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN in the host's environment variables. "
                "You can still upload a PRO-LAB PDF on the dashboard."
            )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def render_lab_inbox(jobs: list[dict]) -> None:
    items = svc.store.list_inbox(status="needs_review")
    if not items:
        return
    st.subheader("Needs review")
    st.caption(
        "These PDFs look like PRO-LAB results, but MTAR could not read them confidently, "
        "so it did not create anything. Decide what each one is."
    )
    job_options = {job["id"]: f"{job.get('client_name') or 'New Assessment'} · {job.get('address') or 'no address'}" for job in jobs}
    for item in items:
        with st.container(border=True):
            left, right = st.columns([3, 2])
            with left:
                st.markdown(f"**{item['filename']}**")
                st.caption(
                    f"Report #{item['report_number'] or 'unknown'} · {item['project_name'] or 'customer unknown'} · "
                    f"from {item['sender'] or 'unknown sender'}"
                )
                st.caption(f"Why: {item['reason']}")
                content = svc.inbox_pdf(item)
                if content:
                    st.download_button(
                        "Download PDF", content, file_name=item["filename"], mime="application/pdf",
                        key=f"inbox_dl_{item['message_id']}",
                    )
            with right:
                target = st.selectbox(
                    "Match to assessment",
                    list(job_options),
                    format_func=job_options.get,
                    index=None,
                    key=f"inbox_target_{item['message_id']}",
                )
                c1, c2, c3 = st.columns(3)
                if c1.button("Match", key=f"inbox_match_{item['message_id']}", disabled=not target):
                    job = run_action(svc.attach_inbox_item_to_job, item["message_id"], target, success="Lab PDF attached.")
                    if job:
                        open_assessment(job["id"])
                    st.rerun()
                if c2.button("Create", key=f"inbox_create_{item['message_id']}"):
                    job = run_action(svc.create_job_from_inbox_item, item["message_id"], success="Assessment created from lab PDF.")
                    if job:
                        open_assessment(job["id"])
                    st.rerun()
                if c3.button("Ignore", key=f"inbox_ignore_{item['message_id']}"):
                    svc.ignore_inbox_item(item["message_id"])
                    st.rerun()


def render_dashboard() -> None:
    st.title("Lab Inbox")
    show_flash()

    summaries = svc.store.list(limit=200)
    jobs = [job for summary in summaries if (job := svc.store.get(summary.id))]

    render_lab_inbox(jobs)

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

    active = [job for job in jobs if job.get("status") != "closed"]
    c1, c2 = st.columns(2)
    c1.metric("Active assessments", len(active))
    c2.metric("Total", len(jobs))

    if not jobs:
        st.info("No assessments yet. Start one with **New Assessment**, or let a PRO-LAB email create one.")
        return

    for job in jobs:
        with st.container(border=True):
            left, middle, right = st.columns([4, 3, 1])
            with left:
                st.markdown(f"**{job.get('client_name') or 'New Assessment'}**")
                address = ", ".join(p for p in [job.get("address"), job.get("city")] if p)
                st.caption(address or "Property address not entered")
            with middle:
                st.markdown(f"<span class='mtar-pill'>{svc.status_label(job.get('status'))}</span>", unsafe_allow_html=True)
                st.caption(next_step(job))
            with right:
                if st.button("Open", key=f"open_{job['id']}", width="stretch"):
                    open_assessment(job["id"])
                    st.rerun()


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

        s1, s2 = st.columns([1, 3])
        sampling_time = s1.time_input("Sampling time", as_time(job.get("sampling_time")), step=300)
        with s2:
            st.markdown("Weather (for the COC)")
            w = st.columns(4)
            fog = w[0].checkbox("Fog", bool(job.get("weather_fog")))
            rain = w[1].checkbox("Rain", bool(job.get("weather_rain")))
            snow = w[2].checkbox("Snow", bool(job.get("weather_snow")))
            wind = w[3].checkbox("Wind", bool(job.get("weather_wind")))

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
                    "sampling_time": sampling_time.strftime("%H:%M") if sampling_time else "",
                    "weather_fog": fog,
                    "weather_rain": rain,
                    "weather_snow": snow,
                    "weather_wind": wind,
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
                moisture = st.text_area("Moisture assessment (inspector)", area.get("moisture_notes", ""))
                thermal = st.text_area("Thermal imaging notes", area.get("thermal_notes", ""))
                if st.form_submit_button("Save area", type="primary"):
                    area.update(
                        {
                            "name": name.strip(),
                            "finding": finding,
                            "description": description,
                            "moisture_notes": moisture,
                            "thermal_notes": thermal,
                        }
                    )
                    svc.save_job(job)
                    flash("success", f"Saved {name or 'area'}.")
                    st.rerun()


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
                serial = c[1].text_input("Serial number", sample.get("serial_number") or sample.get("lab_serial_number", ""))
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
                    sample.update({"name": name.strip(), "serial_number": serial.strip(), "sample_type_code": code.strip().upper()})
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


def _photo_grid(job: dict, photos: list[dict], *, with_kind: bool = False) -> None:
    columns = st.columns(3)
    for i, photo in enumerate(photos):
        with columns[i % 3]:
            content = svc.photo_bytes(job, photo)
            if content:
                st.image(content, width="stretch")
            caption_key = f"caption_{photo['id']}"
            st.text_input(
                "Caption", photo.get("caption", ""), key=caption_key,
                on_change=_update_photo, args=(job["id"], photo["id"], "caption", caption_key),
            )
            if with_kind:
                kind_key = f"kind_{photo['id']}"
                kinds = list(svc.PHOTO_KINDS)
                st.selectbox(
                    "Photo type", kinds, index=kinds.index(photo.get("kind", "inspection")),
                    format_func=svc.PHOTO_KINDS.get, key=kind_key,
                    on_change=_update_photo, args=(job["id"], photo["id"], "kind", kind_key),
                )
            if st.button("Delete", key=f"del_{photo['id']}", icon=":material/delete:"):
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
            st.caption(
                f"Linked to [this Drive folder]({svc.folder_link(folder_id)}). New photos are imported "
                f"automatically every {svc.GMAIL_POLL_SECONDS // 60} minute(s). Subfolders decide placement: "
                "Property, Outdoor, RH, and one folder per area (with optional Sampling and Thermal inside). "
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
                st.rerun()
        else:
            st.caption(
                "Paste the link to this assessment's Drive folder"
                + (", or name a folder in the MTAR Photos folder after the client or address and it links itself." if svc.DRIVE_PHOTOS_FOLDER else ".")
            )
            with st.form(f"drive_link_{job['id']}", clear_on_submit=True):
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
        columns = st.columns(3)
        for i, photo in enumerate(photos):
            with columns[i % 3]:
                content = svc.photo_bytes(job, photo)
                if content:
                    st.image(content, width="stretch")
                if photo.get("drive_path"):
                    st.caption(f"From {photo['drive_path']}/")
                choice = st.selectbox("Goes in", list(targets), format_func=targets.get, key=f"assign_{photo['id']}")
                b1, b2 = st.columns(2)
                if b1.button("Move", key=f"move_{photo['id']}", type="primary", width="stretch"):
                    role, _, area_id = choice.partition(":")
                    run_action(svc.assign_photo, job, photo["id"], role, area_id or None)
                    st.rerun()
                if b2.button("Delete", key=f"del_{photo['id']}", icon=":material/delete:", width="stretch"):
                    svc.delete_photo(job, photo["id"])
                    st.rerun()


def render_photos(job: dict) -> None:
    photos = job.get("photos", [])
    st.caption("Photos are placed in the report by role. Area photos appear under their area in upload order.")
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
        _photo_grid(job, [p for p in photos if p.get("role") == "outdoor"])
        _photo_upload_form(job, f"up_outdoor_{job['id']}", "Outdoor control photos", "outdoor", kind="sampling")
    with c2, st.container(border=True):
        st.markdown("**Environmental / RH meter**")
        _photo_grid(job, [p for p in photos if p.get("role") == "environment"])
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


def _pdf_preview(content: bytes, key: str) -> None:
    with fitz.open(stream=content, filetype="pdf") as doc:
        st.caption(f"{doc.page_count} pages")
        columns = st.columns(3)
        for i, page in enumerate(doc):
            columns[i % 3].image(page.get_pixmap(dpi=60).tobytes("png"), caption=f"Page {i + 1}", width="stretch")


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
        with st.spinner("Rendering final PDF with the PRO-LAB certificate…"):
            run_action(svc.generate_final_pdf, job, success="Final customer PDF generated with the PRO-LAB certificate attached.")
        st.rerun()

    st.markdown("#### Latest files")
    latest = [
        ("latest_final_pdf_filename", "Final PDF (with PRO-LAB certificate)"),
        ("latest_final_filename", "Final DOCX"),
        ("latest_pdf_draft_filename", "Draft PDF"),
        ("latest_draft_filename", "Draft DOCX"),
    ]
    present = [(field, label) for field, label in latest if job.get(field)]
    if not present:
        st.caption("Nothing generated yet.")
    for field, label in present:
        download_stored(job, "reports", job[field], f"{label}: {job[field]}", f"dl_{field}_{job['id']}")

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
    show_flash()

    if job.get("automatic_draft_error"):
        st.warning(f"The automatic draft could not be generated: {job['automatic_draft_error']}")

    status = job.get("status", "draft")
    allowed = [status] + sorted(ALLOWED_TRANSITIONS.get(status, set()))
    s1, s2 = st.columns([2, 5])
    new_status = s1.selectbox("Workflow status", allowed, format_func=svc.status_label, key=f"status_{job_id}_{status}")
    if new_status != status:
        try:
            svc.save_job(svc.transition_job(job, new_status))
            flash("success", f"Status changed to {svc.status_label(new_status)}.")
        except ValueError as exc:
            flash("error", str(exc))
        st.rerun()
    s2.markdown(f"<br><span class='mtar-muted'>{next_step(job)}</span>", unsafe_allow_html=True)

    tabs = st.tabs(["Overview", "Inspection Areas", "Samples", "Photos", "Lab Results", "Report", "Documents"])
    with tabs[0]:
        render_overview(job)
    with tabs[1]:
        render_areas(job)
    with tabs[2]:
        render_samples(job)
    with tabs[3]:
        render_photos(job)
    with tabs[4]:
        render_lab(job)
    with tabs[5]:
        render_report(job)
    with tabs[6]:
        render_documents(job)


render_sidebar()
current = st.query_params.get("assessment")
if current:
    render_assessment(current)
else:
    render_dashboard()
