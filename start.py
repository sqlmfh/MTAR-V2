"""Production entry point (Railway): start the Gmail/Drive checker, then Streamlit.

Starting the checker here, in the same process as the Streamlit server, means
PRO-LAB emails and Drive photos are picked up right after every deploy or
restart, not only once someone opens the app. app.py sees the running checker
and does not start a second one.
"""
from __future__ import annotations

import os
import sys
import threading

import mtar_services as svc

POLLER_NAME = "mtar-gmail-poller"


def main() -> None:
    if os.environ.get("RAILWAY_PROJECT_ID") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        print(
            "WARNING: no Railway volume is attached. Assessments, photos and reports are stored "
            f"in {svc.DB_PATH.parent.resolve()} and will be lost on the next deploy or restart.",
            flush=True,
        )
    else:
        print(f"MTAR data: {svc.DB_PATH} and {svc.DOCUMENT_ROOT}", flush=True)

    if svc.gmail_client.configured() or svc.drive_client.configured():
        threading.Thread(target=svc.gmail_poll_loop, name=POLLER_NAME, daemon=True).start()
        print(f"Gmail/Drive checks every {svc.GMAIL_POLL_SECONDS // 60} minute(s).", flush=True)

    from streamlit.web import cli

    sys.argv = [
        "streamlit", "run", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
        "--server.port", os.environ.get("PORT", "8501"),
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
        "--server.fileWatcherType", "none",
    ]
    sys.exit(cli.main())


if __name__ == "__main__":
    main()
