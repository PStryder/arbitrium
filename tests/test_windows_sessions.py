import asyncio
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from arbitrium.session import ShellSession


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell protocol")
@pytest.mark.parametrize("shell", ["cmd.exe", "powershell.exe", "pwsh.exe"])
@pytest.mark.asyncio
async def test_windows_shell_state_exit_and_partial_line(shell, tmp_path):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip("Shell not installed")
    session = ShellSession("windows-fixture", executable, str(tmp_path))
    await session.start()
    try:
        first = await session.execute("echo hello", timeout_ms=5000)
        assert first["status"] == "ok", first
        assert first["exit_code"] == 0, first
        assert first["output"].strip() == "hello", first
        if shell == "cmd.exe":
            await session.execute("set ARBITRIUM_FIXTURE=retained")
            state = await session.execute("echo %ARBITRIUM_FIXTURE%")
            failure = await session.execute("cmd.exe /D /C exit 7")
            assert failure["exit_code"] == 7, failure
        else:
            await session.execute("$global:ArbitriumFixture = 'retained'")
            state = await session.execute("[Console]::Write($global:ArbitriumFixture)")
            failure = await session.execute("Write-Error 'fixture error'")
            assert failure["exit_code"] == 1, failure
        assert "retained" in state["output"], state
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_timeout_is_total_despite_output_and_no_stream_reuse(tmp_path):
    fixture = tmp_path / "chatter.py"
    fixture.write_text("import time\nfor i in range(12):\n print(i, flush=True)\n time.sleep(.04)\n")
    session = ShellSession("deadline-fixture")
    await session.start()
    try:
        before = time.monotonic()
        result = await session.execute(f'"{sys.executable}" "{fixture}"', timeout_ms=120)
        assert result["status"] == "timeout"
        assert time.monotonic() - before < .4
        assert (await session.execute("echo next"))["status"] == "error"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_cancelled_read_taints_stream(tmp_path):
    session = ShellSession("cancel-fixture")
    reading = asyncio.Event()
    async def blocked_read():
        reading.set()
        await asyncio.Future()
    session.process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()), stdout=SimpleNamespace(readline=blocked_read))
    task = asyncio.create_task(session.execute("echo fixture"))
    await asyncio.wait_for(reading.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await session.execute("echo next"))["status"] == "error"


@pytest.mark.asyncio
async def test_no_newline_output_is_preserved():
    session = ShellSession("no-newline")
    await session.start()
    try:
        result = await session.execute("printf fixture")
        assert result["output"] == "fixture"
        assert result["exit_code"] == 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_invalid_session_id_cannot_escape_log_directory():
    session = ShellSession("../escape")
    with pytest.raises(ValueError):
        await session.start()
    assert session.process is None
