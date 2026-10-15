"""Durable, credential-free journal for recoverable two-service booking attempts.

This is a compensating workflow, not a distributed transaction. Each intent is
committed before its HTTP request. Operation keys let a later process ask the
server what happened, and guarded compensation only removes its own reservation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

TERMINAL_STATES = frozenset({"matched", "aborted"})
STATES = TERMINAL_STATES | {
    "hotel_pending",
    "hotel_reserved",
    "band_pending",
    "compensating",
    "pair_check",
    "pair_compensating",
}


def client_scope(clients: dict[str, Any]) -> str:
    """Separate accounts and endpoints without storing their tokens or URLs."""
    identity = [
        [
            name,
            str(getattr(clients[name], "base_url", "")),
            str(getattr(clients[name], "token", "")),
        ]
        for name in ("hotel", "band")
    ]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


@dataclass
class Workflow:
    workflow_id: str
    scope: str
    slot_id: int
    state: str
    hotel_operation_id: str
    band_operation_id: str
    hotel_release_id: str
    band_release_id: str = ""
    last_error: str = ""


class WorkflowBusyError(RuntimeError):
    """Another process is changing this journal; retry after it finishes."""


class RecoveryJournal:
    """SQLite journal with a process lock around network-facing workflows.

    Omitting the path keeps direct library calls isolated in memory. The CLI
    passes an explicit file path so a new process can recover interrupted work.
    Each state transition commits independently, even while the process lock is
    held. The lock does not keep a SQLite transaction open across HTTP calls.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).resolve() if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path) if self.path is not None else ":memory:",
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._mutex = threading.RLock()
        self._depth = 0
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    slot_id INTEGER NOT NULL CHECK(slot_id > 0),
                    state TEXT NOT NULL,
                    hotel_operation_id TEXT NOT NULL,
                    band_operation_id TEXT NOT NULL,
                    hotel_release_id TEXT NOT NULL,
                    band_release_id TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(workflows)")
            }
            if "band_release_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE workflows ADD COLUMN band_release_id TEXT NOT NULL DEFAULT ''"
                )
            for row in self._connection.execute(
                "SELECT workflow_id FROM workflows WHERE band_release_id=''"
            ).fetchall():
                self._connection.execute(
                    "UPDATE workflows SET band_release_id=? WHERE workflow_id=?",
                    (uuid.uuid4().hex, row["workflow_id"]),
                )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize workflows across threads and CLI processes on this host."""
        with self._mutex, ExitStack() as stack:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            handle = None
            try:
                if self.path is not None:
                    handle = stack.enter_context(Path(f"{self.path}.lock").open("a+b"))
                    handle.seek(0, os.SEEK_END)
                    if not handle.tell():
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        raise WorkflowBusyError(
                            "Another planner is using this recovery journal."
                        ) from error
                self._depth = 1
                yield
            finally:
                self._depth = 0
                # ExitStack closes the descriptor and releases the OS lock.

    def create(self, scope: str, slot_id: int) -> Workflow:
        workflow = Workflow(
            uuid.uuid4().hex,
            scope,
            slot_id,
            "hotel_pending",
            uuid.uuid4().hex,
            uuid.uuid4().hex,
            uuid.uuid4().hex,
            uuid.uuid4().hex,
        )
        self.save(workflow, "hotel_reserve_intent")
        return workflow

    def save(self, workflow: Workflow, detail: str = "") -> None:
        if workflow.state not in STATES:
            raise ValueError("Invalid workflow state")
        values = asdict(workflow)
        with self._mutex, self._connection:
            self._connection.execute(
                """INSERT INTO workflows (
                    workflow_id, scope, slot_id, state, hotel_operation_id,
                    band_operation_id, hotel_release_id, band_release_id, last_error
                ) VALUES (
                    :workflow_id, :scope, :slot_id, :state, :hotel_operation_id,
                    :band_operation_id, :hotel_release_id, :band_release_id, :last_error
                ) ON CONFLICT(workflow_id) DO UPDATE SET
                    state=excluded.state, last_error=excluded.last_error,
                    updated_at=CURRENT_TIMESTAMP""",
                values,
            )
            self._connection.execute(
                "INSERT INTO workflow_events(workflow_id,state,detail) VALUES (?,?,?)",
                (workflow.workflow_id, workflow.state, detail),
            )

    def all(self, scope: str | None = None) -> list[Workflow]:
        query = "SELECT * FROM workflows"
        arguments: tuple[str, ...] = ()
        if scope is not None:
            query += " WHERE scope=?"
            arguments = (scope,)
        query += " ORDER BY rowid"
        with self._mutex:
            return [
                Workflow(**{key: row[key] for key in Workflow.__dataclass_fields__})
                for row in self._connection.execute(query, arguments).fetchall()
            ]

    def pending(self, scope: str) -> list[Workflow]:
        return [item for item in self.all(scope) if item.state not in TERMINAL_STATES]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RecoveryJournal:  # noqa: PYI034 - Python 3.10 compatibility
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
