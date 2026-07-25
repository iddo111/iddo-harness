"""
Shared test doubles for the v3 test modules.

Kept out of conftest.py so the modules can import them explicitly — ``tests``
is a package, so a bare ``from conftest import ...`` does not resolve.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeTask:
    """Minimal stand-in for agent.poller.Task, shared by the v3 test modules."""

    id: str
    kind: str = "shell"
    payload: dict = field(default_factory=dict)
    priority: str = "normal"
    envelope: Any = None
    source_path: Any = None


class StubReporter:
    """Records what would have been written, without touching git or the disk.

    Mirrors the ReporterV2 surface the runner uses. Tests assert against
    ``chunks`` / ``attempts`` / ``results`` instead of reading files back,
    which keeps them fast and independent of the on-disk layout.
    """

    def __init__(self) -> None:
        self.chunks: list[tuple[str, dict, int, bool]] = []
        self.attempts: list[tuple[str, Any, int]] = []
        self.results: list[tuple[str, Any]] = []
        self.errors: list[tuple[str, str]] = []

    def send_chunk(self, task, body: dict, seq: int, is_final: bool):
        self.chunks.append((str(task.id), dict(body), seq, is_final))
        return None

    def send(self, task, result):
        self.results.append((str(task.id), result))
        return None

    def send_attempt(self, task, result, attempt: int):
        self.attempts.append((str(task.id), result, attempt))
        return None

    def send_error(self, task, message: str):
        self.errors.append((str(task.id), message))
        return None

    def final_chunks(self) -> list[tuple[str, dict]]:
        return [(tid, body) for tid, body, _seq, is_final in self.chunks if is_final]
