"""Durable permission store backed by SQLite.

Replaces the in-memory ``pending_permissions`` dict with a persistent state
machine that survives server restarts.  Each permission transitions through a
well-defined lifecycle::

    pending  ─┬─▶  approved   (terminal)
              ├─▶  denied     (terminal)
              ├─▶  cancelled  (terminal, explicit operator action)
              ├─▶  superseded (terminal, new prompt replaces old)
              └─▶  delivery_failed ─▶ pending  (re-enqueue)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class PermissionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    DELIVERY_FAILED = "delivery_failed"

# Valid transitions: from -> set of allowed destinations
_TRANSITIONS: dict[PermissionStatus, set[PermissionStatus]] = {
    PermissionStatus.PENDING: {
        PermissionStatus.APPROVED,
        PermissionStatus.DENIED,
        PermissionStatus.CANCELLED,
        PermissionStatus.SUPERSEDED,
        PermissionStatus.DELIVERY_FAILED,
    },
    PermissionStatus.DELIVERY_FAILED: {
        PermissionStatus.PENDING,  # re-enqueue
    },
    # Terminal states — no outgoing transitions
    PermissionStatus.APPROVED: set(),
    PermissionStatus.DENIED: set(),
    PermissionStatus.CANCELLED: set(),
    PermissionStatus.SUPERSEDED: set(),
}

TERMINAL_STATUSES = frozenset({
    PermissionStatus.APPROVED,
    PermissionStatus.DENIED,
    PermissionStatus.CANCELLED,
    PermissionStatus.SUPERSEDED,
})


def is_valid_transition(from_status: PermissionStatus, to_status: PermissionStatus) -> bool:
    return to_status in _TRANSITIONS.get(from_status, set())


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permissions (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    action          TEXT NOT NULL DEFAULT '',
    options         TEXT NOT NULL DEFAULT '[]',
    raw_block       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    key             TEXT NOT NULL DEFAULT '',
    chosen_label    TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    policy_decision TEXT NOT NULL DEFAULT 'ask_human',
    matched_rule    TEXT,
    source_kind     TEXT NOT NULL DEFAULT 'terminal_parse',
    request_id      TEXT NOT NULL DEFAULT '',
    tool_name       TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    input_preview   TEXT NOT NULL DEFAULT '',

    -- Delivery acks per channel (timestamp or NULL)
    displayed_at_ui       REAL,
    displayed_at_telegram REAL,
    displayed_at_terminal REAL,

    -- Resolution audit
    resolved_at     REAL,
    resolved_by     TEXT,
    resolved_via    TEXT,

    -- Telegram integration
    telegram_message_id INTEGER,

    -- Auto-allow pattern (set when "always allow" is used)
    auto_allow_pattern TEXT
);

CREATE INDEX IF NOT EXISTS idx_permissions_status ON permissions(status);
CREATE INDEX IF NOT EXISTS idx_permissions_agent  ON permissions(agent);
CREATE UNIQUE INDEX IF NOT EXISTS idx_permissions_request_id
    ON permissions(request_id) WHERE request_id != '';
"""


class PermissionStore:
    """Thread-safe, SQLite-backed permission store with state machine enforcement."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path("data/permissions.db")
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # Initialize DB
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        log.info("PermissionStore initialized at %s", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # -- Serialization helpers -----------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a sqlite3.Row to a plain dict, deserializing JSON fields."""
        d = dict(row)
        # options is stored as JSON text
        try:
            d["options"] = json.loads(d.get("options") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["options"] = []
        return d

    @staticmethod
    def _serialize_options(options: list[dict] | None) -> str:
        if not options:
            return "[]"
        return json.dumps(options)

    # -- CRUD ----------------------------------------------------------------

    def create(self, perm: dict[str, Any]) -> dict[str, Any]:
        """Insert a new permission record. Returns the stored dict.

        If ``request_id`` is non-empty and a pending permission with the same
        ``request_id`` already exists, the existing record is returned instead
        of creating a duplicate (idempotent retry / reconnect safety).
        """
        perm_id = perm.get("id") or str(uuid.uuid4())[:8]
        now = perm.get("created_at") or time.time()
        request_id = perm.get("request_id", "").strip()

        with self._lock:
            conn = self._connect()
            try:
                # Deduplicate: if a pending permission with the same request_id
                # exists, return it instead of creating a new row.
                if request_id:
                    existing = conn.execute(
                        "SELECT * FROM permissions WHERE request_id = ? AND status = ?",
                        (request_id, PermissionStatus.PENDING.value),
                    ).fetchone()
                    if existing:
                        log.info(
                            "Permission create deduped: request_id=%s → existing id=%s",
                            request_id, existing["id"],
                        )
                        return self._row_to_dict(existing)

                conn.execute(
                    """INSERT INTO permissions (
                        id, agent, action, options, raw_block, status, key,
                        chosen_label, created_at, policy_decision, matched_rule,
                        source_kind, request_id, tool_name, description,
                        input_preview, telegram_message_id, auto_allow_pattern,
                        displayed_at_terminal
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?
                    )""",
                    (
                        perm_id,
                        perm.get("agent", ""),
                        perm.get("action", ""),
                        self._serialize_options(perm.get("options")),
                        perm.get("raw_block", ""),
                        perm.get("status", "pending"),
                        perm.get("key", ""),
                        perm.get("chosen_label", ""),
                        now,
                        perm.get("policy_decision", "ask_human"),
                        perm.get("matched_rule"),
                        perm.get("source_kind", "terminal_parse"),
                        request_id,
                        perm.get("tool_name", ""),
                        perm.get("description", ""),
                        perm.get("input_preview", ""),
                        perm.get("telegram_message_id"),
                        perm.get("auto_allow_pattern"),
                        # Terminal-sourced permissions are displayed at creation
                        now if perm.get("source_kind") == "terminal_parse" else None,
                    ),
                )
                conn.commit()

                row = conn.execute(
                    "SELECT * FROM permissions WHERE id = ?", (perm_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else perm
            finally:
                conn.close()

    def get(self, perm_id: str) -> dict[str, Any] | None:
        """Fetch a single permission by ID."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM permissions WHERE id = ?", (perm_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_pending(self) -> list[dict[str, Any]]:
        """Return all permissions with status 'pending'."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM permissions WHERE status = ? ORDER BY created_at ASC",
                (PermissionStatus.PENDING.value,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent permissions regardless of status."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM permissions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def transition(
        self,
        perm_id: str,
        to_status: PermissionStatus,
        *,
        key: str = "",
        chosen_label: str = "",
        resolved_by: str = "",
        resolved_via: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """Attempt a state transition.  Returns (updated_perm, error_msg).

        On success error_msg is empty.  On failure updated_perm is None.
        First-writer-wins: if the permission is already resolved, the
        second caller gets an error (idempotent from the caller's perspective).
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM permissions WHERE id = ?", (perm_id,)
                ).fetchone()
                if not row:
                    return None, "not found"

                current = PermissionStatus(row["status"])
                if not is_valid_transition(current, to_status):
                    return self._row_to_dict(row), f"already {current.value}"

                now = time.time()
                updates: dict[str, Any] = {"status": to_status.value}
                if key:
                    updates["key"] = key
                if chosen_label:
                    updates["chosen_label"] = chosen_label
                if to_status in TERMINAL_STATUSES:
                    updates["resolved_at"] = now
                    if resolved_by:
                        updates["resolved_by"] = resolved_by
                    if resolved_via:
                        updates["resolved_via"] = resolved_via

                set_clause = ", ".join(f"{k} = ?" for k in updates)
                values = list(updates.values()) + [perm_id]
                conn.execute(
                    f"UPDATE permissions SET {set_clause} WHERE id = ?",
                    values,
                )
                conn.commit()

                row = conn.execute(
                    "SELECT * FROM permissions WHERE id = ?", (perm_id,)
                ).fetchone()
                if row:
                    return self._row_to_dict(row), ""
                return None, "read-back failed"
            finally:
                conn.close()

    def update_field(self, perm_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update arbitrary fields on a permission (no state machine check).

        Use for delivery acks, telegram_message_id, auto_allow_pattern, etc.
        """
        if not fields:
            return self.get(perm_id)

        # Serialize options if present
        if "options" in fields:
            fields["options"] = self._serialize_options(fields["options"])

        with self._lock:
            conn = self._connect()
            try:
                set_clause = ", ".join(f"{k} = ?" for k in fields)
                values = list(fields.values()) + [perm_id]
                conn.execute(
                    f"UPDATE permissions SET {set_clause} WHERE id = ?",
                    values,
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM permissions WHERE id = ?", (perm_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    def cancel_all_pending(self, agent: str | None = None) -> list[dict[str, Any]]:
        """Cancel all pending permissions, optionally filtered by agent.

        Routes each cancellation through ``transition()`` so audit fields
        (resolved_at, resolved_by, resolved_via) are set correctly.

        Returns list of cancelled permission dicts (for broadcast).
        """
        # Collect IDs first, then transition each individually
        pending = self.get_pending()
        if agent:
            pending = [p for p in pending if p.get("agent") == agent]

        cancelled = []
        for perm in pending:
            updated, error = self.transition(
                perm["id"],
                PermissionStatus.CANCELLED,
                resolved_by="system",
                resolved_via="system",
            )
            if updated and not error:
                cancelled.append(updated)
        return cancelled

    def supersede(self, perm_id: str) -> dict[str, Any] | None:
        """Mark a specific permission as superseded."""
        perm, error = self.transition(
            perm_id,
            PermissionStatus.SUPERSEDED,
            resolved_by="system",
            resolved_via="superseded",
        )
        return perm
