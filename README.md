# MTAR-V2

V2 workspace for Mold Testing and Removal report automation.

## RG-2 review-fixes branch

This branch refines the report review workflow based on real testing.

### Updated workflow

1. Upload the PRO-LAB Certificate of Mold Analysis.
2. MTAR imports supported lab data.
3. Inspection Areas are created manually and remain separate from lab samples.
4. Each indoor or surface sample is manually assigned to an Inspection Area.
5. Indoor RH is blank and required.
6. Visual observations, moisture findings, area findings, and the final professional conclusion are reviewed in Streamlit.
7. Property and area photos are added.
8. The Current Draft is regenerated from the latest review values.
9. The final DOCX is generated only after validation and consultant approval.

### Current fixes

- Lab sample names are visible in Review Report.
- Samples and Inspection Areas are separate entities.
- Inspection Areas can be added, renamed, edited, and removed.
- Samples can be assigned to areas manually.
- Surface samples have a review section when present.
- Client phone/email fields were removed from the report-review UI.
- Indoor RH is required and starts blank.
- Draft generation uses the current selected report outcome, preventing stale conclusion text.
- Visual Observation and Moisture Assessment edits flow into the current draft.
- Property photos use fresh in-memory image streams and are included in regenerated drafts/final reports.
- Air results are shown side by side by sample in Streamlit and in the DOCX.
- The Surface Sample Results section is omitted when no surface samples exist.
- The report introduction only mentions sample types actually present.

### Surface-sample note

Surface sample entities, assignment, PRO-LAB determination review, and report output are implemented. Detailed organism extraction for every possible PRO-LAB surface-report layout still needs validation against a real surface/swab certificate before it should be considered complete.

## Run locally

```powershell
git checkout streamlit-v3
git pull
py -m pip install -r requirements.txt
py -m streamlit run app.py
```


## Automation foundation

The `automation-foundation` branch starts the migration from a session-only Streamlit workflow to a persistent, UI-independent MTAR backend. It intentionally leaves the live Streamlit app on `main` unchanged.

### Added in this foundation

- `workflow.py` centralizes job statuses, allowed workflow transitions, and the final-report validation gate so the Streamlit app and future background workers can share the same rules.
- `job_store.py` adds a SQLite persistence adapter for development/single-instance use while keeping the existing job dictionary model intact. The storage boundary is designed so PostgreSQL can replace SQLite later without rewriting report or lab logic.
- `coc_builder.py` converts a job into a PRO-LAB Chain of Custody payload and fills the known AcroForm fields in the provided blank COC, including company/property data, sampling data, air/surface sample rows, and mold-analysis selections.
- `tests/fixtures/scarlet_parsed_expected.json` captures the key structured results from the real Scarlet PRO-LAB report: outdoor control, Bedroom Closet air sample, and Coat Closet swab with an UNUSUAL determination and growth observed.
- GitHub Actions runs the unit-test suite on this branch and pull requests.

### Persistence note

SQLite is only the first adapter. It is suitable for local development and a single app instance, but the production multi-user target remains PostgreSQL. Application code should use the store interface rather than opening SQLite directly.

### COC note

The supplied PRO-LAB COC was manually verified against `coc_builder.py`. Its visible State/ZIP positions do not match the field numbering implied by the existing default values, so the builder maps values by visible form position. Future template changes should be re-verified before deployment.


### Gmail / PRO-LAB intake

The Streamlit app polls the mailbox that receives PRO-LAB result emails and automatically attaches high-confidence PDF matches to assessments in `Awaiting Lab`. When no assessment matches, it creates the customer, property and assessment from the report and generates the AUTO_DRAFT DOCX and PDF.

Configure these environment variables on the host:

- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`
- `GMAIL_USER_ID` (optional, defaults to `me`)
- `GMAIL_POLL_SECONDS` (optional, defaults to 300 seconds)

The OAuth token only needs the Gmail read-only scope. The intake worker searches recent PDF attachments, parses each PDF with the existing PRO-LAB parser, and compares the result against `Awaiting Lab` jobs.

Automatic matching is conservative. It rewards exact client/property metadata and especially sample serial-number matches. If the best match is weak or too close to another job, MTAR does not attach the report automatically. PDFs that look like PRO-LAB results but fail the report-number/COC-line check go to the **Needs review** list on the Lab Inbox, where the consultant can match them to an assessment, create one, or ignore them. Unrelated PDFs are ignored.

The dashboard also includes a **Check Gmail Now** control for an immediate run. A background thread polls on the `GMAIL_POLL_SECONDS` interval while the Streamlit server is running.


### Railway deployment

The `automation-foundation` branch is prepared for Railway with `railway.json`.

Recommended Railway setup:

1. Create a new Railway project from `sqlmfh/MTAR-V2`.
2. Deploy branch `streamlit-v3` (staging first).
3. Add a persistent Volume to the service. MTAR automatically uses Railway's `RAILWAY_VOLUME_MOUNT_PATH` for the SQLite job database and document storage.
4. Add the environment variables shown in `.env.example`. Keep real Gmail secrets only in Railway Variables.
5. Generate a Railway public domain for the service.

Railway runs `streamlit run app.py --server.port $PORT --server.address 0.0.0.0` and health-checks `/`. The app listens on Railway's injected `PORT`.

The current storage design is suitable for initial testing and a single running MTAR instance. Before multi-instance production use, replace SQLite with PostgreSQL and move binary documents to shared cloud/object storage.


### Streamlit app layout (`streamlit-v3`)

- `app.py` is the Streamlit UI: Lab Inbox dashboard, New Assessment, and one page per assessment with Overview, Inspection Areas, Samples, Photos, Lab Results, Report and Documents tabs.
- `mtar_services.py` holds every workflow action the UI calls (Gmail intake, lab import, duplicate protection, photos, COC, draft and final reports), so the rules can be tested without a browser.
- The final customer PDF is the generated assessment with the original PRO-LAB certificate appended unchanged.
