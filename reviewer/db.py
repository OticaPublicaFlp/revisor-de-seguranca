import functools
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending | sent | dropped | empty
    findings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
    key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    fingerprint TEXT PRIMARY KEY,
    first_run INTEGER NOT NULL,
    severity TEXT NOT NULL,
    file TEXT NOT NULL,
    title TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(finding: dict) -> str:
    # A linha é excluída de propósito: ela muda a cada commit e quebraria a deduplicação.
    key = f"{finding['file']}|{' '.join(finding['title'].lower().split())}"
    return hashlib.sha1(key.encode()).hexdigest()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Store:
    """Acessado pelo pipeline e pelo handler de callbacks (threads distintas), por isso o lock."""

    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @_locked
    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @_locked
    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @_locked
    def filter_new(self, findings: list[dict]) -> list[dict]:
        """Remove achados já reportados em execuções anteriores."""
        fresh = []
        for f in findings:
            exists = self.conn.execute(
                "SELECT 1 FROM findings WHERE fingerprint = ?", (fingerprint(f),)
            ).fetchone()
            if not exists:
                fresh.append(f)
        return fresh

    @_locked
    def create_run(self, base: str, head: str, findings: list[dict]) -> int:
        status = "pending" if findings else "empty"
        cur = self.conn.execute(
            "INSERT INTO runs (base_sha, head_sha, created_at, status, findings_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (base, head, now_iso(), status, json.dumps(findings, ensure_ascii=False)),
        )
        run_id = cur.lastrowid
        for f in findings:
            self.conn.execute(
                "INSERT OR IGNORE INTO findings (fingerprint, first_run, severity, file, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (fingerprint(f), run_id, f["severity"], f["file"], f["title"]),
            )
        return run_id

    @_locked
    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    @_locked
    def transition(self, run_id: int, new_status: str) -> bool:
        """pending -> sent|dropped, atômico. False se já foi decidido (clique duplo)."""
        cur = self.conn.execute(
            "UPDATE runs SET status = ? WHERE id = ? AND status = 'pending'",
            (new_status, run_id),
        )
        return cur.rowcount == 1

    @_locked
    def revert_to_pending(self, run_id: int) -> None:
        self.conn.execute("UPDATE runs SET status = 'pending' WHERE id = ?", (run_id,))

    @_locked
    def get_units(self) -> dict[str, tuple[str, str]]:
        rows = self.conn.execute("SELECT key, fingerprint, reviewed_at FROM units").fetchall()
        return {r["key"]: (r["fingerprint"], r["reviewed_at"]) for r in rows}

    @_locked
    def mark_unit(self, key: str, fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO units (key, fingerprint, reviewed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fingerprint = excluded.fingerprint, reviewed_at = excluded.reviewed_at",
            (key, fingerprint, now_iso()),
        )
