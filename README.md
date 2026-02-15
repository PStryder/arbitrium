# Arbitrium

> *From Latin **arbitrium** — free judgment, mastery, control. The power to decide and command. Where Velle wills and Expergis perceives, Arbitrium commands.*

**Persistent shell MCP server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).**

Arbitrium gives the agent persistent shell sessions that survive across tool calls. Spawn a shell, run commands, read output — the shell retains all state (environment variables, working directory, running processes) between interactions. Every command and its output is logged to `./logs/`.

## How It Works

```
arbitrium_spawn  ->  bash subprocess with stdin/stdout pipes
arbitrium_exec   ->  write command + sentinel to stdin, read output until sentinel
arbitrium_close  ->  graceful termination
```

No console injection, no buffer scraping. Just pipes and a sentinel pattern to detect command completion. The shell is headless — output flows back through the MCP tool response.

## MCP Tools

| Tool | Description |
|------|-------------|
| `arbitrium_spawn` | Open a new persistent shell session |
| `arbitrium_exec` | Execute a command and return full output + exit code |
| `arbitrium_list` | List active sessions with metadata |
| `arbitrium_close` | Close a session and terminate the subprocess |

## Key Features

- **State persistence** — `cd`, `export`, aliases, shell functions all survive across calls
- **Full output capture** — returns everything between command and next prompt, not just a truncated window
- **Exit codes** — every command returns its exit code
- **Timeout handling** — configurable per-command timeout with partial output on timeout
- **Session logging** — every session writes a full transcript to `./logs/`
- **Multiple sessions** — run several shells concurrently with different working directories

## Installation

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/PStryder/arbitrium.git
cd arbitrium
uv sync
```

Add to Claude Code:

```bash
claude mcp add -s user arbitrium -- /path/to/arbitrium/.venv/Scripts/python -m arbitrium.server
```

Or manually in `~/.claude.json`:

```json
{
  "mcpServers": {
    "arbitrium": {
      "command": "/path/to/arbitrium/.venv/Scripts/python",
      "args": ["-m", "arbitrium.server"],
      "cwd": "/path/to/arbitrium"
    }
  }
}
```

## Usage

```
Agent: arbitrium_spawn(shell="bash", cwd="/my/project")
  -> session_id: "shell-a1b2c3"

Agent: arbitrium_exec(session_id="shell-a1b2c3", command="export API_KEY=test123")
  -> ok, exit_code: 0

Agent: arbitrium_exec(session_id="shell-a1b2c3", command="echo $API_KEY")
  -> output: "test123", exit_code: 0    # state persisted!

Agent: arbitrium_exec(session_id="shell-a1b2c3", command="cd src && ls")
  -> output: "main.py  utils.py", exit_code: 0

Agent: arbitrium_exec(session_id="shell-a1b2c3", command="pwd")
  -> output: "/my/project/src", exit_code: 0    # directory persisted!

Agent: arbitrium_close(session_id="shell-a1b2c3")
  -> closed, 4 commands executed
```

## Security

Arbitrium provides **unrestricted shell access** — any command the agent sends will execute with the permissions of the user running Claude Code. There is no sandboxing.

All commands and output are logged to `./logs/` with timestamps. Review logs to audit agent behavior.

## The Triad

| Tool | Role | Latin |
|------|------|-------|
| [Velle](https://github.com/PStryder/Velle) | Volition — self-prompting | *to will* |
| [Expergis](https://github.com/PStryder/expergis) | Perception — event detection | *to wake up* |
| **Arbitrium** | Agency — environmental control | *free judgment* |

## License

[Apache 2.0](LICENSE)
