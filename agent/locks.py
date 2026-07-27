"""
Process-wide locks.

v3 runs up to ``max_concurrent_tasks`` tasks in parallel, and every one of them
eventually wants to ``git add/commit/push`` into the *same* bridge working copy
under the temp dir. Concurrent git writes in one working copy corrupt the index
or lose commits, so all three writers — ``reporter.Reporter``,
``reporter_v2.ReporterV2`` and ``poller.GithubPoller`` — serialise through the
single lock below.

It lives in its own module so those three can share it without importing each
other.
"""
from __future__ import annotations

import threading

#: Serialises every git mutation against the bridge working copy.
GIT_PUSH_LOCK = threading.RLock()
