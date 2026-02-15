"""
Arbitrium MCP Server.

Persistent shell sessions for Claude Code. Spawn shells, execute commands,
read output — all across multiple turns with full session persistence.

Exposes 4 MCP tools:
  - arbitrium_spawn: Open a new shell session
  - arbitrium_exec: Execute a command in a session
  - arbitrium_list: List active sessions
  - arbitrium_close: Close a session
"""

import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from arbitrium.session import ShellSession

logger = logging.getLogger("arbitrium")

# Active sessions
_sessions: dict[str, ShellSession] = {}


def _create_server() -> Server:
    server = Server("arbitrium")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="arbitrium_spawn",
                description=(
                    "Open a new persistent shell session. The shell stays alive "
                    "across tool calls. Returns a session_id to use with arbitrium_exec. "
                    "All commands and output are logged to ./logs/."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Unique ID for this session. Defaults to auto-generated.",
                        },
                        "shell": {
                            "type": "string",
                            "description": "Shell to use (default: bash). Examples: bash, cmd, powershell.",
                            "default": "bash",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory for the shell. Defaults to current directory.",
                        },
                    },
                },
            ),
            Tool(
                name="arbitrium_exec",
                description=(
                    "Execute a command in a persistent shell session. Returns the "
                    "full output of the command and its exit code. The shell retains "
                    "state (environment variables, working directory) between calls."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "ID of the shell session to use.",
                        },
                        "command": {
                            "type": "string",
                            "description": "The command to execute.",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 30000).",
                            "default": 30000,
                        },
                    },
                    "required": ["session_id", "command"],
                },
            ),
            Tool(
                name="arbitrium_list",
                description="List all active shell sessions with their metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="arbitrium_close",
                description="Close a shell session and terminate the subprocess.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "ID of the session to close.",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "arbitrium_spawn":
            return await _handle_spawn(arguments)
        elif name == "arbitrium_exec":
            return await _handle_exec(arguments)
        elif name == "arbitrium_list":
            return await _handle_list(arguments)
        elif name == "arbitrium_close":
            return await _handle_close(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": f"Unknown tool: {name}",
            }))]

    return server


async def _handle_spawn(args: dict) -> list[TextContent]:
    """Spawn a new shell session."""
    import uuid

    session_id = args.get("session_id") or f"shell-{uuid.uuid4().hex[:6]}"
    shell = args.get("shell", "bash")
    cwd = args.get("cwd")

    if session_id in _sessions:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "error": f"Session '{session_id}' already exists.",
        }))]

    session = ShellSession(session_id=session_id, shell=shell, cwd=cwd)

    try:
        await session.start()
    except (OSError, FileNotFoundError) as e:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "error": f"Failed to start shell: {e}",
        }))]

    _sessions[session_id] = session

    return [TextContent(type="text", text=json.dumps({
        "status": "spawned",
        **session.info(),
    }))]


async def _handle_exec(args: dict) -> list[TextContent]:
    """Execute a command in a session."""
    session_id = args["session_id"]
    command = args["command"]
    timeout_ms = args.get("timeout_ms", 30000)

    if session_id not in _sessions:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "error": f"No session with ID '{session_id}'.",
        }))]

    session = _sessions[session_id]
    if not session.alive:
        del _sessions[session_id]
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "error": f"Session '{session_id}' has died.",
        }))]

    result = await session.execute(command, timeout_ms=timeout_ms)
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_list(args: dict) -> list[TextContent]:
    """List all active sessions."""
    # Clean up dead sessions
    dead = [sid for sid, s in _sessions.items() if not s.alive]
    for sid in dead:
        del _sessions[sid]

    sessions = [s.info() for s in _sessions.values()]
    return [TextContent(type="text", text=json.dumps({
        "sessions": sessions,
        "total": len(sessions),
    }, indent=2))]


async def _handle_close(args: dict) -> list[TextContent]:
    """Close a session."""
    session_id = args["session_id"]

    if session_id not in _sessions:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "error": f"No session with ID '{session_id}'.",
        }))]

    session = _sessions.pop(session_id)
    await session.close()

    return [TextContent(type="text", text=json.dumps({
        "status": "closed",
        "session_id": session_id,
        "commands_executed": session.command_count,
    }))]


async def _run():
    server = _create_server()
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("Arbitrium MCP server starting")
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        # Close all sessions on shutdown
        for session in _sessions.values():
            await session.close()


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
