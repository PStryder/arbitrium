"""Persistent shell session management."""

import asyncio
import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("arbitrium.session")

# Sentinel used to detect command completion
_SENTINEL_PREFIX = "__ARBITRIUM_DONE_"

# Log directory
LOGS_DIR = Path(__file__).parent.parent.parent / "logs"


def detect_shell() -> str:
    """Auto-detect the best available shell for the current platform.

    On Windows, probes in order of preference:
      1. Git Bash (bin/bash.exe — has full PATH with ls, git, etc.)
      2. Git Bash (usr/bin/bash.exe — works but minimal PATH)
      3. PowerShell 7+ (pwsh)
      4. PowerShell 5 (powershell)
      5. cmd.exe (always available)

    On Unix, uses $SHELL or falls back to /bin/sh.
    """
    if sys.platform != "win32":
        return os.environ.get("SHELL", "/bin/sh")

    # Git Bash — prefer bin/bash.exe (full PATH) over usr/bin/bash.exe
    # Use PROGRAMFILES env var for non-standard install locations
    program_dirs = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    for pdir in program_dirs:
        for subpath in [r"Git\bin\bash.exe", r"Git\usr\bin\bash.exe"]:
            candidate = os.path.join(pdir, subpath)
            if os.path.isfile(candidate):
                logger.info(f"Detected shell: {candidate}")
                return candidate

    # bash on PATH (e.g. WSL, MSYS2)
    for name in ["bash.exe", "bash"]:
        bash_path = shutil.which(name)
        if bash_path:
            logger.info(f"Detected shell: {bash_path}")
            return bash_path

    # PowerShell 7+
    pwsh_path = shutil.which("pwsh")
    if pwsh_path:
        logger.info(f"Detected shell: {pwsh_path}")
        return pwsh_path

    # PowerShell 5 (ships with Windows)
    ps_path = shutil.which("powershell")
    if ps_path:
        logger.info(f"Detected shell: {ps_path}")
        return ps_path

    # cmd.exe — always available
    logger.info("Detected shell: cmd.exe (fallback)")
    return "cmd.exe"


class ShellSession:
    """A persistent shell subprocess with stdin/stdout pipes."""

    def __init__(
        self,
        session_id: str,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.session_id = session_id
        self.shell = shell or detect_shell()
        self.cwd = cwd or os.getcwd()
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.command_count: int = 0
        self.last_command: str | None = None
        self._log_file = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the shell subprocess."""
        shell_env = os.environ.copy()
        if self.env:
            shell_env.update(self.env)

        # Remove CLAUDECODE env var so nested claude calls don't fail
        shell_env.pop("CLAUDECODE", None)

        self.process = await asyncio.create_subprocess_exec(
            self.shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            cwd=self.cwd,
            env=shell_env,
        )

        # Set up log file
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"{self.session_id}_{timestamp}.log"
        self._log_file = open(log_path, "a", encoding="utf-8")
        self._log(f"Session started: shell={self.shell}, cwd={self.cwd}")

        logger.info(f"Shell session '{self.session_id}' started (PID {self.process.pid})")

    @staticmethod
    def _fix_powershell_quoting(command: str) -> str:
        """Auto-fix PowerShell commands to use single quotes around -Command.

        Bash expands $ variables in double-quoted strings, which breaks
        PowerShell's $_ and other PS variables. This detects patterns like:
            powershell.exe -Command "... $_ ..."
        and rewrites to:
            powershell.exe -Command '... $_ ...'
        with internal single quotes escaped as '\\''
        """
        import re

        # Match powershell/pwsh with -Command followed by a double-quoted string
        # that contains $ (indicating PS variables that bash would expand)
        pattern = r'((?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(?:-\w+\s+)*-[Cc]ommand\s+)"((?:[^"\\]|\\.)*\$(?:[^"\\]|\\.)*)"'

        def replace_quotes(m):
            prefix = m.group(1)
            inner = m.group(2)
            # Escape any single quotes inside the command
            inner = inner.replace("'", "'\\''")
            return f"{prefix}'{inner}'"

        return re.sub(pattern, replace_quotes, command)

    async def execute(self, command: str, timeout_ms: int = 30000) -> dict[str, Any]:
        """Execute a command and return its full output."""
        if not self.process or self.process.returncode is not None:
            return {"status": "error", "error": "Shell session is not running"}

        # Auto-fix PowerShell quoting to prevent bash $ expansion
        command = self._fix_powershell_quoting(command)

        async with self._lock:
            sentinel = f"{_SENTINEL_PREFIX}{uuid.uuid4().hex[:8]}"

            # Write command + sentinel echo to stdin
            # The sentinel echo lets us detect when the command finishes
            # We also echo the exit code before the sentinel
            #
            # Problem: bash sets $_ to the last arg of the previous command.
            # After our sentinel echo, $_ contains the sentinel string, which
            # leaks into the next command if it uses $_.
            #
            # Fix: `: _` on its own line before the command resets $_ to "_".
            # Bash parses each line separately, so by the time it parses the
            # user's command, $_ is already clean. Same after the sentinel.
            cmd_line = f": _\n{command}\necho $?:{sentinel}\n: _\n"
            self.process.stdin.write(cmd_line.encode())
            await self.process.stdin.drain()

            self._log(f"$ {command}")

            # Read output until sentinel appears
            output_lines: list[str] = []
            exit_code: int | None = None
            timeout_sec = timeout_ms / 1000.0

            try:
                while True:
                    line_bytes = await asyncio.wait_for(
                        self.process.stdout.readline(),
                        timeout=timeout_sec,
                    )

                    if not line_bytes:
                        # EOF — process died
                        break

                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

                    if sentinel in line:
                        # Extract exit code from "exitcode:sentinel" format
                        prefix = line.split(sentinel)[0].rstrip(":")
                        try:
                            exit_code = int(prefix)
                        except ValueError:
                            exit_code = None
                        break

                    output_lines.append(line)

            except asyncio.TimeoutError:
                output = "\n".join(output_lines)
                self._log(f"[TIMEOUT after {timeout_ms}ms]\n{output}")
                self.command_count += 1
                self.last_command = command
                return {
                    "status": "timeout",
                    "output": output,
                    "timeout_ms": timeout_ms,
                    "command": command,
                }

            output = "\n".join(output_lines)
            self._log(f"{output}\n[exit: {exit_code}]")

            self.command_count += 1
            self.last_command = command

            return {
                "status": "ok",
                "output": output,
                "exit_code": exit_code,
                "command": command,
            }

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self) -> None:
        """Terminate the shell session."""
        if self.process and self.process.returncode is None:
            self.process.stdin.write(b"exit\n")
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

        self._log("Session closed")
        if self._log_file:
            self._log_file.close()
            self._log_file = None

        logger.info(f"Shell session '{self.session_id}' closed")

    def _log(self, message: str) -> None:
        """Write to the session log file."""
        if self._log_file:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            self._log_file.write(f"[{ts}] {message}\n")
            self._log_file.flush()

    def info(self) -> dict[str, Any]:
        """Return session metadata."""
        return {
            "session_id": self.session_id,
            "shell": self.shell,
            "cwd": self.cwd,
            "alive": self.alive,
            "pid": self.process.pid if self.process else None,
            "command_count": self.command_count,
            "last_command": self.last_command,
            "created_at": self.created_at,
        }
