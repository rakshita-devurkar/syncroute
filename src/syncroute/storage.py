"""SQLite persistence.

Properties that matter here:

* Only sanitized payloads are written. Every JSON blob passes through the
  redactor again on the way in, so a path that skipped sanitization upstream
  still cannot persist a secret shape.
* Decisions are immutable per run. Changing a threshold produces a new run; it
  never rewrites history.
* ``(run_id, event_id)`` is unique, which makes reprocessing idempotent. A
  Streamlit rerun cannot duplicate a decision or a workflow action.
* Workflow transitions are written inside a transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import (
    RoutingDecision,
    SyncFailureEvent,
    WorkflowPlan,
)
from .sanitize import sanitize_mapping

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    proposed_route TEXT,
    final_route TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_id TEXT,
    policy_id TEXT,
    fallback_reason TEXT,
    mode TEXT NOT NULL,
    system TEXT NOT NULL,
    model_called INTEGER NOT NULL,
    confidence REAL,
    selected_probability REAL,
    top_two_margin REAL,
    model_version TEXT,
    is_fixture INTEGER NOT NULL DEFAULT 0,
    total_latency_ms REAL,
    model_latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    decision_json TEXT NOT NULL,
    UNIQUE (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    approved INTEGER NOT NULL,
    next_attempt_at TEXT,
    plan_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    corrected_route TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    split TEXT NOT NULL,
    mode TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_run ON routing_decisions (run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_event ON routing_decisions (event_id);
CREATE INDEX IF NOT EXISTS idx_feedback_event ON feedback (event_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(model: Any) -> str:
    """Serialize a model to JSON, redacting defensively on the way out."""
    raw = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    cleaned, _ = sanitize_mapping(raw)
    return json.dumps(cleaned, default=str)


class Storage:
    """A thin SQLite wrapper. Safe to construct per request."""

    def __init__(self, path: str | Path = "syncroute.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # ------------------------------------------------------------------ events

    def save_event(self, event: SyncFailureEvent) -> None:
        """Insert or refresh an event. The stored payload is sanitized."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO events (event_id, incident_id, connector_id, provider,
                                    connector_type, occurred_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (
                    event.event_id,
                    event.incident_id,
                    event.connector_id,
                    event.provider,
                    event.connector_type.value,
                    event.occurred_at.isoformat(),
                    _dump(event),
                    _now(),
                ),
            )

    def get_event(self, event_id: str) -> Optional[SyncFailureEvent]:
        row = self._conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return SyncFailureEvent.model_validate_json(row["payload_json"])

    # --------------------------------------------------------------- decisions

    def record_decision(self, decision: RoutingDecision) -> bool:
        """Record a decision. Returns False if this (run, event) already exists.

        Idempotent by construction: a Streamlit rerun that re-submits the same
        event within the same run is a no-op rather than a duplicate row.
        """
        answer = decision.answer
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO routing_decisions (
                    run_id, event_id, incident_id, decided_at, proposed_route, final_route,
                    source, rule_id, policy_id, fallback_reason, mode, system, model_called,
                    confidence, selected_probability, top_two_margin, model_version, is_fixture,
                    total_latency_ms, model_latency_ms, input_tokens, output_tokens, decision_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision.run_id,
                    decision.event_id,
                    decision.incident_id,
                    decision.decided_at.isoformat(),
                    decision.proposed_route.value if decision.proposed_route else None,
                    decision.final_route.value,
                    decision.source.value,
                    decision.rule_id,
                    decision.policy_id,
                    decision.fallback_reason,
                    decision.mode,
                    decision.system,
                    int(decision.model_called),
                    answer.confidence if answer else None,
                    answer.selected_probability if answer else None,
                    answer.top_two_margin if answer else None,
                    answer.model_version if answer else None,
                    int(bool(answer and answer.is_fixture)),
                    decision.total_latency_ms,
                    answer.latency_ms if answer else None,
                    answer.input_tokens if answer else None,
                    answer.output_tokens if answer else None,
                    _dump(decision),
                ),
            )
            return cursor.rowcount > 0

    def get_decision(self, run_id: str, event_id: str) -> Optional[RoutingDecision]:
        row = self._conn.execute(
            "SELECT decision_json FROM routing_decisions WHERE run_id = ? AND event_id = ?",
            (run_id, event_id),
        ).fetchone()
        if row is None:
            return None
        return RoutingDecision.model_validate_json(row["decision_json"])

    def list_decisions(
        self,
        run_id: Optional[str] = None,
        *,
        system: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM routing_decisions"
        clauses, params = [], []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if system:
            clauses.append("system = ?")
            params.append(system)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    # --------------------------------------------------------------- workflows

    def save_workflow(self, run_id: str, event_id: str, plan: WorkflowPlan) -> None:
        """Insert or transition a simulated workflow, transactionally."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (run_id, event_id, route, status, requires_approval,
                                           approved, next_attempt_at, plan_json, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, event_id) DO UPDATE SET
                    status = excluded.status,
                    approved = excluded.approved,
                    next_attempt_at = excluded.next_attempt_at,
                    plan_json = excluded.plan_json,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    event_id,
                    plan.route.value,
                    plan.status.value,
                    int(plan.requires_approval),
                    int(plan.approved),
                    plan.next_attempt_at.isoformat() if plan.next_attempt_at else None,
                    _dump(plan),
                    _now(),
                ),
            )

    def get_workflow(self, run_id: str, event_id: str) -> Optional[WorkflowPlan]:
        row = self._conn.execute(
            "SELECT plan_json FROM workflow_runs WHERE run_id = ? AND event_id = ?",
            (run_id, event_id),
        ).fetchone()
        if row is None:
            return None
        return WorkflowPlan.model_validate_json(row["plan_json"])

    # ---------------------------------------------------------------- feedback

    def add_feedback(
        self, run_id: str, event_id: str, reviewer: str, corrected_route: str, note: str = ""
    ) -> None:
        """Record a reviewer correction.

        Feedback is a record of disagreement. It does not alter a locked
        evaluation label or any decision already written.
        """
        cleaned, _ = sanitize_mapping(note)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feedback (run_id, event_id, reviewer, corrected_route, note, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (run_id, event_id, reviewer, corrected_route, cleaned, _now()),
            )

    def list_feedback(self, event_id: Optional[str] = None) -> list[dict[str, Any]]:
        if event_id:
            rows = self._conn.execute(
                "SELECT * FROM feedback WHERE event_id = ? ORDER BY id DESC", (event_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- evaluation

    def record_evaluation_run(
        self, run_id: str, split: str, mode: str, manifest: dict, metrics: dict
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evaluation_runs
                    (run_id, created_at, split, mode, manifest_json, metrics_json)
                VALUES (?,?,?,?,?,?)
                """,
                (run_id, _now(), split, mode, json.dumps(manifest, default=str),
                 json.dumps(metrics, default=str)),
            )

    def list_evaluation_runs(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        if mode:
            rows = self._conn.execute(
                "SELECT * FROM evaluation_runs WHERE mode = ? ORDER BY created_at DESC", (mode,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM evaluation_runs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
