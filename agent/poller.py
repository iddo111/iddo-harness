"""
GitHub-based task poller.
Pulls task packets from tasks/*.json in the bridge repo.
"""
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("harness.poller")


@dataclass
class Task:
    id: str
    kind: str  # "shell" | "read_file" | "write_file" | "list_dir"
    payload: dict = field(default_factory=dict)
    priority: str = "normal"
    source_path: Path | None = None


class GithubPoller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg.transport["repo"]
        self.task_dir = cfg.transport.get("task_dir", "tasks/")
        self.result_dir = cfg.transport.get("result_dir", "results/")
        self._local = Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{self.repo.replace('/', '_')}"

    # -----------------------------------------------------------------------
    def _sync(self):
        """git pull the bridge repo into a temp workdir."""
        if not self._local.exists():
            log.info(f"Cloning bridge repo {self.repo} → {self._local}")
            subprocess.run(
                ["gh", "repo", "clone", self.repo, str(self._local)],
                check=True, capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(self._local), "pull", "--quiet"],
                check=False, capture_output=True,
            )

    # -----------------------------------------------------------------------
    def fetch_pending_tasks(self) -> list[Task]:
        try:
            self._sync()
        except Exception as e:
            log.warning(f"Bridge sync failed: {e}")
            return []

        task_folder = self._local / self.task_dir
        task_folder.mkdir(parents=True, exist_ok=True)
        tasks = []
        for p in sorted(task_folder.glob("*.json")):
            if p.name.startswith("done-"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tasks.append(Task(
                    id=data.get("id", p.stem),
                    kind=data.get("kind", "shell"),
                    payload=data.get("payload", {}),
                    priority=data.get("priority", "normal"),
                    source_path=p,
                ))
            except Exception as e:
                log.error(f"Bad task {p}: {e}")
        return tasks

    # -----------------------------------------------------------------------
    def mark_done(self, task: Task):
        if task.source_path and task.source_path.exists():
            new = task.source_path.parent / f"done-{task.source_path.name}"
            task.source_path.rename(new)
            self._commit_and_push(f"mark done: {task.id}")

    def mark_failed(self, task: Task):
        if task.source_path and task.source_path.exists():
            new = task.source_path.parent / f"failed-{task.source_path.name}"
            task.source_path.rename(new)
            self._commit_and_push(f"mark failed: {task.id}")

    # -----------------------------------------------------------------------
    def _commit_and_push(self, msg: str):
        subprocess.run(["git", "-C", str(self._local), "add", "-A"], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(self._local), "commit", "-m", msg, "--allow-empty"], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(self._local), "push", "--quiet"], check=False, capture_output=True)
