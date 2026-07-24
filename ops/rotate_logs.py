#!/usr/bin/env python3
"""
Iddo Harness — audit log rotation.

Rotates ~/.iddo-harness/audit.log once it exceeds a size threshold
(default 100MB): the current file is gzip-compressed and timestamped,
a fresh empty audit.log is left in place for the running agent to keep
writing to, and only the most recent N rotated archives are kept
(default 7 — older ones are deleted).

Designed to be run:
  - by hand:        python3 ops/rotate_logs.py
  - via cron:        0 * * * * /opt/iddo-harness/.venv/bin/python3 /opt/iddo-harness/ops/rotate_logs.py
  - via Task Sched:  hourly trigger, action = pythonw ops\\rotate_logs.py

Notes on the "keep writing to the same file" problem: this script does a
rename-then-recreate (like logrotate's `copytruncate`-free default), which
is safe for the agent's `logging.FileHandler` because Python's
FileHandler keeps the file open by *path* at handler-creation time, not by
inode — so after we rename the old file away and create a new empty one at
the same path, the *next* Python process restart will pick up the fresh
file. Since FileHandler doesn't reopen mid-run, this script also sends
SIGHUP-equivalent behavior isn't available cross-platform; instead we rely
on the fact this is normally invoked right before/after a service restart,
OR we truncate in place (safe for an *already open* file handle — the fd
stays valid, offset resets to 0) when the process is detected to still be
running. See `--mode` below.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_LOG_PATH = Path.home() / ".iddo-harness" / "audit.log"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100MB
DEFAULT_KEEP = 7


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _find_rotated_archives(log_path: Path) -> list[Path]:
    """Return existing rotated archives (audit.log.YYYYmmdd-HHMMSS.gz), oldest first."""
    pattern = f"{log_path.name}.*.gz"
    return sorted(log_path.parent.glob(pattern), key=lambda p: p.stat().st_mtime)


def rotate(log_path: Path, max_bytes: int, keep: int, mode: str, dry_run: bool = False) -> dict:
    """Perform rotation if needed. Returns a summary dict (also used for JSON output)."""
    summary = {
        "log_path": str(log_path),
        "action": "none",
        "reason": None,
        "size_before_bytes": None,
        "archived_to": None,
        "deleted_archives": [],
        "dry_run": dry_run,
    }

    if not log_path.exists():
        summary["reason"] = f"{log_path} does not exist — nothing to rotate"
        return summary

    size = log_path.stat().st_size
    summary["size_before_bytes"] = size

    if size <= max_bytes:
        summary["reason"] = f"{_human(size)} <= threshold {_human(max_bytes)} — no rotation needed"
        return summary

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = log_path.with_name(f"{log_path.name}.{timestamp}.gz")

    summary["action"] = "rotate"
    summary["reason"] = f"{_human(size)} > threshold {_human(max_bytes)}"
    summary["archived_to"] = str(archive_path)

    if dry_run:
        summary["action"] = "would_rotate"
        return summary

    if mode == "truncate":
        # Safe when the writer process keeps the fd open across our rotation
        # (Python logging.FileHandler holds the fd; truncating in place
        # resets the offset without invalidating the handle).
        with open(log_path, "rb") as src:
            with gzip.open(archive_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        with open(log_path, "r+b") as f:
            f.truncate(0)
    else:
        # "rename" mode: move the old file aside, gzip it, create a fresh
        # empty file at the original path. Requires the agent process to be
        # restarted (or to reopen the file) to pick up the new inode —
        # recommended to pair this with a scheduled `systemctl restart` /
        # Scheduled Task restart shortly after rotation, or just always use
        # --mode truncate for a live service (the default).
        tmp_path = log_path.with_suffix(log_path.suffix + ".rotating")
        os.replace(log_path, tmp_path)
        with open(tmp_path, "rb") as src:
            with gzip.open(archive_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        tmp_path.unlink()
        log_path.touch()
        try:
            shutil.chown(log_path, user=None, group=None)  # no-op placeholder; perms below
        except Exception:
            pass
        os.chmod(log_path, 0o640)

    # Prune old archives beyond `keep`.
    archives = _find_rotated_archives(log_path)
    if len(archives) > keep:
        to_delete = archives[: len(archives) - keep]
        for p in to_delete:
            p.unlink()
            summary["deleted_archives"].append(str(p))

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate the Iddo Harness audit log")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH),
                         help=f"path to audit.log (default: {DEFAULT_LOG_PATH})")
    parser.add_argument("--max-mb", type=float, default=DEFAULT_MAX_BYTES / (1024 * 1024),
                         help="rotate once the log exceeds this size in MB (default: 100)")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                         help="number of rotated archives to keep (default: 7)")
    parser.add_argument("--mode", choices=["truncate", "rename"], default="truncate",
                         help=(
                             "'truncate' (default): safe for a live running service — keeps the "
                             "same inode open, just resets its length after archiving. "
                             "'rename': classic logrotate-style swap; requires the writer to "
                             "reopen/restart to see the new file."
                         ))
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, change nothing")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    args = parser.parse_args()

    log_path = Path(args.log_path).expanduser()
    max_bytes = int(args.max_mb * 1024 * 1024)

    result = rotate(log_path, max_bytes, args.keep, args.mode, dry_run=args.dry_run)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
