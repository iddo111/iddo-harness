"""
Reporter — writes results back to the bridge repo.

AMP integration (docs/amp_alignment.md, docs/amp_envelope_examples.md):
when the originating task carried a validated AMP envelope (i.e.
`task.envelope` is set by poller.py), the result written to
`results/<id>.json` is now a full AMP v1.0 envelope with
`payload.type == "harness_result"`, `direction == "outbound"`, and
`reply.to_address` copied from the incoming task's own `reply` (so the
result routes back to whoever sent the task). For legacy (non-AMP) task
packets, `task.envelope` is None and the old plain-dict result shape
(docs/task_packet_spec.md) is preserved unchanged for backward
compatibility.
"""
import json
import logging
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

import amp

try:
    from locks import GIT_PUSH_LOCK
except ImportError:  # installed as a package
    from agent.locks import GIT_PUSH_LOCK

try:
    from signing import Signer
except ImportError:  # pragma: no cover - packaged imports
    from agent.signing import Signer

log = logging.getLogger("harness.reporter")

# Iddo Harness's own AMP identity when acting as the outbound source/actor.
# TODO(amp): make these configurable via policy.yaml / config.py instead of
# hardcoding — deferred here since config.py is off-limits for this change.
HARNESS_BRICK_NAME = "iddo-harness"
HARNESS_IDENTITY_CANONICAL = "brick:iddo-harness"


class Reporter:
    def __init__(self, cfg, signer=None):
        self.cfg = cfg
        self.repo = cfg.transport["repo"]
        self.result_dir = cfg.transport.get("result_dir", "results/")
        self._local = Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{self.repo.replace('/', '_')}"
        self._instance = getattr(cfg, "owner", None) or "agent-default"
        # Every published result is signed (docs/security_v3.md §1). An install
        # that never ran `installer.gen_keys` has no key, and Signer then passes
        # documents through unchanged — unsigned beats not reporting at all.
        self.signer = signer if signer is not None else Signer.from_config(cfg)

    # -----------------------------------------------------------------------
    def send(self, task, result):
        payload = self._build_result_payload(task, asdict(result))
        self._write(task, payload)

    def send_error(self, task, err_msg: str):
        legacy = {"task_id": task.id, "ok": False, "error": err_msg, "decision": "error"}
        payload = self._build_result_payload(task, legacy)
        self._write(task, payload)

    def send_attempt(self, task, result, attempt: int):
        """Record one retry attempt as `results/<id>-attempt-<n>.json`.

        The final attempt is *also* written to `results/<id>.json` by `send`,
        so a consumer that knows nothing about retries still finds the outcome
        where it has always been.
        """
        body = {**asdict(result), "attempt": attempt}
        payload = self._build_result_payload(task, body)
        self._write(task, payload, filename=f"{task.id}-attempt-{attempt}.json")

    # -----------------------------------------------------------------------
    def _build_result_payload(self, task, result_body: dict) -> dict:
        """
        Return the dict to write to results/<id>.json.

        If `task.envelope` is a validated AmpEnvelope (AMP-shaped inbound
        task, see poller.py), wrap `result_body` in a full AMP
        `harness_result` outbound envelope, replying to the address the
        incoming task itself came from (`task.envelope.reply.to_address`).

        Otherwise (legacy, non-AMP task packet), return `result_body`
        unchanged — this is the exact pre-AMP result shape from
        docs/task_packet_spec.md, preserved for backward compatibility.
        """
        envelope = getattr(task, "envelope", None)
        if envelope is None:
            return result_body

        # result_body already carries task_id/ok/etc — that's exactly the
        # payload.body shape harness_result expects (amp.py requires at
        # least task_id + ok).
        result_envelope = amp.build_envelope(
            direction="outbound",
            source_brick=HARNESS_BRICK_NAME,
            source_instance=self._instance,
            channel=envelope.channel,
            identity_canonical=HARNESS_IDENTITY_CANONICAL,
            identity_self=False,
            payload_type="harness_result",
            payload_body=result_body,
            to_channel=envelope.reply.to_channel,
            to_address=envelope.reply.to_address,
            reply_to_id=envelope.id,
        )
        return amp.serialize(result_envelope)

    # -----------------------------------------------------------------------
    def _write(self, task, payload: dict, filename: str | None = None):
        payload = self.signer.sign(payload, context=f"result:{task.id}")
        out_dir = self._local / self.result_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / (filename or f"{task.id}.json")
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        # One working copy, several worker threads: git must be single-writer.
        with GIT_PUSH_LOCK:
            subprocess.run(["git", "-C", str(self._local), "add", str(p)], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(self._local), "commit", "-m", f"result: {p.stem}"], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(self._local), "push", "--quiet"], check=False, capture_output=True)
        # AMP-shaped results nest ok/decision under payload.body; legacy
        # results carry them at the top level. Read from whichever is present
        # so the log line stays informative either way.
        body = payload.get("payload", {}).get("body", payload) if "payload" in payload else payload
        log.info(f"reported {task.id}: ok={body.get('ok')} decision={body.get('decision')}")
