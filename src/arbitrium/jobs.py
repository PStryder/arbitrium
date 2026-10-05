"""Local durable jobs. No MCP execution endpoint or production approval provider.

Trusted templates are supplied by embedding code, never loaded from job events.
The SQLite file and template code require the same local trust as Arbitrium itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

from .winjob import OwnedProcess

TERMINAL = {"completed", "failed", "canceled", "interrupted", "unknown"}
STATUSES = {"queued", "running"} | TERMINAL
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)(?![\s\S])"
REFERENCE_PATTERN = r"^[A-Za-z]:[\\/](?!\.\.(?:[\\/]|$))(?!.*[\\/]\.\.(?:[\\/]|$))[^:\x00-\x1f]*(?![\s\S])"
REASONS = {"queued", "started", "exit_zero", "exit_nonzero", "cancel_requested",
           "deadline", "launch_failed", "worker_stopped", "ownership_lost",
           "lease_expired", "approval_denied"}
STATUS_REASONS = {
    "queued": {"queued"}, "running": {"started"}, "completed": {"exit_zero"},
    "failed": {"exit_nonzero", "deadline", "launch_failed"},
    "canceled": {"cancel_requested", "approval_denied"},
    "interrupted": {"worker_stopped", "lease_expired"},
    "unknown": {"ownership_lost", "lease_expired"},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value):
        raise ValueError("Invalid identifier")
    return value


def uuid_id(value: str) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected canonical UUID")
    return value


def bounded(value: str, limit=1024) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid bounded string")
    return value


def local_reference(value: str) -> str:
    """Lexical contract validation; never open or resolve an event reference."""
    bounded(value)
    if not re.fullmatch(REFERENCE_PATTERN, value):
        raise ValueError("Invalid absolute local Windows reference")
    path = PureWindowsPath(value)
    if not re.fullmatch(r"[A-Za-z]:", path.drive) or not path.root:
        raise ValueError("Absolute local Windows reference required")
    if any(part == ".." or ":" in part for part in path.parts[1:]):
        raise ValueError("Traversal and alternate streams are forbidden")
    return value


@dataclass(frozen=True)
class Template:
    """Reviewed executable and exact argument choices, not arbitrary command text.

    Each argument slot is a finite allowlist. Script/interpreter flags and paths
    belong in fixed_args, never free-form user inputs. env_ref resolves through a
    trusted callback at execution time. It does not contain environment values.
    """
    template_id: str
    executable: str
    fixed_args: tuple[str, ...]
    choices: tuple[tuple[str, ...], ...]
    cwd: str
    env_ref: str
    timeout_seconds: int = 300
    result_paths: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()

    def validate(self):
        identifier(self.template_id)
        identifier(self.env_ref)
        for path in (self.executable, self.cwd):
            local_reference(path)
            if not Path(path).is_absolute():
                raise ValueError("Absolute paths required")
        if not Path(self.executable).is_file() or not Path(self.cwd).is_dir():
            raise ValueError("Executable and working directory must exist")
        if Path(self.executable).suffix.lower() in {".bat", ".cmd", ".ps1"} or Path(self.executable).stem.lower() in {"cmd", "powershell", "pwsh", "bash", "sh", "zsh"}:
            raise ValueError("Managed shell templates are disabled")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 86400:
            raise ValueError("Invalid timeout")
        if len(self.fixed_args) > 32 or len(self.choices) > 16 or len(self.result_paths) > 8:
            raise ValueError("Template exceeds bounds")
        for arg in self.fixed_args:
            bounded(arg, 256)
        for options in self.choices:
            if not 1 <= len(options) <= 32:
                raise ValueError("Argument choices must be finite and bounded")
            for option in options:
                bounded(option, 256)
        for path in self.result_paths:
            bounded(path)
            resolved = (Path(self.cwd) / path).resolve()
            if Path(path).is_absolute() or not resolved.is_relative_to(Path(self.cwd).resolve()):
                raise ValueError("Result references must remain inside working directory")
            bounded(str(resolved))
            local_reference(str(resolved))
        if len(self.assets) > 16:
            raise ValueError("Too many reviewed assets")
        for asset in self.assets:
            local_reference(asset)
            if not Path(asset).is_absolute() or not Path(asset).is_file():
                raise ValueError("Reviewed asset must be an existing absolute file")

    def digest(self):
        self.validate()
        hashes = {}
        for path in (self.executable, *self.assets):
            digest = hashlib.sha256()
            with Path(path).open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            hashes[path] = digest.hexdigest()
        resolved = {path: os.path.normcase(str(Path(path).resolve())) for path in (self.executable, self.cwd, *self.assets)}
        return hashlib.sha256(canonical({"template": asdict(self), "file_hashes": hashes, "resolved_paths": resolved}).encode()).hexdigest()

    def arguments(self, values):
        if not isinstance(values, (tuple, list)) or len(values) != len(self.choices):
            raise ValueError("Incorrect argument count")
        if any(not isinstance(v, str) or v not in allowed for v, allowed in zip(values, self.choices)):
            raise ValueError("Argument not approved by template")
        return [*self.fixed_args, *values]

    def spec(self, values):
        return {"executable": str(Path(self.executable).resolve()), "arguments": self.arguments(values),
                "values": list(values), "cwd": str(Path(self.cwd).resolve()), "env_ref": self.env_ref,
                "timeout_seconds": self.timeout_seconds, "result_paths": list(self.result_paths)}


class Registry:
    """Transactionally couples status changes and immutable outbox events."""

    def __init__(self, root: Path, *, max_pending=10000):
        if os.name != "nt":
            raise OSError("Managed jobs and producer events currently require Windows")
        self.root = Path(root).resolve()
        local_reference(str(self.root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        self.max_pending = max_pending
        if type(max_pending) is not int or max_pending < 6:
            raise ValueError("max_pending must be at least six")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,template_id TEXT NOT NULL,
                    template_digest TEXT NOT NULL,spec TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
                    exit_code INTEGER,reason_code TEXT NOT NULL,owner_id TEXT NOT NULL,
                    lease_until REAL NOT NULL,pid INTEGER,sequence INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(job_id)
                    WHERE status IN ('queued','running','unknown');
                CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,body TEXT NOT NULL,
                    created_at TEXT NOT NULL,delivered_at TEXT);
            """)
            version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version and version[0] != "1":
                raise ValueError("Unsupported registry schema")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('instance_id',?)", (str(uuid.uuid4()),))
            self.instance_id = db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0]

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, template: Template, values=()) -> str:
        digest = template.digest()
        spec = template.spec(values)
        job_id = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Re-saving the identical immutable specification is idempotent; a
            # new UUID must not bypass the active/unknown-run exclusion.
            existing = db.execute("SELECT job_id FROM jobs WHERE template_digest=? AND spec=? ORDER BY created_at LIMIT 1", (digest, canonical(spec))).fetchone()
            if existing:
                return existing[0]
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job_id, template.template_id, digest, canonical(spec), utcnow()))
        return job_id

    def job(self, job_id):
        uuid_id(job_id)
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown job ID")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        return result

    def run(self, run_id):
        uuid_id(run_id)
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown run ID")
        return dict(row)

    def _event(self, db, run_id):
        run = dict(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (run["job_id"],)).fetchone()
        spec = json.loads(job["spec"])
        sequence = run["sequence"] + 1
        db.execute("UPDATE runs SET sequence=? WHERE run_id=?", (sequence, run_id))
        event = {"schema_version": 1, "event_id": str(uuid.uuid4()), "source": "arbitrium",
                 "instance_id": self.instance_id, "job_id": run["job_id"], "run_id": run_id,
                 "status": run["status"], "observed_at": utcnow(), "sequence": sequence,
                 "context": {"template_id": job["template_id"], "reason_code": run["reason_code"], "exit_code": run["exit_code"]},
                 "log_refs": [], "result_refs": []}
        # SQLite is the authoritative bounded-per-run lifecycle log. References
        # never include stdout, user command text, environment values, or errors.
        event["log_refs"] = [str(self.path)]
        if run["status"] == "completed":
            root = Path(spec["cwd"]).resolve()
            refs = [(root / p).resolve() for p in spec["result_paths"]]
            if any(not p.is_relative_to(root) for p in refs):
                raise ValueError("Result reference escaped working directory")
            event["result_refs"] = [str(p) for p in refs]
        validate_event(event)
        db.execute("INSERT INTO outbox VALUES (?,?,?,NULL)", (event["event_id"], canonical(event), event["observed_at"]))

    def reserve(self, job_id, owner_id):
        self.job(job_id)
        uuid_id(owner_id)
        run_id = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Also cover pre-idempotency duplicate rows and edited template
            # versions: unresolved execution blocks this template/cwd scope.
            requested = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            requested_cwd = os.path.normcase(json.loads(requested['spec'])['cwd'])
            active_specs = db.execute("""SELECT prior.spec FROM runs r JOIN jobs prior ON r.job_id=prior.job_id
                WHERE r.status IN ('queued','running','unknown') AND prior.template_id=?""", (requested['template_id'],)).fetchall()
            if any(os.path.normcase(json.loads(row['spec'])['cwd']) == requested_cwd for row in active_specs):
                raise sqlite3.IntegrityError("An active or unknown run blocks this template and working directory")
            pending = db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]
            active = db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
            if pending + active * 3 + 3 > self.max_pending:
                raise RuntimeError("Outbox capacity exhausted; job admission stopped")
            db.execute("INSERT INTO runs(run_id,job_id,status,created_at,reason_code,owner_id,lease_until) VALUES (?,?,'queued',?,'queued',?,?)",
                       (run_id, job_id, utcnow(), owner_id, time.time() + 30))
            self._event(db, run_id)
        return run_id

    def transition(self, run_id, owner_id, status, reason, exit_code=None, pid=None):
        if status not in STATUSES or reason not in STATUS_REASONS[status]:
            raise ValueError("Invalid transition")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["owner_id"] != owner_id or row["status"] not in {"queued", "running"}:
                raise RuntimeError("Run ownership or state changed")
            if status == "queued" or (status == "running" and row["status"] != "queued"):
                raise ValueError("Invalid transition")
            if row["status"] == "queued" and status in {"completed", "unknown"}:
                raise ValueError("Unstarted run cannot complete or lose process ownership")
            db.execute("UPDATE runs SET status=?,reason_code=?,exit_code=?,pid=COALESCE(?,pid),started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,finished_at=?,lease_until=? WHERE run_id=?",
                       (status, reason, exit_code, pid, status, utcnow(), utcnow() if status in TERMINAL else None, time.time()+30, run_id))
            self._event(db, run_id)

    def heartbeat(self, run_id, owner_id):
        with self.connection() as db:
            result = db.execute("UPDATE runs SET lease_until=? WHERE run_id=? AND owner_id=? AND status IN ('queued','running')", (time.time()+30, run_id, owner_id))
            if result.rowcount != 1:
                raise RuntimeError("Run ownership lost")

    def cancel(self, run_id):
        """Persist a request. Only the live owning worker may signal its handle."""
        uuid_id(run_id)
        with self.connection() as db:
            result = db.execute("UPDATE runs SET cancel_requested=1 WHERE run_id=? AND status IN ('queued','running')", (run_id,))
            return result.rowcount == 1

    def recover(self):
        """Never kill/adopt PIDs. Expired running leases remain uncertain."""
        recovered = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM runs WHERE status IN ('queued','running') AND lease_until < ?", (time.time(),)).fetchall():
                status = "unknown" if row["status"] == "running" else "interrupted"
                db.execute("UPDATE runs SET status=?,reason_code='lease_expired',finished_at=? WHERE run_id=?", (status, utcnow(), row["run_id"]))
                self._event(db, row["run_id"])
                recovered.append(row["run_id"])
        return recovered


def validate_event(event):
    keys = {"schema_version", "event_id", "source", "instance_id", "job_id", "run_id", "status", "observed_at", "sequence", "context", "log_refs", "result_refs"}
    if not isinstance(event, dict) or set(event) != keys or type(event["schema_version"]) is not int or event["schema_version"] != 1 or event["source"] != "arbitrium":
        raise ValueError("Unsupported event contract")
    for key in ("event_id", "instance_id", "job_id", "run_id"):
        uuid_id(event[key])
    if event["status"] not in STATUSES or type(event["sequence"]) is not int or not 1 <= event["sequence"] < 2**31:
        raise ValueError("Invalid lifecycle event")
    bounded(event["observed_at"], 32)
    if not re.fullmatch(TIMESTAMP_PATTERN, event["observed_at"]):
        raise ValueError("RFC3339 UTC with at most six fractional digits required")
    observed = datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00"))
    if observed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    context = event["context"]
    if not isinstance(context, dict) or set(context) != {"template_id", "reason_code", "exit_code"} or context["reason_code"] not in STATUS_REASONS[event["status"]]:
        raise ValueError("Invalid context")
    identifier(context["template_id"])
    if context["exit_code"] is not None and (type(context["exit_code"]) is not int or not -(2**31) <= context["exit_code"] < 2**32):
        raise ValueError("Invalid exit code")
    if event["status"] == "completed" and context["exit_code"] != 0:
        raise ValueError("Completed requires observed exit zero")
    for key in ("log_refs", "result_refs"):
        if not isinstance(event[key], list) or len(event[key]) > 8:
            raise ValueError("Reference limit exceeded")
        for ref in event[key]:
            local_reference(ref)
    if len(canonical(event).encode()) > 16384:
        raise ValueError("Event exceeds 16 KiB")


class Publisher:
    """Opt-in adapter; default denies publication, including inbox creation."""
    def __init__(self, registry: Registry, inbox: Path, authorize: Callable[[Path], bool] | None = None):
        self.registry = registry
        self.inbox = Path(inbox).resolve()
        self.authorize = authorize

    def publish(self, limit=100, *, event_id=None):
        if self.authorize is None or self.authorize(self.inbox) is not True:
            raise PermissionError("Inbox publication requires action-time approval")
        if not self.inbox.is_dir():
            raise ValueError("Inbox must already exist")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid batch bound")
        if event_id is not None:
            uuid_id(event_id)
        delivered = 0
        # Serialize publishers using SQLite so conflicting content is never overwritten.
        with self.registry.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prefix = ".arbitrium-" + self.registry.instance_id + "-"
            # This registry's lock excludes another active publisher. Recover only
            # its exact temporary filenames; never delete consumer JSON files.
            pattern = re.compile(re.escape(prefix) + r"[0-9a-f-]{36}-[0-9a-f]{32}\.tmp")
            for stale in self.inbox.glob(prefix + "*.tmp"):
                if pattern.fullmatch(stale.name) and stale.is_file() and not stale.is_symlink():
                    stale.unlink()
            if event_id is None:
                rows = db.execute("SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY created_at,event_id LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM outbox WHERE delivered_at IS NULL AND event_id=? LIMIT 1", (event_id,)).fetchall()
            for row in rows:
                event = json.loads(row["body"])
                validate_event(event)
                target = self.inbox / (row["event_id"] + ".json")
                data = row["body"].encode("utf-8")
                if target.exists():
                    if target.is_symlink() or target.stat().st_size > 16384 or target.read_bytes() != data:
                        raise RuntimeError("Event ID collision; publication stopped")
                else:
                    temp = self.inbox / (prefix + row["event_id"] + "-" + uuid.uuid4().hex + ".tmp")
                    try:
                        with temp.open("xb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        # On Windows rename refuses an existing destination.
                        os.rename(temp, target)
                    finally:
                        temp.unlink(missing_ok=True)
                db.execute("UPDATE outbox SET delivered_at=? WHERE event_id=?", (utcnow(), row["event_id"]))
                delivered += 1
            # Bounded producer delivery ledger. Inbox files belong to consumer;
            # delivered means published, NOT consumer-processed or acknowledged.
            db.execute("DELETE FROM outbox WHERE event_id IN (SELECT event_id FROM outbox WHERE delivered_at IS NOT NULL ORDER BY delivered_at DESC,event_id DESC LIMIT -1 OFFSET 1000)")
        return delivered


class Runner:
    """Synchronous local worker. No approval implementation ships in this milestone.

    Embedders must approve the exact immutable job and run at action time. Tests
    inject a fixture-only approver. A native Windows Job Object owns the process
    group; persisted PID values are diagnostic only, never kill targets.
    """
    def __init__(self, registry: Registry, templates: dict[str, Template], *, authorize=None, resolve_environment=None):
        self.registry = registry
        self.templates = dict(templates)
        self.authorize = authorize
        self.resolve_environment = resolve_environment
        self.owner_id = str(uuid.uuid4())
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def execute(self, job_id):
        job = self.registry.job(job_id)
        template = self.templates[job["template_id"]]
        if template.digest() != job["template_digest"]:
            raise ValueError("Template changed; save and approve a new job")
        expected = template.spec(job["spec"]["values"])
        if job["spec"] != expected:
            raise ValueError("Saved job does not match template")
        if self.authorize is None or self.resolve_environment is None:
            raise PermissionError("Managed execution is disabled without an action-time approval provider")
        run_id = self.registry.reserve(job_id, self.owner_id)
        process = None
        try:
            if self.authorize(job, run_id) is not True:
                self.registry.transition(run_id, self.owner_id, "canceled", "approval_denied")
                return self.registry.run(run_id)
            if template.digest() != job["template_digest"]:
                raise ValueError("Reviewed files changed during approval")
            env = self.resolve_environment(template.env_ref)
            if not isinstance(env, dict) or len(env) > 64 or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", k) or not isinstance(v, str) or "\0" in v or len(v) > 32768 for k, v in env.items()):
                raise ValueError("Invalid resolved environment")
            # No inherited environment, shell, stdout, stderr, or exception text.
            # Trusted provider supplies the minimum OS variables needed to start.
            self.registry.heartbeat(run_id, self.owner_id)
            if self.registry.run(run_id)["cancel_requested"]:
                self.registry.transition(run_id, self.owner_id, "canceled", "cancel_requested")
                return self.registry.run(run_id)
            if self._stop.is_set():
                self.registry.transition(run_id, self.owner_id, "interrupted", "worker_stopped")
                return self.registry.run(run_id)
            # Record intent BEFORE spawning; a crash in this window is unknown.
            self.registry.transition(run_id, self.owner_id, "running", "started")
            process = OwnedProcess([expected["executable"], *expected["arguments"]], cwd=expected["cwd"], env=dict(env))
            with self.registry.connection() as db:
                db.execute("UPDATE runs SET pid=? WHERE run_id=? AND owner_id=? AND status='running'", (process.pid, run_id, self.owner_id))
            deadline = time.monotonic() + template.timeout_seconds
            while process.poll() is None or process.active_count():
                row = self.registry.run(run_id)
                if row["status"] != "running":
                    process.kill()
                    process.wait(timeout=5)
                    return self.registry.run(run_id)
                reason = "cancel_requested" if row["cancel_requested"] else "worker_stopped" if self._stop.is_set() else "deadline" if time.monotonic() >= deadline else None
                if reason:
                    process.kill()
                    code = process.wait(timeout=5)
                    status = {"cancel_requested": "canceled", "worker_stopped": "interrupted", "deadline": "failed"}[reason]
                    self.registry.transition(run_id, self.owner_id, status, reason, code)
                    return self.registry.run(run_id)
                self.registry.heartbeat(run_id, self.owner_id)
                self._stop.wait(0.05)
            code = process.wait()
            self.registry.transition(run_id, self.owner_id, "completed" if code == 0 else "failed", "exit_zero" if code == 0 else "exit_nonzero", code)
        except BaseException:
            status, reason = "failed", "launch_failed"
            if process is not None:
                status, reason = "interrupted", "worker_stopped"
                try:
                    if process.poll() is None or process.active_count():
                        process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    status, reason = "unknown", "ownership_lost"
            row = self.registry.run(run_id)
            if row["status"] in {"queued", "running"}:
                self.registry.transition(run_id, self.owner_id, status, reason)
            raise
        finally:
            if process is not None:
                process.close()
        return self.registry.run(run_id)
