#!/usr/bin/env python3
"""
Iddo Harness — Agent main entry point
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

try:
    from config import ensure_ws_token, load_config, load_runtime_config
    from metrics import Metrics, MetricsServer, set_metrics
    from poller import GithubPoller
    from executor import Executor
    from reporter import Reporter
    from policy import PolicyEngine
    from runner import TaskRunner
except ImportError:  # pragma: no cover
    from agent.config import ensure_ws_token, load_config, load_runtime_config
    from agent.metrics import Metrics, MetricsServer, set_metrics
    from agent.poller import GithubPoller
    from agent.executor import Executor
    from agent.reporter import Reporter
    from agent.policy import PolicyEngine
    from agent.runner import TaskRunner
try:
    from confirm import ConfirmManager
except ImportError:  # pragma: no cover
    from agent.confirm import ConfirmManager
try:
    from approval import ApprovalManager
    from audit import AuditLog
    from health_server import HealthServer
    from secrets_vault import SecretVault
except ImportError:  # pragma: no cover
    from agent.approval import ApprovalManager
    from agent.audit import AuditLog
    from agent.health_server import HealthServer
    from agent.secrets_vault import SecretVault

LOCK_PATH = Path.home() / ".iddo-harness" / "agent.lock"


def setup_logging(level=logging.INFO):
    root = Path.home() / ".iddo-harness"
    root.mkdir(exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(root / "audit.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _write_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _remove_lock():
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Iddo Harness agent")
    parser.add_argument("--config", default=None, help="path to policy.yaml")
    parser.add_argument("--runtime-config", default=None, help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("harness.main")

    log.info("=" * 60)
    log.info("Iddo Harness starting up")
    log.info("=" * 60)

    cfg = load_config(args.config)
    runtime = load_runtime_config(args.runtime_config)

    # v3 Track B. Each of these degrades to the v1/v2 behaviour on its own:
    # no audit key yet → the log generates one; no agent.key → the vault only
    # complains when a task actually references {{secret:...}}; health.enabled
    # false → no socket is opened at all.
    audit_log = AuditLog.from_config(cfg)
    vault = SecretVault.from_config(cfg, audit_sink=audit_log)
    policy = PolicyEngine(cfg, audit_log=audit_log)
    confirm_manager = ApprovalManager(cfg, audit_log=audit_log)
    executor = Executor(policy, confirm_manager, vault=vault, audit_log=audit_log)
    reporter = Reporter(cfg)
    poller = GithubPoller(cfg)

    audit_log.record(
        actor="harness", action="agent_startup", resource=cfg.transport.get("repo", ""),
        meta={"pid": os.getpid(), "approval_mode": confirm_manager.mode, "once": bool(args.once)},
    )

    metrics = set_metrics(Metrics(enabled=runtime.metrics.enabled))
    runner = TaskRunner(
        executor=executor,
        reporter=reporter,
        metrics=metrics,
        max_concurrent_tasks=runtime.max_concurrent_tasks,
        default_max_attempts=runtime.retry.default_max_attempts,
        default_backoff_seconds=runtime.retry.default_backoff_seconds,
        on_finished=lambda outcome: poller.mark_done(outcome.task)
        if outcome.status == "ok"
        else poller.mark_failed(outcome.task),
    )
    metrics.register_gauge("queue_depth", lambda: runner.queue.depth)
    metrics.register_gauge("active_tasks", lambda: runner.in_flight)
    metrics.register_gauge("active_sessions", lambda: len(getattr(executor.v2, "sessions", ())))
    metrics.register_gauge("active_watches", lambda: len(getattr(executor.v2, "watches", ())))

    metrics_server = None
    if runtime.metrics.enabled:
        metrics_server = MetricsServer(metrics, version=str(cfg.version))
        try:
            metrics_server.start()
            log.info(f"Metrics on http://127.0.0.1:{metrics_server.bound_port}/metrics")
        except OSError:
            log.warning("metrics port busy — continuing without the metrics endpoint")
            metrics_server = None

    ws = _start_ws_bridge(runtime, executor, reporter, metrics, log)

    health: HealthServer | None = None
    if HealthServer.enabled_in(cfg):
        try:
            health = HealthServer.from_config(cfg, audit_log=audit_log)
            health.start()
        except Exception:
            log.exception("health endpoint failed to start — continuing without it")
            health = None

    log.info(
        f"Polling {cfg.transport['repo']} every {runtime.poll_interval_seconds}s, "
        f"up to {runtime.max_concurrent_tasks} task(s) at a time"
    )

    _write_lock()
    try:
        while True:
            # 1. Scan for resolved pending confirmations first, so approved
            #    tasks resume before we look for brand-new work.
            try:
                resolved = poller.scan_pending(confirm_manager)
                for task, approved in resolved:
                    try:
                        result = executor.resume_after_confirm(task, approved)
                        reporter.send(task, result)
                    except Exception:
                        log.exception(f"Task {task.id} failed while resuming after confirm")
                        reporter.send_error(task, "resume_after_confirm failed")
            except Exception:
                log.exception("pending-confirmation scan failed")

            # 2. Normal task polling. Tasks are queued rather than executed
            #    inline, so a long build no longer blocks the ones behind it.
            tasks = poller.fetch_pending_tasks()
            if tasks:
                log.info(f"Found {len(tasks)} pending task(s)")
                runner.submit_all(tasks)
            runner.pump()

            if args.once:
                runner.drain(timeout=runtime.poll_interval_seconds * 60 or 600)
                break
            time.sleep(runtime.poll_interval_seconds)
    except KeyboardInterrupt:
        log.info("Stopped by user")
        audit_log.record(actor="user", action="agent_shutdown", resource="keyboard_interrupt")
    except Exception as e:
        log.exception("Fatal error in main loop")
        audit_log.record(
            actor="harness", action="agent_shutdown", resource="fatal_error",
            outcome="error", meta={"error": str(e)},
        )
        raise
    else:
        audit_log.record(actor="harness", action="agent_shutdown", resource="clean_exit")
    finally:
        runner.wait_idle(timeout=30)
        if ws is not None:
            ws.stop()
        if metrics_server is not None:
            metrics_server.stop()
        if health is not None:
            health.stop()
        executor.v2.shutdown()
        _remove_lock()


def _start_ws_bridge(runtime, executor, reporter, metrics, log):
    """Bring up the WebSocket transport when config asks for it.

    A missing ``websockets`` package is logged and skipped rather than fatal:
    the git bridge is the transport that always works, and losing the fast path
    should not take the agent down with it.
    """
    if not runtime.ws.enabled:
        return None
    try:
        from ws_bridge import WsBridge, build_execute
    except ImportError:  # pragma: no cover
        from agent.ws_bridge import WsBridge, build_execute

    try:
        bridge = WsBridge(
            execute=build_execute(executor, reporter),
            token=ensure_ws_token(runtime),
            host=runtime.ws.host,
            port=runtime.ws.port,
            metrics=metrics,
        )
        bridge.start()
    except Exception as exc:
        log.warning(f"ws bridge unavailable ({exc}) — continuing on the git bridge only")
        return None
    log.info(f"WebSocket bridge on {bridge.url}")
    return bridge


if __name__ == "__main__":
    main()
