import dataclasses
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from arbitrium.jobs import Publisher, Registry, Runner, Template, canonical, validate_event, STATUSES, REASONS
from arbitrium.jobs_cli import main
from arbitrium.scheduler import TaskDefinition, WindowsTaskScheduler

pytestmark = pytest.mark.skipif(os.name != "nt", reason="First managed-job backend targets Windows")


@pytest.fixture
def setup_job(tmp_path):
    fixture = tmp_path / "fixture.py"
    fixture.write_text("import os,sys,time\nprint(os.environ.get('FIXTURE_SECRET',''),flush=True)\nif sys.argv[1] == 'wait': time.sleep(10)\nsys.exit(7 if sys.argv[1] == 'fail' else 0)\n")
    template = Template("fixture-v1", sys.executable, ("-I", str(fixture)), (("ok", "fail", "wait"),), str(tmp_path), "fixture-env", 2, assets=(str(fixture),))
    registry = Registry(tmp_path / "state")
    return registry, template


def runner(registry, template, authorize=lambda job, run: True):
    # Only harmless fixture programs from pytest's temporary workspace are used.
    def environment(ref):
        assert ref == "fixture-env"
        return {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"), "FIXTURE_SECRET": "DO-NOT-PERSIST-SECRET"}
    return Runner(registry, {template.template_id: template}, authorize=authorize, resolve_environment=environment)


def events(registry):
    with registry.connection() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM outbox ORDER BY created_at,event_id")]


@pytest.mark.parametrize("argument,status,code", [("ok", "completed", 0), ("fail", "failed", 7)])
def test_durable_lifecycle_and_no_secret_output(setup_job, argument, status, code):
    registry, template = setup_job
    job_id = registry.save(template, [argument])
    result = runner(registry, template).execute(job_id)
    assert result["status"] == status and result["exit_code"] == code
    reopened = Registry(registry.root)
    assert reopened.instance_id == registry.instance_id
    assert reopened.run(result["run_id"]) == result
    assert reopened.job(job_id)["spec"]["env_ref"] == "fixture-env"
    lifecycle = events(registry)
    assert [event["status"] for event in lifecycle] == ["queued", "running", status]
    assert [event["sequence"] for event in lifecycle] == [1, 2, 3]
    assert len({event["event_id"] for event in lifecycle}) == 3
    for event in lifecycle:
        validate_event(event)
    assert "DO-NOT-PERSIST-SECRET" not in canonical(lifecycle)
    with registry.connection() as db:
        assert "DO-NOT-PERSIST-SECRET" not in "\n".join(db.iterdump())


def test_default_execution_and_cli_are_disabled(setup_job, tmp_path):
    registry, template = setup_job
    job_id = registry.save(template, ["ok"])
    with patch("subprocess.Popen") as spawn:
        with pytest.raises(PermissionError):
            Runner(registry, {template.template_id: template}).execute(job_id)
        assert main(["run", "--registry", str(tmp_path / "must-not-exist"), "--job-id", job_id]) == 3
        spawn.assert_not_called()
    assert not (tmp_path / "must-not-exist").exists()
    assert not events(registry)


def test_approval_denial_prevents_spawn(setup_job):
    registry, template = setup_job
    job_id = registry.save(template, ["ok"])
    with patch("subprocess.Popen") as spawn:
        result = runner(registry, template, authorize=lambda job, run: False).execute(job_id)
        spawn.assert_not_called()
    assert result["status"] == "canceled"
    assert result["reason_code"] == "approval_denied"


def test_cancel_request_is_durable_and_owned_worker_confirms(setup_job):
    registry, template = setup_job
    job_id = registry.save(template, ["wait"])
    ready = threading.Event()
    ids = []
    def approve(job, run_id):
        ids.append(run_id)
        ready.set()
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner(registry, template, approve).execute, job_id)
        assert ready.wait(5)
        until = time.monotonic()+5
        while registry.run(ids[0])["pid"] is None and time.monotonic() < until:
            time.sleep(.01)
        assert registry.run(ids[0])["pid"] is not None
        assert Registry(registry.root).cancel(ids[0])
        result = future.result(timeout=5)
    assert result["status"] == "canceled"
    assert result["reason_code"] == "cancel_requested"
    assert result["exit_code"] is not None
    assert not registry.cancel(ids[0])


def test_timeout_and_worker_stop_distinct(setup_job):
    registry, template = setup_job
    template = dataclasses.replace(template, timeout_seconds=1)
    job_id = registry.save(template, ["wait"])
    result = runner(registry, template).execute(job_id)
    assert (result["status"], result["reason_code"]) == ("failed", "deadline")
    stopped = runner(registry, template)
    stopped.stop()
    result = stopped.execute(job_id)
    assert (result["status"], result["reason_code"]) == ("interrupted", "worker_stopped")


def test_recovery_never_signals_saved_pid_or_assumes_reboot_survival(setup_job):
    registry, template = setup_job
    job_id = registry.save(template, ["ok"])
    owner = str(uuid.uuid4())
    run_id = registry.reserve(job_id, owner)
    registry.transition(run_id, owner, "running", "started", pid=os.getpid())
    assert registry.recover() == []  # live lease is left alone
    with registry.connection() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE run_id=?", (run_id,))
    with patch("os.kill") as kill, patch("subprocess.Popen") as spawn:
        assert Registry(registry.root).recover() == [run_id]
        kill.assert_not_called()
        spawn.assert_not_called()
    assert registry.run(run_id)["status"] == "unknown"
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve(job_id, str(uuid.uuid4()))  # unknown blocks accidental overlap
    with pytest.raises(RuntimeError):
        registry.transition(run_id, owner, "completed", "exit_zero", 0)
    assert registry.recover() == []


def test_unstarted_expired_run_is_interrupted(setup_job):
    registry, template = setup_job
    run_id = registry.reserve(registry.save(template, ["ok"]), str(uuid.uuid4()))
    with registry.connection() as db:
        db.execute("UPDATE runs SET lease_until=0")
    registry.recover()
    assert registry.run(run_id)["status"] == "interrupted"


def test_argument_template_and_asset_changes_rejected(setup_job):
    registry, template = setup_job
    with pytest.raises(ValueError):
        registry.save(template, ["ok & calc.exe"])
    with pytest.raises(ValueError):
        registry.save(dataclasses.replace(template, cwd="relative"), ["ok"])
    with pytest.raises(ValueError):
        registry.save(dataclasses.replace(template, result_paths=("../escape",)), ["ok"])
    job_id = registry.save(template, ["ok"])
    Path(template.assets[0]).write_text("raise SystemExit(0)")
    with patch("subprocess.Popen") as spawn:
        with pytest.raises(ValueError, match="Template changed"):
            runner(registry, template).execute(job_id)
        spawn.assert_not_called()


def test_forged_saved_arguments_rejected(setup_job):
    registry, template = setup_job
    job_id = registry.save(template, ["ok"])
    spec = registry.job(job_id)["spec"]
    spec["arguments"] = ["-c", "raise Exception('unapproved')"]
    with registry.connection() as db:
        db.execute("UPDATE jobs SET spec=? WHERE job_id=?", (canonical(spec), job_id))
    with pytest.raises(ValueError, match="does not match"):
        runner(registry, template).execute(job_id)


def test_outbox_atomic_retry_dedup_and_failure(setup_job, tmp_path):
    registry, template = setup_job
    runner(registry, template).execute(registry.save(template, ["ok"]))
    inbox = tmp_path / "fake-inbox"
    inbox.mkdir()
    publisher = Publisher(registry, inbox, authorize=lambda path: path == inbox)
    with patch("arbitrium.jobs.os.rename", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            publisher.publish()
    assert list(inbox.iterdir()) == []
    assert publisher.publish() == 3
    first = {p.name: p.read_bytes() for p in inbox.iterdir()}
    # Simulate crash after rename but before SQLite commit: replay same IDs/bytes.
    with registry.connection() as db:
        db.execute("UPDATE outbox SET delivered_at=NULL")
    assert publisher.publish() == 3
    assert {p.name: p.read_bytes() for p in inbox.iterdir()} == first
    assert publisher.publish() == 0
    assert len(list(inbox.glob("*.json"))) == 3
    assert not list(inbox.glob("*.tmp"))


def test_publish_denied_collision_and_retention_bound(setup_job, tmp_path):
    registry, template = setup_job
    registry.reserve(registry.save(template, ["ok"]), str(uuid.uuid4()))
    inbox = tmp_path / "fake-inbox"
    with pytest.raises(PermissionError):
        Publisher(registry, inbox).publish()
    assert not inbox.exists()
    inbox.mkdir()
    event = events(registry)[0]
    target = inbox / (event["event_id"] + ".json")
    target.write_text("conflicting-data")
    with pytest.raises(RuntimeError, match="collision"):
        Publisher(registry, inbox, lambda p: True).publish()
    assert target.read_text() == "conflicting-data"
    # Inbox retention is consumer-owned; delivered ledger alone is bounded.
    with registry.connection() as db:
        for _ in range(1010):
            db.execute("INSERT INTO outbox VALUES (?,?,?,?)", (str(uuid.uuid4()), "{}", "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))
    target.write_text(canonical(event), encoding="utf-8")
    Publisher(registry, inbox, lambda p: True).publish()
    with registry.connection() as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NOT NULL").fetchone()[0] == 1000
    assert target.exists()


def test_outbox_backpressure_and_concurrent_admission(setup_job):
    registry, template = setup_job
    registry.max_pending = 6
    job_id = registry.save(template, ["ok"])
    registry.reserve(job_id, str(uuid.uuid4()))
    with pytest.raises(RuntimeError, match="capacity"):
        registry.reserve(registry.save(template, ["ok"]), str(uuid.uuid4()))
    registry.max_pending = 10000
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve(job_id, str(uuid.uuid4()))


def test_event_bounds_and_untrusted_fields(setup_job):
    registry, template = setup_job
    registry.reserve(registry.save(template, ["ok"]), str(uuid.uuid4()))
    event = events(registry)[0]
    event["instruction"] = "run arbitrary command"
    with pytest.raises(ValueError):
        validate_event(event)
    del event["instruction"]
    event["result_refs"] = ["x" * 1025]
    with pytest.raises(ValueError):
        validate_event(event)


def test_scheduler_xml_is_disabled_bound_to_job_and_never_registered(setup_job):
    registry, template = setup_job
    job_id = registry.save(template, ["ok"])
    definition = TaskDefinition(job_id, sys.executable, template.cwd, "2030-01-01T12:00:00+00:00", "S-1-5-21-111-222-333-1001")
    scheduler = WindowsTaskScheduler(registry, {template.template_id: template})
    with patch("subprocess.Popen") as spawn:
        report = scheduler.dry_run(definition)
        assert report["registered"] is False and report["execution_available"] is False
        assert "LeastPrivilege" in report["xml"] and "InteractiveToken" in report["xml"]
        assert job_id in report["xml"] and "-I -m arbitrium.jobs_cli run" in report["xml"]
        with pytest.raises(ValueError):
            scheduler.validate(report["xml"].replace("LeastPrivilege", "HighestAvailable"), definition)
        with pytest.raises(ValueError):
            scheduler.validate(report["xml"].replace("<Enabled>false", "<Enabled>true"), definition)
        with pytest.raises(PermissionError):
            scheduler.register(definition)
        spawn.assert_not_called()


def test_scheduler_requires_ordinary_sid_and_timezone(setup_job):
    registry, template = setup_job
    scheduler = WindowsTaskScheduler(registry, {template.template_id: template})
    definition = TaskDefinition(registry.save(template, ["ok"]), sys.executable, template.cwd, "2030-01-01T12:00:00", "S-1-5-18")
    with pytest.raises(ValueError):
        scheduler.generate(definition)
    with pytest.raises(ValueError):
        scheduler.generate(dataclasses.replace(definition, user_id="S-1-5-21-111-222-333-1001"))


def test_canonical_fixture_matches_runtime_and_schema():
    docs = Path(__file__).resolve().parents[1] / "docs"
    event = json.loads((docs / "job-event-v1.example.json").read_text())
    schema = json.loads((docs / "job-events-v1.schema.json").read_text())
    validate_event(event)
    assert set(schema["required"]) == set(event)
    assert set(schema["properties"]["status"]["enum"]) == STATUSES
    assert set(schema["properties"]["context"]["properties"]["reason_code"]["enum"]) == REASONS


@pytest.mark.parametrize("reference", [r"\\server\share\file", r"\\?\C:\file", r"C:\safe\..\file", r"C:\file:stream", "relative", "/tmp/file"])
def test_unsafe_consumer_references_rejected(reference):
    docs = Path(__file__).resolve().parents[1] / "docs"
    event = json.loads((docs / "job-event-v1.example.json").read_text())
    event["log_refs"] = [reference]
    with pytest.raises(ValueError):
        validate_event(event)
