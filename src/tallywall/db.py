"""Local SQLite tally log. Rows not yet synced to Turff form the outbox.

A row is in the outbox while `synced_at IS NULL AND failed_at IS NULL`.
All calls are short and run inline under a lock (one connection, WAL).
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS tally (
  id          INTEGER PRIMARY KEY,
  person_uid  TEXT NOT NULL,
  drink_uid   TEXT NOT NULL,
  amount      INTEGER NOT NULL,      -- +1, or -1 (manual removal, or an undo row when undoes_id is set)
  undoes_id   INTEGER REFERENCES tally(id),
  ts          TEXT NOT NULL,         -- UTC ISO
  device_id   TEXT, device_name TEXT, ip TEXT, user_agent TEXT,
  is_group    INTEGER NOT NULL DEFAULT 0,
  synced_at   TEXT,                  -- NULL = still in outbox (unless failed_at)
  failed_at   TEXT,                  -- set on a permanent Turff error; row leaves the outbox
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS tally_undoes ON tally(undoes_id) WHERE undoes_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tally_outbox ON tally(id) WHERE synced_at IS NULL AND failed_at IS NULL;
"""

OUTBOX = "synced_at IS NULL AND failed_at IS NULL"
UNDO_WINDOW = timedelta(minutes=10)
CANCELLED = "cancelled locally"


class TallyNotFound(LookupError):
    pass


class UndoRejected(ValueError):
    pass


@dataclass(frozen=True)
class Tally:
    id: int
    person_uid: str
    drink_uid: str
    amount: int
    is_group: bool
    undoes_id: int | None
    ts: str
    synced_at: str | None
    failed_at: str | None
    attempts: int
    last_error: str | None

    @property
    def in_outbox(self) -> bool:
        return self.synced_at is None and self.failed_at is None


@dataclass(frozen=True)
class UndoResult:
    id: int  # the undo row
    cancelled: bool  # True: original never reached Turff, both rows dropped locally


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_COLS = (
    "id, person_uid, drink_uid, amount, is_group, undoes_id, ts, synced_at, failed_at, attempts, last_error"
)


def _row(r: tuple) -> Tally:
    return Tally(
        id=r[0], person_uid=r[1], drink_uid=r[2], amount=r[3], is_group=bool(r[4]), undoes_id=r[5],
        ts=r[6], synced_at=r[7], failed_at=r[8], attempts=r[9], last_error=r[10],
    )  # fmt: skip


class TallyStore:
    def __init__(self, path: str | pathlib.Path) -> None:
        if str(path) != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        # Row the sync worker is currently sending; it must not be cancelled locally.
        self.in_flight: int | None = None

    def close(self) -> None:
        self._db.close()

    # -- writes --------------------------------------------------------------

    def add(
        self,
        person_uid: str,
        drink_uid: str,
        *,
        amount: int = 1,
        is_group: bool = False,
        device_id: str | None = None,
        device_name: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> int:
        if amount not in (1, -1):
            raise ValueError(f"amount must be +1 or -1, got {amount!r}")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO tally (person_uid, drink_uid, amount, is_group, ts, device_id, device_name, ip,"
                " user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person_uid, drink_uid, amount, int(is_group), iso(now or utcnow()), device_id, device_name, ip,
                 user_agent),
            )  # fmt: skip
            return int(cur.lastrowid)

    def undo(
        self,
        tally_id: int,
        *,
        device_id: str | None = None,
        device_name: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> UndoResult:
        now = now or utcnow()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                orig = self._get(tally_id)
                if orig is None:
                    raise TallyNotFound(tally_id)
                if orig.amount != 1 or orig.undoes_id is not None:
                    raise UndoRejected("only a +1 tally can be undone")
                if self._db.execute("SELECT 1 FROM tally WHERE undoes_id = ?", (tally_id,)).fetchone():
                    raise UndoRejected("already undone")
                if orig.failed_at is not None:
                    raise UndoRejected("tally never reached Turff")
                if now - datetime.fromisoformat(orig.ts) > UNDO_WINDOW:
                    raise UndoRejected("too old to undo")

                cancel = orig.in_outbox and self.in_flight != tally_id
                ts = iso(now)
                cur = self._db.execute(
                    "INSERT INTO tally (person_uid, drink_uid, amount, is_group, undoes_id, ts, device_id,"
                    " device_name, ip, user_agent, synced_at, last_error) VALUES (?, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (orig.person_uid, orig.drink_uid, int(orig.is_group), tally_id, ts, device_id, device_name,
                     ip, user_agent, ts if cancel else None, CANCELLED if cancel else None),
                )  # fmt: skip
                if cancel:
                    self._db.execute(
                        "UPDATE tally SET synced_at = ?, last_error = ? WHERE id = ?",
                        (ts, CANCELLED, tally_id),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return UndoResult(id=int(cur.lastrowid), cancelled=cancel)

    def mark_synced(self, tally_id: int, *, now: datetime | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tally SET synced_at = ?, attempts = attempts + 1, last_error = NULL WHERE id = ?",
                (iso(now or utcnow()), tally_id),
            )

    def mark_retry(self, tally_id: int, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tally SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:500], tally_id),
            )

    def mark_failed(self, tally_id: int, error: str, *, now: datetime | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tally SET failed_at = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
                (iso(now or utcnow()), error[:500], tally_id),
            )

    # -- reads ---------------------------------------------------------------

    def get(self, tally_id: int) -> Tally | None:
        with self._lock:
            return self._get(tally_id)

    def _get(self, tally_id: int) -> Tally | None:
        r = self._db.execute(f"SELECT {_COLS} FROM tally WHERE id = ?", (tally_id,)).fetchone()
        return _row(r) if r else None

    def pending(self, limit: int = 100) -> list[Tally]:
        """Outbox rows, oldest first."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM tally WHERE {OUTBOX} ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM tally WHERE {OUTBOX}").fetchone()[0])

    def pending_deltas(self) -> dict[tuple[str, str], int]:
        """Net unsynced amount per (person_uid, drink_uid)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT person_uid, drink_uid, SUM(amount) FROM tally WHERE {OUTBOX}"
                " GROUP BY person_uid, drink_uid"
            ).fetchall()
        return {(p, d): int(n) for p, d, n in rows if n}
