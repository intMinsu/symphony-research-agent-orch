"""Durable local approvals and execution leases; never auto-steal stale jobs."""
from __future__ import annotations

import json
import os
import sqlite3
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import ConflictError, SRAError
from .models import ACTIVE, RunRecord, Selection, WorkSpec, digest, utcnow

TRANSITIONS = {
    "preparing": {"running", "blocked", "failed", "cancelled"},
    "running": {"verifying", "blocked", "failed", "cancelled"},
    "verifying": {"delivering", "awaiting_review", "blocked", "failed", "cancelled"},
    "delivering": {"awaiting_review", "blocked", "failed", "cancelled"},
    "awaiting_review": {"delivering"},
    "blocked": set(), "failed": set(), "cancelled": set(),
}


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "sra"


class Store:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "state.sqlite3"
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise SRAError(f"Unsupported database schema {version}; use the matching SRA release")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                  repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                  spec_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  spec_sha TEXT NOT NULL, policy_sha TEXT NOT NULL,
                  spec_json TEXT NOT NULL, approved_at TEXT NOT NULL,
                  PRIMARY KEY(repository, issue_number), UNIQUE(repository, spec_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                  state TEXT NOT NULL, created_at TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(repository, issue_number)
                  WHERE state IN ('preparing','running','verifying','delivering');
                PRAGMA user_version=1;
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            db.close()

    def approve(self, repository: str, issue: int, spec: WorkSpec, policy_sha: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE repository=? AND issue_number=? AND state IN "
                          "('preparing','running','verifying','delivering')", (repository, issue)).fetchone():
                raise ConflictError("Cannot change approval while this issue has an active run")
            old = db.execute("SELECT * FROM approvals WHERE repository=? AND issue_number=?",
                             (repository, issue)).fetchone()
            if old and (old["spec_id"] != spec.id or (old["spec_sha"] != digest(spec)
                                                    and spec.revision <= old["revision"])):
                raise ConflictError("Keep the WorkSpec id and increment revision when changing its content")
            try:
                db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(repository,issue_number) "
                           "DO UPDATE SET revision=excluded.revision,spec_sha=excluded.spec_sha,"
                           "policy_sha=excluded.policy_sha,spec_json=excluded.spec_json,approved_at=excluded.approved_at",
                           (repository, issue, spec.id, spec.revision, digest(spec), policy_sha,
                            spec.model_dump_json(), utcnow()))
                db.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError("This WorkSpec id is already attached to another issue") from exc

    def approved(self, repository: str, issue: int, spec: WorkSpec, policy_sha: str) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT spec_sha,policy_sha FROM approvals WHERE repository=? AND issue_number=?",
                             (repository, issue)).fetchone()
            return bool(row and row["spec_sha"] == digest(spec) and row["policy_sha"] == policy_sha)

    def latest(self, repository: str, issue: int) -> RunRecord | None:
        with self.connection() as db:
            row = db.execute("SELECT record FROM runs WHERE repository=? AND issue_number=? "
                             "ORDER BY rowid DESC LIMIT 1", (repository, issue)).fetchone()
            return RunRecord.model_validate_json(row[0]) if row else None

    def get(self, run_id: str) -> RunRecord:
        with self.connection() as db:
            row = db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise SRAError(f"Unknown run: {run_id}")
            return RunRecord.model_validate_json(row[0])

    def list(self, repository: str) -> list[RunRecord]:
        with self.connection() as db:
            rows = db.execute("SELECT record FROM runs WHERE repository=? ORDER BY rowid DESC LIMIT 100",
                              (repository,)).fetchall()
            return [RunRecord.model_validate_json(row[0]) for row in rows]

    def claim(self, repository: str, issue: int, spec: WorkSpec, policy_sha: str,
              selection: Selection, workspace: str, branch: str, base_sha: str,
              *, retry: bool, max_attempts: int) -> RunRecord:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            approval = db.execute("SELECT * FROM approvals WHERE repository=? AND issue_number=?",
                                  (repository, issue)).fetchone()
            if not approval or approval["spec_sha"] != digest(spec) or approval["policy_sha"] != policy_sha:
                raise ConflictError("Spec or policy is not locally approved; inspect and approve its current hash")
            previous = db.execute("SELECT record FROM runs WHERE repository=? AND issue_number=? "
                                  "ORDER BY rowid DESC LIMIT 1", (repository, issue)).fetchone()
            attempt = 1
            if previous:
                old = RunRecord.model_validate_json(previous[0])
                if old.state in ACTIVE:
                    raise ConflictError(f"Active run {old.id}; inspect/recover it instead of duplicating execution")
                if old.spec_sha256 == digest(spec):
                    if not retry:
                        raise ConflictError("Already attempted; use --retry for explicit rework/provider switching")
                    attempt = old.attempt + 1
            if attempt > max_attempts:
                raise ConflictError("Attempt budget exhausted; revise the spec or explicitly raise the project limit")
            now = utcnow()
            record = RunRecord(id=uuid.uuid4().hex, repository=repository, issue_number=issue,
                               spec_sha256=digest(spec), policy_sha256=policy_sha, state="preparing",
                               attempt=attempt, provider=selection.provider, model=selection.model,
                               effort=selection.effort, workspace=workspace, branch=branch, base_sha=base_sha,
                               owner_pid=os.getpid(), created_at=now, updated_at=now)
            try:
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (record.id, repository, issue,
                           record.state, record.created_at, record.model_dump_json()))
                db.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Another process already claimed this issue") from exc
            return record

    def update(self, run_id: str, *, expected_state: str | None = None, **changes) -> RunRecord:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise SRAError(f"Unknown run: {run_id}")
            old = RunRecord.model_validate_json(row[0])
            if expected_state is not None and old.state != expected_state:
                raise ConflictError(f"Expected state {expected_state}, found {old.state}")
            state = changes.get("state", old.state)
            if state != old.state and state not in TRANSITIONS[old.state]:
                raise ConflictError(f"Invalid state transition: {old.state} -> {state}")
            record = RunRecord.model_validate({**old.model_dump(), **changes, "updated_at": utcnow()})
            try:
                db.execute("UPDATE runs SET state=?,record=? WHERE id=?",
                           (record.state, record.model_dump_json(), run_id))
                db.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Another run is active for this issue") from exc
            return record

    def run_dir(self, run_id: str) -> Path:
        if not run_id.isalnum():
            raise SRAError("Invalid run ID")
        path = self.root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def recover(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record.state not in ACTIVE:
            raise ConflictError("Run is not active")
        if record.host != socket.gethostname():
            raise ConflictError("Run belongs to another host; do not recover shared/copied live state")
        if record.child_pid:
            try:
                os.killpg(record.child_pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise ConflictError("Cannot inspect the recorded process group") from exc
            else:
                raise ConflictError("Recorded child process group still exists; inspect it before recovery")
        for pid in (record.owner_pid, record.child_pid):
            if not pid:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise ConflictError("Cannot establish that the recorded process has stopped") from exc
            raise ConflictError(f"Recorded PID {pid} still exists; refusing automatic lease takeover")
        return self.update(run_id, state="blocked", error="Manually recovered after confirming processes stopped")

    @contextmanager
    def publish_lock(self, repository: str):
        # Coordinate repeated publish commands on this machine/state directory.
        import fcntl
        lock_path = self.root / ("publish-" + digest(repository)[:24] + ".lock")
        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
