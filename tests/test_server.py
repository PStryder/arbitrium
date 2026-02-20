"""Tests for arbitrium.server — MCP tool handlers."""

import json

import pytest
import pytest_asyncio

from arbitrium import server as srv


@pytest.fixture(autouse=True)
def clean_sessions():
    """Ensure no leftover sessions between tests."""
    srv._sessions.clear()
    yield
    # Cleanup any sessions created during the test
    for s in list(srv._sessions.values()):
        if s.alive:
            import asyncio
            asyncio.get_event_loop().run_until_complete(s.close())
    srv._sessions.clear()


def _parse(result):
    """Extract JSON dict from handler result."""
    return json.loads(result[0].text)


class TestHandleSpawn:
    @pytest.mark.asyncio
    async def test_spawn_creates_session(self):
        result = await srv._handle_spawn({"session_id": "s1"})
        data = _parse(result)
        assert data["status"] == "spawned"
        assert "s1" in srv._sessions
        assert srv._sessions["s1"].alive
        await srv._sessions["s1"].close()

    @pytest.mark.asyncio
    async def test_spawn_duplicate_id_error(self):
        await srv._handle_spawn({"session_id": "dup"})
        result = await srv._handle_spawn({"session_id": "dup"})
        data = _parse(result)
        assert data["status"] == "error"
        assert "already exists" in data["error"]
        await srv._sessions["dup"].close()


class TestHandleExec:
    @pytest.mark.asyncio
    async def test_exec_nonexistent_session_error(self):
        result = await srv._handle_exec({"session_id": "nope", "command": "echo hi"})
        data = _parse(result)
        assert data["status"] == "error"
        assert "No session" in data["error"]


class TestHandleList:
    @pytest.mark.asyncio
    async def test_list_empty(self):
        result = await srv._handle_list({})
        data = _parse(result)
        assert data["total"] == 0
        assert data["sessions"] == []


class TestHandleClose:
    @pytest.mark.asyncio
    async def test_close_removes_session(self):
        await srv._handle_spawn({"session_id": "to-close"})
        assert "to-close" in srv._sessions
        result = await srv._handle_close({"session_id": "to-close"})
        data = _parse(result)
        assert data["status"] == "closed"
        assert "to-close" not in srv._sessions
