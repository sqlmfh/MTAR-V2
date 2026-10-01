from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from workflow import JOB_STATUSES, STATUS_DRAFT


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_job_id() -> str:
    return f"job_{uuid4().hex[:12]}"


def _profile_key(value: str) -> str:
    return " ".join(str(value or "").upper().split())


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return {"__mtar_type__": value.__class__.__name__, "value": value.isoformat()}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_object_hook(value: dict):
    marker = value.get("__mtar_type__")
    raw = value.get("value")
    if marker == "date" and isinstance(raw, str):
        return date.fromisoformat(raw)
    if marker == "datetime" and isinstance(raw, str):
        return datetime.fromisoformat(raw)
    return value


def serialize_job(job: dict) -> str:
    return json.dumps(job, default=_json_default, separators=(",", ":"), sort_keys=True)


def deserialize_job(payload: str) -> dict:
    return json.loads(payload, object_hook=_json_object_hook)


def prepare_persistent_job(job: dict, *, job_id: str | None = None) -> dict:
    """Return a copy of a job with persistence/workflow metadata attached.

    This intentionally keeps the existing MTAR dict model intact so the current
    Streamlit report workflow can be migrated incrementally instead of rewritten.
    """
    result = copy.deepcopy(job)
    now = utc_now_iso()
    result.setdefault("id", job_id or new_job_id())
    result.setdefault("status", STATUS_DRAFT)
    result.setdefault("created_at", now)
    result["updated_at"] = now
    result.setdefault("schema_version", 1)
    return result


def new_persistent_job() -> dict:
    from models import new_job_state

    return prepare_persistent_job(new_job_state())


@dataclass(frozen=True)
class JobSummary:
    id: str
    status: str
    client_name: str
    property_address: str
    updated_at: str


class SQLiteJobStore:
    """Small persistence adapter for the automation foundation.

    SQLite is used for development and single-instance deployments. The rest of
    MTAR talks to this class rather than directly to sqlite3, making it possible
    to replace the adapter with PostgreSQL later without changing report/parser
    business logic.
    """

    def __init__(self, path: str | Path = "mtar_jobs.sqlite3") -> None:
        self.path = str(path)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    property_address TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS properties (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    address TEXT NOT NULL,
                    city TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    zip TEXT NOT NULL DEFAULT '',
                    address_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(customer_id) REFERENCES customers(id)
                )
                """
            )

    def ensure_customer(self, name: str) -> dict:
        """Return an existing customer profile or create one by normalized name."""
        display = " ".join(str(name or "").split()).strip()
        if not display:
            raise ValueError("Customer name is required")
        key = _profile_key(display)
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, created_at, updated_at FROM customers WHERE name_key = ?",
                (key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE customers SET name = ?, updated_at = ? WHERE id = ?",
                    (display, now, row["id"]),
                )
                return {
                    "id": row["id"],
                    "name": display,
                    "created_at": row["created_at"],
                    "updated_at": now,
                }

            customer_id = f"customer_{uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO customers (id, name, name_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (customer_id, display, key, now, now),
            )
        return {"id": customer_id, "name": display, "created_at": now, "updated_at": now}

    def ensure_property(
        self,
        customer_id: str,
        address: str,
        city: str = "",
        state: str = "",
        zip_code: str = "",
    ) -> dict:
        """Return an existing property or create one by normalized address + ZIP."""
        address = " ".join(str(address or "").split()).strip()
        if not address:
            raise ValueError("Property address is required")
        city = " ".join(str(city or "").split()).strip()
        state = " ".join(str(state or "").split()).strip()
        zip_code = " ".join(str(zip_code or "").split()).strip()
        key = _profile_key(f"{address}|{city}|{state}|{zip_code}")
        now = utc_now_iso()

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, customer_id, address, city, state, zip, created_at, updated_at
                FROM properties WHERE address_key = ?
                """,
                (key,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE properties
                    SET customer_id = ?, address = ?, city = ?, state = ?, zip = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (customer_id, address, city, state, zip_code, now, row["id"]),
                )
                return {
                    "id": row["id"],
                    "customer_id": customer_id,
                    "address": address,
                    "city": city,
                    "state": state,
                    "zip": zip_code,
                    "created_at": row["created_at"],
                    "updated_at": now,
                }

            property_id = f"property_{uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO properties (
                    id, customer_id, address, city, state, zip, address_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (property_id, customer_id, address, city, state, zip_code, key, now, now),
            )
        return {
            "id": property_id,
            "customer_id": customer_id,
            "address": address,
            "city": city,
            "state": state,
            "zip": zip_code,
            "created_at": now,
            "updated_at": now,
        }

    def save(self, job: dict) -> dict:
        stored = prepare_persistent_job(job, job_id=job.get("id"))
        if stored["status"] not in JOB_STATUSES:
            raise ValueError(f"Unknown job status: {stored['status']}")

        created_at = stored.get("created_at") or utc_now_iso()
        stored["created_at"] = created_at
        payload = serialize_job(stored)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, client_name, property_address,
                    payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    client_name = excluded.client_name,
                    property_address = excluded.property_address,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    stored["id"],
                    stored["status"],
                    stored.get("client_name", ""),
                    stored.get("address", ""),
                    payload,
                    created_at,
                    stored["updated_at"],
                ),
            )
        return stored

    def get(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return deserialize_job(row["payload"]) if row else None

    def list(self, *, statuses: Iterable[str] | None = None, limit: int = 100) -> list[JobSummary]:
        if limit <= 0:
            return []

        params: list[object] = []
        where = ""
        if statuses:
            status_list = list(statuses)
            invalid = [status for status in status_list if status not in JOB_STATUSES]
            if invalid:
                raise ValueError(f"Unknown job status: {invalid[0]}")
            placeholders = ",".join("?" for _ in status_list)
            where = f"WHERE status IN ({placeholders})"
            params.extend(status_list)

        params.append(limit)
        query = f"""
            SELECT id, status, client_name, property_address, updated_at
            FROM jobs
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            JobSummary(
                id=row["id"],
                status=row["status"],
                client_name=row["client_name"],
                property_address=row["property_address"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def delete(self, job_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0
