"""PostgreSQL-backed audit log: one row per incident (see agent/k8s/postgres.yaml
for the Postgres service this connects to), plus one row per pipeline step
(every LLM prompt/response and Kubernetes API call - see logging_utils.log_step)
so both resolved incidents and the evidence/tool-call trail behind them survive
an agent pod restart/redeploy, unlike the old SQLite-on-emptyDir file.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg2
import psycopg2.extras

from .config import settings

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id SERIAL PRIMARY KEY,
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
    confidence_score REAL,
    computed_severity TEXT,
    escalation_reason TEXT,
    decision_by TEXT,
    decision_at TEXT,
    execution_result TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_steps (
    id SERIAL PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    direction TEXT NOT NULL,
    note TEXT,
    payload TEXT,
    step_ms INTEGER,
    elapsed_ms INTEGER,
    "timestamp" TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incident_steps_incident_id ON incident_steps (incident_id);
"""

# Columns added after the original schema - guarded so an agent DB created by
# an older version of this app (no migration framework here, it's a demo)
# picks them up instead of failing every UPDATE with 'column does not exist'.
_ADDED_COLUMNS = {
    "confidence_score": "REAL",
    "computed_severity": "TEXT",
    "escalation_reason": "TEXT",
    "agent_evidence": "TEXT",
    "router_decision": "TEXT",
    "router_rationale": "TEXT",
    "reflections": "TEXT",
}


@contextmanager
def _connect() -> Iterator[Any]:
    conn = psycopg2.connect(settings.database_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA)
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'incidents'")
        existing = {row["column_name"] for row in cur.fetchall()}
        for column, col_type in _ADDED_COLUMNS.items():
            if column not in existing:
                cur.execute(f"ALTER TABLE incidents ADD COLUMN {column} {col_type}")


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
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO incidents
               (fingerprint, alertname, incident_type, namespace, resource_name, severity,
                status, raw_alert, received_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
               RETURNING id""",
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
        return int(cur.fetchone()["id"])


def update_incident(incident_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    columns = ", ".join(f"{k} = %s" for k in fields)
    values: list[Any] = []
    for value in fields.values():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        values.append(value)
    values.append(incident_id)
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE incidents SET {columns} WHERE id = %s", values)


def get_incident(incident_id: int) -> dict[str, Any] | None:
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_incidents(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with _lock, _connect() as conn, conn.cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM incidents WHERE status = %s ORDER BY id DESC LIMIT %s",
                (status, limit),
            )
        else:
            cur.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def find_active_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Used for de-duplication: don't open a second incident while one is still pending."""
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM incidents WHERE fingerprint = %s AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


_TERMINAL_STATUSES = {"resolved", "rejected", "executed", "execution_failed"}


def duration_seconds(incident: dict[str, Any]) -> float | None:
    """
    Wall-clock time from alert received to now (still-active incidents) or to
    the last update (terminal incidents) - derived from the durable
    received_at/updated_at columns so it survives an agent restart, unlike the
    in-memory per-step timings in logging_utils.
    """
    received_at = incident.get("received_at")
    if not received_at:
        return None
    end = incident.get("updated_at") if incident.get("status") in _TERMINAL_STATUSES else None
    end_dt = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    start_dt = datetime.fromisoformat(received_at)
    return (end_dt - start_dt).total_seconds()


def find_similar_resolved(incident_type: str, exclude_id: int | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """
    Historical retrieval for the diagnosis prompt: the most recent past
    incidents of the same type that reached a known-good outcome, so Bedrock
    can ground its explanation in concrete precedent instead of generalities.
    """
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM incidents
               WHERE incident_type = %s AND status IN ('resolved', 'executed') AND id != %s
               ORDER BY id DESC LIMIT %s""",
            (incident_type, exclude_id or -1, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def insert_step(incident_id: int, event: dict[str, Any]) -> None:
    """Persists one pipeline step (LLM prompt/response, k8s API call, etc.) -
    see logging_utils.log_step, which is the sole caller."""
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO incident_steps
               (incident_id, step, direction, note, payload, step_ms, elapsed_ms, "timestamp")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                incident_id,
                event["step"],
                event["direction"],
                event.get("note"),
                event.get("payload"),
                event.get("step_ms"),
                event.get("elapsed_ms"),
                event["timestamp"],
            ),
        )


def get_steps(incident_id: int) -> list[dict[str, Any]]:
    """Full LLM/tool-call timeline for one incident, oldest first - see ui/routes.py."""
    with _lock, _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT step, direction, note, payload, step_ms, elapsed_ms, "timestamp" '
            "FROM incident_steps WHERE incident_id = %s ORDER BY id ASC",
            (incident_id,),
        )
        return [dict(r) for r in cur.fetchall()]
