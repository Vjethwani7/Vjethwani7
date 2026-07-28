"""Shared test fixtures."""

from __future__ import annotations

import pytest

from medic.core.config import Config
from medic.core.context import Context
from medic.core.journal import Journal
from medic.core.shell import CommandResult


class FakeContext(Context):
    """A Context whose command execution is scripted rather than real.

    Tests register expected command output with :meth:`stub`; anything not
    stubbed returns a "command not found" result, so a check that shells
    out unexpectedly fails loudly instead of touching the test machine.
    """

    def __init__(self, **kwargs) -> None:
        defaults = dict(
            os_name="linux",
            distro="Test Linux",
            init_system="systemd",
            arch="x86_64",
            hostname="testhost",
            user="tester",
            home="/home/tester",
            is_root=False,
            config=Config(),
        )
        defaults.update(kwargs)
        super().__init__(**defaults)
        self._stubs: dict[tuple[str, ...], CommandResult] = {}
        self._available: set[str] = set()
        self.files: dict[str, str] = {}
        self.commands_run: list[list[str]] = []

    def stub(self, argv: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self._stubs[tuple(argv)] = CommandResult(argv, returncode, stdout, stderr)
        self._available.add(argv[0])

    def provide(self, *names: str) -> None:
        """Mark executables as present without stubbing their output."""
        self._available.update(names)

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self._available else None

    def run(self, argv, *, cache=True, timeout=None, **kwargs) -> CommandResult:
        self.commands_run.append(list(argv))
        stub = self._stubs.get(tuple(argv))
        if stub is not None:
            return stub
        return CommandResult(list(argv), 127, "", "not found", not_found=True)

    def read_text(self, path: str, *, limit: int = 1_000_000):
        return self.files.get(path)


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(str(tmp_path / "journal.jsonl"))
