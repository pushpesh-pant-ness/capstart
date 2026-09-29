"""SQLite-backed audit log: one row per incident, full lifecycle in one place."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT,
    alertname TEXT,
    incident_type TEXT,
    namespace TEXT,
    resource_name TEXT,
    severity TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_alert TEXT,
    context_snapshot TEXT,
    diagnosis_text TEXT,
    remediation_plan TEXT,
    decision_by TEXT,
    decision_at TEXT,
    execution_result TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.audit_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(settings.audit_db_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def create_incident(
    *,
    fingerprint: str,
    alertname: str,
    incident_type: str,
    namespace: str,
    resource_name: str,
    severity: str,
    raw_alert: dict[str, Any],
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO incidents
               (fingerprint, alertname, incident_type, namespace, resource_name, severity,
                status, raw_alert, received_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                fingerprint,
                alertname,
                incident_type,
                namespace,
                resource_name,
                severity,
                json.dumps(raw_alert, default=str),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_incident(incident_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values: list[Any] = []
    for value in fields.values():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        values.append(value)
    values.append(incident_id)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE incidents SET {columns} WHERE id = ?", values)


def get_incident(incident_id: int) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None


def list_incidents(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def find_active_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Used for de-duplication: don't open a second incident while one is still pending."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return dict(row) if row else None
