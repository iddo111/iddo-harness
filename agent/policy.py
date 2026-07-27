"""
Policy engine — decides whether a task can run, needs confirmation, or is blocked.
"""
import fnmatch
import logging
import os
import re
from enum import Enum
from pathlib import Path

try:
    from secrets_vault import mask_text
except ImportError:  # pragma: no cover - packaged imports
    from agent.secrets_vault import mask_text

log = logging.getLogger("harness.policy")

# v2 kinds have no real command line, so the executors synthesise one
# (`grep <path>`, `patch <path>`, `read_chunk <path>`, …). These verb lists let
# decide() apply the path-glob rules that policy.yaml has always declared under
# `auto_allow.paths.read` / `require_confirm.paths.write`. See docs/v2_spec.md §5.
READ_VERBS = frozenset(
    {"cat", "type", "head", "tail", "read", "read_chunk", "list", "ls", "dir", "grep", "glob", "watch_start", "watch_poll"}
)
WRITE_VERBS = frozenset({"write", "patch", "rm", "del", "mv", "move", "cp", "copy"})


def _path_matches(path: str, pattern: str) -> bool:
    """Glob-match a target path against a policy path pattern.

    Beyond plain `fnmatch`, this normalises the three things that bite in a
    mixed Windows/POSIX policy file: `~` and `%USERNAME%` are expanded,
    separators are unified, and a trailing `/**` also matches the root itself
    (so `D:\\CLAUDE\\**` covers `D:\\CLAUDE`). Matching is case-insensitive
    because Windows paths in policy.yaml rarely match the casing the OS reports.
    """
    expanded = os.path.expandvars(os.path.expanduser(pattern))
    norm_pat = expanded.replace("\\", "/").lower()
    norm_path = os.path.expanduser(path).replace("\\", "/").rstrip("/").lower()

    if fnmatch.fnmatchcase(norm_path, norm_pat):
        return True
    if norm_pat.endswith("/**") and norm_path == norm_pat[:-3]:
        return True
    return False


class Decision(Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    BLOCK = "block"


class PolicyEngine:
    def __init__(self, cfg, audit_log=None):
        self.cfg = cfg
        # v3 Track B: when wired up, every decision also lands in the
        # hash-chained audit log with the rule that produced it. None keeps the
        # v1/v2 behaviour of logging to the python logger only.
        self.audit_log = audit_log

    # -----------------------------------------------------------------------
    def decide(self, command: str, target_paths: list[str] | None = None) -> tuple[Decision, str]:
        """
        Returns (decision, reason).

        Order of evaluation:
        block commands → block paths → confirm commands → **confirm write paths**
        → auto commands → **auto read paths** → default confirm.

        The two path steps are v2 additions (docs/v2_spec.md §5). They are
        additive: a v1 read verb already matched an `auto_allow.commands`
        pattern before reaching the read-path step, and a v1 write verb already
        fell through to the default CONFIRM, so no v1 decision changes.
        """
        # Vault references are matched in their masked form: `{{secret:foo}}`
        # becomes `<vault:foo>`, so the `*secret*` / `*token*` / `*password*`
        # block patterns keep rejecting a credential pasted inline while a
        # reference to the encrypted vault passes. Using the vault is the
        # sanctioned way to pass a credential — see docs/security_v3.md §3.
        cmd = mask_text(command.strip())
        verb = cmd.split(maxsplit=1)[0].lower() if cmd else ""

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

        # 3. write verb touching a confirm-gated path?
        if verb in WRITE_VERBS:
            for path in target_paths or []:
                for pat in self.cfg.require_confirm.get("paths", {}).get("write", []):
                    if _path_matches(path, pat):
                        return Decision.CONFIRM, f"write requires confirmation: {pat}"

        # 4. auto-allowed?
        for pat in self.cfg.auto_allow.get("commands", []):
            if fnmatch.fnmatchcase(cmd, pat):
                return Decision.AUTO, f"auto-allowed: {pat}"

        # 5. read verb, and every target path sits inside an allowed read root?
        read_roots = self.cfg.auto_allow.get("paths", {}).get("read", [])
        if verb in READ_VERBS and target_paths and read_roots:
            if all(any(_path_matches(p, pat) for pat in read_roots) for p in target_paths):
                return Decision.AUTO, "auto-allowed: read path"

        # default — safety first
        return Decision.CONFIRM, "no matching rule (defaulting to confirmation)"

    # -----------------------------------------------------------------------
    def audit(self, task_id: str, command: str, decision: Decision, reason: str):
        """Log a decision, and append it to the audit chain when one is wired up.

        ``command`` is masked before it goes anywhere: this is the exact line a
        human reads later, which makes it the last place a credential should
        appear.
        """
        safe = mask_text(command)
        log.info(f"task={task_id} decision={decision.value} cmd={safe!r} reason={reason}")
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(
                actor=str(task_id),
                action=f"policy_decision:{decision.value}",
                resource=safe,
                outcome="deny" if decision is Decision.BLOCK else "ok",
                meta={"rule": reason, "decision": decision.value},
            )
        except Exception:  # pragma: no cover - auditing must not block a task
            log.exception("audit sink failed while recording a policy decision")
