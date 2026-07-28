"""A thin, defensive wrapper around subprocess.

Every external command medic runs goes through :func:`run`. It never uses a
shell, always has a timeout, and never raises on non-zero exit - callers
inspect the result. That keeps a missing or misbehaving system tool from
taking down a diagnostic run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 15.0


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found

    @property
    def failure_reason(self) -> str:
        if self.not_found:
            return f"{self.argv[0]}: command not found"
        if self.timed_out:
            return f"{' '.join(self.argv)}: timed out"
        if self.returncode != 0:
            detail = (self.stderr or self.stdout).strip().splitlines()
            tail = detail[-1] if detail else ""
            return f"exit {self.returncode}{': ' + tail if tail else ''}"
        return ""

    def lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


def which(name: str) -> str | None:
    """Absolute path to an executable, or None."""
    return shutil.which(name)


def run(
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    stdin_text: str | None = None,
    env_extra: dict[str, str] | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run a command and capture its output. Never raises for command failure."""
    if not argv:
        raise ValueError("argv must not be empty")

    env = dict(os.environ)
    # Predictable, parseable output regardless of the user's locale.
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")
    # Stop apt/dnf and friends from trying to open a pager or prompt.
    env.setdefault("DEBIAN_FRONTEND", "noninteractive")
    env["SYSTEMD_PAGER"] = ""
    env["PAGER"] = "cat"
    if env_extra:
        env.update(env_extra)

    try:
        completed = subprocess.run(  # noqa: S603 - argv is never shell-interpreted
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            input=stdin_text,
            env=env,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(argv, 127, "", f"{argv[0]}: not found", not_found=True)
    except PermissionError as exc:
        return CommandResult(argv, 126, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return CommandResult(argv, 124, stdout, stderr, timed_out=True)
    except OSError as exc:
        return CommandResult(argv, 125, "", str(exc))

    return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)
