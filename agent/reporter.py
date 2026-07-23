"""
Reporter — writes results back to the bridge repo.
"""
import json
import logging
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

log = logging.getLogger("harness.reporter")


class Reporter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg.transport["repo"]
        self.result_dir = cfg.transport.get("result_dir", "results/")
        self._local = Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{self.repo.replace('/', '_')}"

    # -----------------------------------------------------------------------
    def send(self, task, result):
        self._write(task, asdict(result))

    def send_error(self, task, err_msg: str):
        self._write(task, {"task_id": task.id, "ok": False, "error": err_msg, "decision": "error"})

    # -----------------------------------------------------------------------
    def _write(self, task, payload: dict):
        out_dir = self._local / self.result_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{task.id}.json"
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "-C", str(self._local), "add", str(p)], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(self._local), "commit", "-m", f"result: {task.id}"], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(self._local), "push", "--quiet"], check=False, capture_output=True)
        log.info(f"reported {task.id}: ok={payload.get('ok')} decision={payload.get('decision')}")
