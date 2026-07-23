"""
Policy engine — decides whether a task can run, needs confirmation, or is blocked.
"""
import fnmatch
import logging
import re
from enum import Enum
from pathlib import Path

log = logging.getLogger("harness.policy")


class Decision(Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    BLOCK = "block"


class PolicyEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    # -----------------------------------------------------------------------
    def decide(self, command: str, target_paths: list[str] | None = None) -> tuple[Decision, str]:
        """
        Returns (decision, reason).
        Order of evaluation: block → require_confirm → auto_allow → default require_confirm.
        """
        cmd = command.strip()

        # 1. blocked?
        for pat in self.cfg.block.get("commands", []):
            if fnmatch.fnmatchcase(cmd, pat):
                return Decision.BLOCK, f"blocked by pattern: {pat}"

        for path in target_paths or []:
            for pat in self.cfg.block.get("paths", {}).get("absolute_no_touch", []):
                if fnmatch.fnmatchcase(path, pat):
                    return Decision.BLOCK, f"path blocked: {pat}"

        # 2. requires confirm?
        for pat in self.cfg.require_confirm.get("commands", []):
            if fnmatch.fnmatchcase(cmd, pat):
                return Decision.CONFIRM, f"requires confirmation: {pat}"

        # 3. auto-allowed?
        for pat in self.cfg.auto_allow.get("commands", []):
            if fnmatch.fnmatchcase(cmd, pat):
                return Decision.AUTO, f"auto-allowed: {pat}"

        # default — safety first
        return Decision.CONFIRM, "no matching rule (defaulting to confirmation)"

    # -----------------------------------------------------------------------
    def audit(self, task_id: str, command: str, decision: Decision, reason: str):
        log.info(f"task={task_id} decision={decision.value} cmd={command!r} reason={reason}")
