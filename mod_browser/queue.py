"""The request queue: what friends asked for, and where each ask stands.

States: `pending -> approved -> queued`, with a branch to `denied` (with a
reason). Applying a queued batch is the next ticket's job; this module owns
only the order of states and the record.

Stored in SQLite (standard library) under the data directory, so the queue
survives a restart. Every change takes the next `seq`, so a poller (PVE, for
Discord) can ask for "everything since seq N" and never miss a change.

A request keeps a snapshot of what the resolver said when it was made: the
mod, its version, and the dependencies that would come with it. The snapshot
is data about a request, not a promise: the intake re-resolves on approval.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Still the owner's to decide on: can be approved, denied, overridden or removed.
OPEN_STATES = ("pending", "approved", "queued")
#: Being applied: nothing changes it from outside until the applier finishes.
BUSY_STATES = ("testing", "applying")
#: Everything not yet finished, for "is this mod already asked for".
ACTIVE_STATES = (*OPEN_STATES, *BUSY_STATES)
FINISHED_STATES = ("applied", "test-failed", "rolled-back", "denied")
STATES = (*ACTIVE_STATES, *FINISHED_STATES)
_ACTIVE_SQL = "(" + ",".join(f"'{s}'" for s in ACTIVE_STATES) + ")"


class InvalidTransition(Exception):
    """The request is not in a state that allows this change."""


class NoSuchRequest(Exception):
    pass


@dataclass(frozen=True)
class Request:
    id: int
    seq: int
    requester: str
    owner_added: bool
    source: str
    project_id: str
    version_id: str
    mod_name: str
    version_number: str
    channel: str
    side: str
    added: list[dict[str, Any]]
    state: str
    reason: str | None
    note: str | None
    created_at: str
    updated_at: str
    batch: int | None = None

    def public(self) -> dict[str, Any]:
        """What the public page may show: no version ids, no notes."""
        return {
            "id": self.id,
            "requester": self.requester,
            "ownerAdded": self.owner_added,
            "mod": {"name": self.mod_name, "source": self.source, "projectId": self.project_id,
                    "versionNumber": self.version_number, "channel": self.channel, "side": self.side},
            "dependencies": [a["name"] for a in self.added[1:]],
            "state": self.state,
            "reason": self.reason,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    def admin(self) -> dict[str, Any]:
        return {**self.public(), "seq": self.seq, "note": self.note, "versionId": self.version_id,
                "added": self.added, "batch": self.batch}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER NOT NULL,
  requester TEXT NOT NULL,
  owner_added INTEGER NOT NULL,
  source TEXT NOT NULL,
  project_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  mod_name TEXT NOT NULL,
  version_number TEXT NOT NULL,
  channel TEXT NOT NULL,
  side TEXT NOT NULL,
  added TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  batch INTEGER
);
CREATE INDEX IF NOT EXISTS requests_seq ON requests (seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Queue:
    def __init__(self, path: Path | str, clock: Callable[[], str] = _now):
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        if "batch" not in {r["name"] for r in self._db.execute("PRAGMA table_info(requests)")}:
            self._db.execute("ALTER TABLE requests ADD COLUMN batch INTEGER")  # a queue made before the applier

    # ---- reads ----
    def _row(self, row: sqlite3.Row) -> Request:
        return Request(id=row["id"], seq=row["seq"], requester=row["requester"], owner_added=bool(row["owner_added"]),
                       source=row["source"], project_id=row["project_id"], version_id=row["version_id"],
                       mod_name=row["mod_name"], version_number=row["version_number"], channel=row["channel"],
                       side=row["side"], added=json.loads(row["added"]), state=row["state"], reason=row["reason"],
                       note=row["note"], created_at=row["created_at"], updated_at=row["updated_at"], batch=row["batch"])

    def get(self, request_id: int) -> Request:
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NoSuchRequest(str(request_id))
        return self._row(row)

    def all(self, since: int = 0) -> list[Request]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM requests WHERE seq > ? ORDER BY id", (since,)).fetchall()
        return [self._row(r) for r in rows]

    def open_for(self, requester: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM requests WHERE lower(requester) = lower(?) AND state IN " + _ACTIVE_SQL,
                (requester,)).fetchone()[0]

    def open_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM requests WHERE state IN " + _ACTIVE_SQL).fetchone()[0]

    def open_for_project(self, source: str, project_id: str) -> Request | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM requests WHERE source = ? AND project_id = ? AND state IN " + _ACTIVE_SQL + " ORDER BY id LIMIT 1",
                (source, project_id)).fetchone()
        return self._row(row) if row else None

    # ---- writes ----
    def _next_seq(self) -> int:
        return (self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM requests").fetchone()[0]) + 1

    def create(self, requester: str, owner_added: bool, snapshot: dict[str, Any]) -> Request:
        """`snapshot` is the resolver's verdict for the version asked for."""
        added = snapshot["added"]
        with self._lock:
            now = self._clock()
            cur = self._db.execute(
                "INSERT INTO requests (seq, requester, owner_added, source, project_id, version_id, mod_name, version_number,"
                " channel, side, added, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._next_seq(), requester, int(owner_added), added[0]["source"], added[0]["projectId"],
                 added[0]["versionId"], added[0]["name"], added[0]["versionNumber"], added[0]["channel"],
                 snapshot.get("side", "unknown"), json.dumps(added), "pending", now, now))
            return self.get(cur.lastrowid)

    def _change(self, request_id: int, allowed: tuple[str, ...], state: str, **fields: Any) -> Request:
        with self._lock:
            current = self.get(request_id)
            if current.state not in allowed:
                raise InvalidTransition(f"request {request_id} is {current.state}, not {' or '.join(allowed)}")
            sets = {"state": state, "seq": self._next_seq(), "updated_at": self._clock(), **fields}
            cols = ", ".join(f"{k} = ?" for k in sets)
            self._db.execute(f"UPDATE requests SET {cols} WHERE id = ?", (*sets.values(), request_id))
            return self.get(request_id)

    def approve(self, request_id: int) -> Request:
        return self._change(request_id, ("pending",), "approved", note=None)

    def queue(self, request_id: int) -> Request:
        return self._change(request_id, ("approved",), "queued", note=None)

    def hold(self, request_id: int, note: str) -> Request:
        """An approved request that cannot be queued yet, and why."""
        return self._change(request_id, ("approved", "queued"), "approved", note=note)

    def deny(self, request_id: int, reason: str) -> Request:
        return self._change(request_id, OPEN_STATES, "denied", reason=reason)

    def override(self, request_id: int, snapshot: dict[str, Any]) -> Request:
        """Swap the version (and the dependency set that goes with it) of an open request."""
        with self._lock:
            current = self.get(request_id)
            added = snapshot["added"]
            return self._change(
                request_id, OPEN_STATES, current.state if current.state != "queued" else "approved",
                version_id=added[0]["versionId"], version_number=added[0]["versionNumber"],
                channel=added[0]["channel"], side=snapshot.get("side", current.side),
                added=json.dumps(added), note=None)

    # ---- the applier's side ----
    def queued(self) -> list[Request]:
        """Requests waiting to be applied, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM requests WHERE state = 'queued' ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def next_batch_id(self) -> int:
        with self._lock:
            return (self._db.execute("SELECT COALESCE(MAX(batch), 0) FROM requests").fetchone()[0]) + 1

    def in_flight(self) -> list[Request]:
        """Requests a restart interrupted: testing or applying, with no applier running them."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM requests WHERE state IN ('testing','applying') ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def advance(self, ids: list[int], allowed: tuple[str, ...], state: str, *, batch: int | None = None,
                reason: str | None = None) -> list[Request]:
        """Move a whole batch to `state`, or none of it if any member is not in an allowed state."""
        with self._lock:
            current = [self.get(i) for i in ids]
            for r in current:
                if r.state not in allowed:
                    raise InvalidTransition(f"request {r.id} is {r.state}, not {' or '.join(allowed)}")
            self._db.execute("BEGIN")
            try:
                for r in current:
                    sets = {"state": state, "seq": self._next_seq(), "updated_at": self._clock()}
                    if batch is not None:
                        sets["batch"] = batch
                    if reason is not None:
                        sets["reason"] = reason
                    cols = ", ".join(f"{k} = ?" for k in sets)
                    self._db.execute(f"UPDATE requests SET {cols} WHERE id = ?", (*sets.values(), r.id))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return [self.get(i) for i in ids]
