"""Real harmless process-tree fixtures; never run production jobs or sessions."""
import ctypes as C
from ctypes import wintypes as W
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from arbitrium.jobs import Registry, Runner, Template
from arbitrium.winjob import OwnedProcess

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")


def environment(ref=None):
    return {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}


def await_file(path, timeout=5):
    until = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < until:
        time.sleep(.01)
    assert path.exists(), str(path)
    return path.read_text()


def process_handle(pid):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    # Read-only observation of our fixture, never signal a PID-loaded process.
    handle = kernel.OpenProcess(0x100000, False, pid)
    assert handle, C.get_last_error()
    return kernel, handle


def tree_fixture(tmp_path, delay=10):
    child = tmp_path / "child.py"
    child.write_text(f"import os,time\nfrom pathlib import Path\nPath('child.pid').write_text(str(os.getpid()))\ntime.sleep({delay})\nPath('child.done').write_text('done')\n")
    parent = tmp_path / "parent.py"
    parent.write_text("import subprocess,sys\nsubprocess.Popen([sys.executable, '-I', 'child.py'])\n")
    return parent, child


def test_root_exit_waits_for_descendant(tmp_path):
    parent, child = tree_fixture(tmp_path, .2)
    registry = Registry(tmp_path / "state")
    template = Template("tree", sys.executable, ("-I", str(parent)), (), str(tmp_path), "empty", 3, assets=(str(parent), str(child)))
    worker = Runner(registry, {"tree": template}, authorize=lambda j, r: True, resolve_environment=environment)
    result = worker.execute(registry.save(template))
    assert result["status"] == "completed"
    assert (tmp_path / "child.done").read_text() == "done"


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_root_exit_does_not_leave_descendant_after_cancel_or_deadline(tmp_path, mode):
    parent, child = tree_fixture(tmp_path)
    registry = Registry(tmp_path / "state")
    template = Template("tree", sys.executable, ("-I", str(parent)), (), str(tmp_path), "empty", 1, assets=(str(parent), str(child)))
    ids = []
    def approve(job, run_id):
        ids.append(run_id)
        return True
    worker = Runner(registry, {"tree": template}, authorize=approve, resolve_environment=environment)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.execute, registry.save(template))
        handle = None
        try:
            pid = int(await_file(tmp_path / "child.pid"))
            kernel, handle = process_handle(pid)
            if mode == "cancel":
                assert registry.cancel(ids[0])
            result = future.result(timeout=5)
            assert result["status"] == ("canceled" if mode == "cancel" else "failed")
            assert kernel.WaitForSingleObject(handle, 0) == 0
            assert not (tmp_path / "child.done").exists()
        finally:
            worker.stop()
            if handle:
                kernel.CloseHandle(handle)


def test_worker_crash_kills_tree_and_recovery_blocks_duplicate(tmp_path):
    parent, child = tree_fixture(tmp_path)
    worker = tmp_path / "worker.py"
    source = str(Path(__file__).resolve().parents[1] / "src")
    worker.write_text(
        "import sys\nsys.path.insert(0, " + repr(source) + ")\n"
        "from arbitrium.jobs import Registry,Template,Runner\nfrom pathlib import Path\n"
        "import os\n"
        "t=Template('tree',sys.executable,('-I',str(Path('parent.py').resolve())),(),str(Path.cwd()),'empty',30,assets=(str(Path('parent.py').resolve()),str(Path('child.py').resolve())))\n"
        "r=Registry(Path('state')); j=r.save(t); Path('job.id').write_text(j)\n"
        "Runner(r,{'tree':t},authorize=lambda j,r:True,resolve_environment=lambda _: {'SystemRoot':os.environ.get('SystemRoot','C:\\\\Windows')}).execute(j)\n"
    )
    owner = subprocess.Popen([sys.executable, "-I", str(worker)], cwd=tmp_path, env=environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handle = None
    try:
        pid = int(await_file(tmp_path / "child.pid"))
        kernel, handle = process_handle(pid)
        owner.kill()  # retained handle to this test's worker only
        owner.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 5000) == 0
        registry = Registry(tmp_path / "state")
        job_id = await_file(tmp_path / "job.id")
        with registry.connection() as db:
            db.execute("UPDATE runs SET lease_until=0")
        recovered = registry.recover()
        assert len(recovered) == 1 and registry.run(recovered[0])["status"] == "unknown"
        with pytest.raises(sqlite3.IntegrityError):
            registry.reserve(job_id, str(uuid.uuid4()))
        assert not (tmp_path / "child.done").exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if handle:
            kernel.CloseHandle(handle)


def test_resave_or_template_revision_cannot_bypass_unknown(tmp_path):
    program = tmp_path / "fixture.py"
    program.write_text("pass\n")
    template = Template("fixture", sys.executable, ("-I", str(program)), (), str(tmp_path), "empty", assets=(str(program),))
    registry = Registry(tmp_path / "state")
    job_id = registry.save(template)
    assert registry.save(template) == job_id
    owner = str(uuid.uuid4())
    run_id = registry.reserve(job_id, owner)
    registry.transition(run_id, owner, "running", "started")
    with registry.connection() as db:
        db.execute("UPDATE runs SET lease_until=0")
    registry.recover()
    revised = dataclasses.replace(template, timeout_seconds=1)
    revised_id = registry.save(revised)
    assert revised_id != job_id
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve(revised_id, str(uuid.uuid4()))
    alias = dataclasses.replace(template, cwd=str(tmp_path).swapcase().replace('\\', '/'))
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve(registry.save(alias), str(uuid.uuid4()))


def test_containment_setup_failure_never_launches(tmp_path, monkeypatch):
    from arbitrium import winjob
    class DeniedKernel:
        def CreateJobObjectW(self, *args):
            raise PermissionError("synthetic denial")
        def CreateProcessW(self, *args):
            raise AssertionError("Must never create an uncontained process")
    monkeypatch.setattr(winjob, "_kernel", lambda: DeniedKernel())
    with pytest.raises(PermissionError):
        OwnedProcess([sys.executable], cwd=str(tmp_path), env=environment())
