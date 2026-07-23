#!/usr/bin/env python3
"""
Iddo Harness — Agent main entry point
"""
import argparse
import logging
import sys
import time
from pathlib import Path

from config import load_config
from poller import GithubPoller
from executor import Executor
from reporter import Reporter
from policy import PolicyEngine


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


def main():
    parser = argparse.ArgumentParser(description="Iddo Harness agent")
    parser.add_argument("--config", default=None, help="path to policy.yaml")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("harness.main")

    log.info("=" * 60)
    log.info("Iddo Harness starting up")
    log.info("=" * 60)

    cfg = load_config(args.config)
    policy = PolicyEngine(cfg)
    executor = Executor(policy)
    reporter = Reporter(cfg)
    poller = GithubPoller(cfg)

    log.info(f"Polling {cfg.transport['repo']} every {cfg.polling['interval_seconds']}s")

    try:
        while True:
            tasks = poller.fetch_pending_tasks()
            if tasks:
                log.info(f"Found {len(tasks)} pending task(s)")
                for task in tasks:
                    try:
                        result = executor.run(task)
                        reporter.send(task, result)
                        poller.mark_done(task)
                    except Exception as e:
                        log.exception(f"Task {task.id} failed")
                        reporter.send_error(task, str(e))
                        poller.mark_failed(task)
            if args.once:
                break
            time.sleep(cfg.polling["interval_seconds"])
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception:
        log.exception("Fatal error in main loop")
        raise


if __name__ == "__main__":
    main()
