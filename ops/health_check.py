#!/usr/bin/env python3
"""
Iddo Harness — standalone health probe.

Runs a handful of independent checks against a live (or supposedly live)
installation and prints a single JSON status blob. Designed to be run:

  - by hand:            python3 ops/health_check.py --config /etc/iddo-harness/policy.yaml
  - from cron/Task Sched: on a schedule, alerting on non-zero exit
  - by monitoring tools:  parse the JSON on stdout

Checks performed:
  1. github        — can we reach GitHub / the configured bridge repo at all?
  2. disk_space    — is there enough free space on the task_queue path's volume?
  3. last_poll     — how long ago did the agent last poll (via ~/.iddo-harness/last_poll,
                     falling back to the audit log's last "Polling" line, then to
                     agent.lock's mtime as a coarse liveness signal)?
  4. audit_growth  — has the audit log grown at all in the last hour? (0 growth => warn)

Exit code is 0 only if every check that ran is "ok". Checks that could not
determine a definitive answer are reported as "warn" and do not fail the
overall run by themselves — only explicit "fail" checks do. Use --strict to
also fail the run on any "warn".

This script intentionally does NOT import anything from agent/ — it is a
fully standalone probe that only needs pyyaml (optional; falls back to a
tiny hand-rolled scanner for the couple of top-level keys it needs if
PyYAML isn't installed in whatever Python is running it).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_CANDIDATES = [
    Path("/etc/iddo-harness/policy.yaml"),
    Path.home() / ".iddo-harness" / "policy.yaml",
]

STATE_DIR = Path.home() / ".iddo-harness"
AUDIT_LOG = STATE_DIR / "audit.log"
LOCK_FILE = STATE_DIR / "agent.lock"
LAST_POLL_FILE = STATE_DIR / "last_poll"

AUDIT_GROWTH_WARN_SECONDS = 3600  # 1 hour
LAST_POLL_WARN_SECONDS = 120      # policy default interval is 5s; 2 min of silence is suspicious
DISK_SPACE_WARN_MB = 500
DISK_SPACE_FAIL_MB = 100


# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _load_minimal_config(path: Optional[Path]) -> dict:
    """Load just the bits of policy.yaml this script needs (transport.repo,
    paths.task_queue). Uses PyYAML if available, else a tiny fallback parser
    good enough for this flat-ish config file."""
    candidates = [path] if path else DEFAULT_CONFIG_CANDIDATES
    for p in candidates:
        if p and Path(p).exists():
            text = Path(p).read_text(encoding="utf-8")
            try:
                import yaml  # type: ignore
                return yaml.safe_load(text) or {}
            except ImportError:
                return _fallback_parse(text)
    return {}


def _fallback_parse(text: str) -> dict:
    """Extract transport.repo and paths.task_queue without PyYAML.
    Good-enough regex/line scanner for this one file's shape."""
    import re
    data: dict[str, Any] = {"transport": {}, "paths": {}}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("transport:"):
            section = "transport"
            continue
        if line.startswith("paths:"):
            section = "paths"
            continue
        if not line.startswith(" ") and ":" in line:
            section = None
            continue
        m = re.match(r"^\s+(\w+):\s*\"?([^\"#]+)\"?", line)
        if m and section:
            key, val = m.group(1), m.group(2).strip()
            data[section][key] = val
    return data


def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
def check_github(repo: Optional[str]) -> dict:
    """Check basic connectivity to GitHub, and to the configured bridge repo if known."""
    result = {"name": "github", "status": "ok", "detail": {}}

    # 1. Generic reachability (api.github.com), works without any auth.
    try:
        proc = _run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "8", "https://api.github.com"])
        code = proc.stdout.strip()
        result["detail"]["api_github_com_http_code"] = code
        if proc.returncode != 0 or not code.isdigit():
            result["status"] = "fail"
            result["detail"]["error"] = f"curl failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
            return result
        if int(code) >= 500:
            result["status"] = "fail"
            result["detail"]["error"] = f"api.github.com returned HTTP {code}"
            return result
    except FileNotFoundError:
        result["status"] = "warn"
        result["detail"]["error"] = "curl not found — cannot verify GitHub reachability"
        return result
    except subprocess.TimeoutExpired:
        result["status"] = "fail"
        result["detail"]["error"] = "timed out reaching api.github.com"
        return result
    except Exception as e:
        result["status"] = "fail"
        result["detail"]["error"] = str(e)
        return result

    # 2. Bridge repo specifically, via gh CLI if available (mirrors what the
    #    agent itself uses in poller.py/reporter.py).
    if repo:
        result["detail"]["bridge_repo"] = repo
        gh = shutil.which("gh")
        if gh:
            try:
                proc = _run([gh, "repo", "view", repo, "--json", "name"], timeout=15)
                if proc.returncode == 0:
                    result["detail"]["bridge_repo_reachable"] = True
                else:
                    result["status"] = "fail"
                    result["detail"]["bridge_repo_reachable"] = False
                    result["detail"]["error"] = (proc.stderr or proc.stdout).strip()[:300]
            except Exception as e:
                result["status"] = "warn"
                result["detail"]["error"] = f"gh repo view failed: {e}"
        else:
            result["status"] = "warn"
            result["detail"]["error"] = "gh CLI not found — could not verify bridge repo access specifically"

    return result


# ---------------------------------------------------------------------------
def check_disk_space(task_queue_path: Optional[str]) -> dict:
    result = {"name": "disk_space", "status": "ok", "detail": {}}
    target = Path(task_queue_path).expanduser() if task_queue_path else STATE_DIR
    # Walk up to the nearest existing ancestor so this works even before the
    # queue dir has been created yet.
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    try:
        usage = shutil.disk_usage(probe)
        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)
        result["detail"] = {
            "path_checked": str(probe),
            "free_mb": round(free_mb, 1),
            "total_mb": round(total_mb, 1),
            "percent_free": round(100 * usage.free / usage.total, 1) if usage.total else None,
        }
        if free_mb < DISK_SPACE_FAIL_MB:
            result["status"] = "fail"
            result["detail"]["error"] = f"only {free_mb:.0f}MB free (< {DISK_SPACE_FAIL_MB}MB threshold)"
        elif free_mb < DISK_SPACE_WARN_MB:
            result["status"] = "warn"
            result["detail"]["warning"] = f"only {free_mb:.0f}MB free (< {DISK_SPACE_WARN_MB}MB threshold)"
    except Exception as e:
        result["status"] = "fail"
        result["detail"]["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
def _extract_last_poll_from_audit_log() -> Optional[float]:
    """Best-effort: scan audit.log tail for the most recent 'Polling' or
    poller-related log line and parse its timestamp."""
    if not AUDIT_LOG.exists():
        return None
    try:
        # Only look at the tail — audit logs can get large; rotate_logs.py
        # keeps this in check but be defensive anyway.
        with open(AUDIT_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", errors="replace")
        last_ts = None
        for line in tail.splitlines():
            if "harness.poller" in line or "harness.main" in line:
                ts_str = line.split(" [")[0].strip()
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    last_ts = dt.timestamp()
                except ValueError:
                    continue
        return last_ts
    except Exception:
        return None


def check_last_poll() -> dict:
    result = {"name": "last_poll", "status": "ok", "detail": {}}

    ts: Optional[float] = None
    source = None

    if LAST_POLL_FILE.exists():
        try:
            raw = LAST_POLL_FILE.read_text(encoding="utf-8").strip()
            ts = float(raw)
            source = str(LAST_POLL_FILE)
        except Exception:
            try:
                ts = LAST_POLL_FILE.stat().st_mtime
                source = f"{LAST_POLL_FILE} (mtime fallback)"
            except Exception:
                ts = None

    if ts is None:
        ts = _extract_last_poll_from_audit_log()
        if ts is not None:
            source = f"{AUDIT_LOG} (parsed)"

    if ts is None and LOCK_FILE.exists():
        try:
            ts = LOCK_FILE.stat().st_mtime
            source = f"{LOCK_FILE} (mtime — coarse liveness only)"
        except Exception:
            ts = None

    if ts is None:
        result["status"] = "warn"
        result["detail"]["error"] = (
            f"no last_poll signal found (checked {LAST_POLL_FILE}, {AUDIT_LOG}, {LOCK_FILE})"
        )
        return result

    age_seconds = time.time() - ts
    result["detail"] = {
        "source": source,
        "last_poll_iso": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_seconds": round(age_seconds, 1),
    }
    if age_seconds > LAST_POLL_WARN_SECONDS:
        result["status"] = "warn"
        result["detail"]["warning"] = (
            f"last poll was {age_seconds:.0f}s ago (> {LAST_POLL_WARN_SECONDS}s threshold) — "
            f"agent may be stuck or stopped"
        )
    return result


# ---------------------------------------------------------------------------
def check_audit_growth() -> dict:
    result = {"name": "audit_growth", "status": "ok", "detail": {}}
    if not AUDIT_LOG.exists():
        result["status"] = "warn"
        result["detail"]["error"] = f"audit log not found at {AUDIT_LOG}"
        return result

    try:
        stat = AUDIT_LOG.stat()
        age_since_modified = time.time() - stat.st_mtime
        result["detail"] = {
            "path": str(AUDIT_LOG),
            "size_bytes": stat.st_size,
            "seconds_since_last_write": round(age_since_modified, 1),
        }
        if age_since_modified > AUDIT_GROWTH_WARN_SECONDS:
            result["status"] = "warn"
            result["detail"]["warning"] = (
                f"audit log has not grown in {age_since_modified/60:.0f} minutes "
                f"(> {AUDIT_GROWTH_WARN_SECONDS/60:.0f}min threshold) — agent may be idle/stuck"
            )
    except Exception as e:
        result["status"] = "fail"
        result["detail"]["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Iddo Harness health probe")
    parser.add_argument("--config", default=None, help="path to policy.yaml")
    parser.add_argument("--strict", action="store_true",
                         help="treat warnings as failures for the exit code")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON (default: compact)")
    args = parser.parse_args()

    cfg = _load_minimal_config(Path(args.config) if args.config else None)
    repo = (cfg.get("transport") or {}).get("repo")
    task_queue = (cfg.get("paths") or {}).get("task_queue")

    checks = [
        check_github(repo),
        check_disk_space(task_queue),
        check_last_poll(),
        check_audit_growth(),
    ]

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "ok"

    report = {
        "timestamp": _now_iso(),
        "overall_status": overall,
        "checks": checks,
    }

    print(json.dumps(report, indent=2 if args.pretty else None))

    if overall == "fail":
        return 2
    if overall == "warn" and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
