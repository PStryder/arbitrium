"""Durable per-occurrence authorization. This module never schedules work."""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import uuid
import sys

from .jobs import Registry, TIMESTAMP_PATTERN, canonical, identifier, local_reference, utcnow, uuid_id
from .catalog import Catalog


def current_user_sid():
    """Read the current process token; no impersonation or privilege changes."""
    kernel = C.WinDLL("kernel32", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    advapi.OpenProcessToken.restype = W.BOOL
    advapi.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    advapi.GetTokenInformation.restype = W.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    token, text = W.HANDLE(), W.LPWSTR()
    def check(result):
        if not result:
            raise C.WinError(C.get_last_error())
    check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)))
    try:
        elevation, elevation_size = W.DWORD(), W.DWORD()
        check(advapi.GetTokenInformation(token, 20, C.byref(elevation), C.sizeof(elevation), C.byref(elevation_size)))
        if elevation.value:
            raise PermissionError("Elevated execution is not supported by managed grants")
        size = W.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))
        if not size.value:
            raise C.WinError(C.get_last_error())
        storage = C.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, 1, storage, size, C.byref(size)))
        sid = C.cast(storage, C.POINTER(C.c_void_p)).contents.value
        check(advapi.ConvertSidToStringSidW(sid, C.byref(text)))
        return text.value
    finally:
        if text:
            kernel.LocalFree(C.cast(text, C.c_void_p))
        kernel.CloseHandle(token)


def parse_utc(value):
    if not isinstance(value, str) or not re.fullmatch(TIMESTAMP_PATTERN, value):
        raise ValueError("Explicit UTC timestamp required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def runtime_scope(executable, cwd, registry_root):
    exe, directory = Path(executable).resolve(), Path(cwd).resolve()
    local_reference(str(exe))
    local_reference(str(directory))
    if not exe.is_file() or not directory.is_dir():
        raise ValueError("Approved runtime paths must exist")
    executable_hash = hashlib.sha256()
    with exe.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            executable_hash.update(chunk)
    package_hash = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob('*.py')):
        package_hash.update(source.name.encode())
        package_hash.update(source.read_bytes())
    return {"executable": os.path.normcase(str(exe)), "cwd": os.path.normcase(str(directory)),
            "registry": os.path.normcase(str(Path(registry_root).resolve())),
            "executable_digest": executable_hash.hexdigest(), "package_digest": package_hash.hexdigest()}


@dataclass(frozen=True)
class ScheduleScope:
    schedule_id: str
    start_at: str
    occurrences: int = 1
    interval_seconds: int | None = None
    grace_seconds: int = 60

    def validate(self):
        uuid_id(self.schedule_id)
        parse_utc(self.start_at)
        if type(self.occurrences) is not int or not 1 <= self.occurrences <= 10000:
            raise ValueError("A finite occurrence bound is required")
        if type(self.grace_seconds) is not int or not 1 <= self.grace_seconds <= 300:
            raise ValueError("Invalid start grace")
        if self.occurrences == 1:
            if self.interval_seconds is not None:
                raise ValueError("One-shot scope cannot have an interval")
        elif type(self.interval_seconds) is not int or not 60 <= self.interval_seconds <= 2678400 or self.grace_seconds >= self.interval_seconds:
            raise ValueError("Invalid recurring interval or overlapping start windows")
        if self.duration_seconds > 366 * 86400:
            raise ValueError("Schedule authorization exceeds one year")

    @property
    def duration_seconds(self):
        return (self.occurrences - 1) * (self.interval_seconds or 0) + self.grace_seconds

    @property
    def end_at(self):
        return parse_utc(self.start_at) + timedelta(seconds=self.duration_seconds)

    def occurrence(self, now):
        self.validate()
        delta = (now - parse_utc(self.start_at)).total_seconds()
        if delta < 0:
            raise PermissionError("Schedule has not started")
        index = int(delta // self.interval_seconds) if self.interval_seconds else 0
        scheduled_delta = index * (self.interval_seconds or 0)
        if index >= self.occurrences or delta - scheduled_delta > self.grace_seconds:
            raise PermissionError("Outside approved occurrence window")
        return index


@dataclass(frozen=True)
class GrantRequest:
    job_id: str
    principal_sid: str
    schedule: ScheduleScope
    approval_ref: str
    runner_executable: str
    runner_cwd: str


class ApprovalStore:
    """Issuance requires an injected trusted reviewer; no issuance CLI is exposed.

    A recurring grant authorizes a bounded schedule once. Each occurrence can be
    consumed once and is bound to one durable run. A crash never refunds a claim.
    """
    def __init__(self, registry: Registry, catalog: Catalog, *, initialize=True):
        self.registry, self.catalog = registry, catalog
        if not initialize:
            return
        with registry.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants(grant_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                    template_digest TEXT NOT NULL,catalog_digest TEXT NOT NULL,principal_sid TEXT NOT NULL,
                    schedule_id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,runtime TEXT NOT NULL,approval_ref TEXT NOT NULL,
                    issued_at TEXT NOT NULL,revoked_at TEXT);
                CREATE TABLE IF NOT EXISTS grant_claims(grant_id TEXT NOT NULL,occurrence INTEGER NOT NULL,
                    run_id TEXT UNIQUE NOT NULL,claimed_at TEXT NOT NULL,
                    PRIMARY KEY(grant_id,occurrence));
            """)

    def issue(self, request: GrantRequest, *, authorize=None):
        request.schedule.validate()
        identifier(request.approval_ref)
        if not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", request.principal_sid) or request.principal_sid != current_user_sid():
            raise PermissionError("Grant must name the reviewing ordinary user account")
        job = self.registry.job(request.job_id)
        self.catalog.validate_job(job)
        runtime = runtime_scope(request.runner_executable, request.runner_cwd, self.registry.root)
        review = {"job": job, "catalog_digest": self.catalog.digest, "runtime": runtime, **asdict(request)}
        if authorize is None or authorize(review) is not True:
            raise PermissionError("Exact operational grant issuance requires explicit approval")
        self.catalog.validate_job(self.registry.job(request.job_id))
        grant_id = str(uuid.uuid4())
        with self.registry.connection() as db:
            db.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                       (grant_id, request.job_id, job["template_digest"], self.catalog.digest,
                        request.principal_sid, request.schedule.schedule_id, canonical(asdict(request.schedule)), canonical(runtime), request.approval_ref, utcnow()))
        return grant_id

    def describe(self, grant_id):
        uuid_id(grant_id)
        with self.registry.connection() as db:
            row = db.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise PermissionError("Unknown grant")
        return dict(row)

    def revoke(self, grant_id):
        uuid_id(grant_id)
        with self.registry.connection() as db:
            return db.execute("UPDATE grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL", (utcnow(), grant_id)).rowcount == 1

    def _check(self, row, job, principal, runtime, now):
        if row["revoked_at"] or row["job_id"] != job["job_id"] or row["template_digest"] != job["template_digest"] or row["catalog_digest"] != self.catalog.digest or row["principal_sid"] != principal or json.loads(row["runtime"]) != runtime:
            raise PermissionError("Grant revoked or approval scope mismatch")
        return ScheduleScope(**json.loads(row["scope"])).occurrence(now)

    def authorize(self, grant_id, job, run_id, *, now=None):
        uuid_id(grant_id)
        uuid_id(run_id)
        self.catalog.validate_job(job)
        principal = current_user_sid()
        runtime = runtime_scope(sys.executable, os.getcwd(), self.registry.root)
        now = now or datetime.now(timezone.utc)
        try:
            with self.registry.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
                run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None or run is None or run["job_id"] != job["job_id"] or run["status"] != "queued":
                    return False
                index = self._check(row, job, principal, runtime, now)
                db.execute("INSERT INTO grant_claims VALUES (?,?,?,?)", (grant_id, index, run_id, utcnow()))
            return True
        except (PermissionError, sqlite3.IntegrityError):
            return False
