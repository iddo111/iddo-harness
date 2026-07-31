"""
Tests for the v2 Agent Fabric executor (agent/executor_v2.py).

Covers all 14 v2 kinds plus the v1 router and the chunked reporter. The
policy fixture below auto-allows the pytest tmp tree so each test exercises
the executor rather than the confirmation flow; the policy-specific tests
build their own restrictive configs.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from executor import Executor, Result
from executor_v2 import ExecutorV2, V2_KINDS, _assert_local_url, _is_local_ip
from policy import Decision, PolicyEngine
from reporter_v2 import ReporterV2

IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@dataclass
class FakeTask:
    """Minimal stand-in for agent.poller.Task."""

    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    envelope: object | None = None


def make_cfg(tmp_path: Path, **overrides) -> SimpleNamespace:
    """Build a Config-shaped namespace that auto-allows the tmp tree."""
    cfg = SimpleNamespace(
        version=1,
        owner="tester",
        auto_allow={
            "commands": [
                "echo*", "printf*", "python*", "grep *", "glob *", "read_chunk *",
                "process_list", "process_list *", "watch_poll*", "watch_stop*",
                "watch_start *", "cat*", "sh", "/bin/sh", "cmd.exe",
                "http_local GET http://127.0.0.1*", "http_local GET http://localhost*",
            ],
            "paths": {"read": [f"{tmp_path}/**"]},
        },
        require_confirm={
            "commands": ["patch *", "process_kill*", "pip install*"],
            "paths": {"write": [f"{tmp_path}/**"]},
        },
        block={"commands": ["rm -rf /*", "*secret*"], "paths": {"absolute_no_touch": ["**/.env"]}},
        polling={},
        paths={},
        transport={"repo": "iddo111/iddo-harness-bridge", "result_dir": "results/"},
        confirm={"timeout_minutes": 30},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class RecordingConfirm:
    """ConfirmManager stub that records parked tasks instead of notifying."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def create(self, task, reason):
        self.created.append((task.id, reason))
        return SimpleNamespace(message=f"approve? {reason}")


@pytest.fixture
def confirmer() -> RecordingConfirm:
    return RecordingConfirm()


@pytest.fixture
def chunks() -> list[dict]:
    return []


@pytest.fixture
def ex(tmp_path: Path, confirmer: RecordingConfirm, chunks: list[dict]) -> ExecutorV2:
    """An ExecutorV2 wired to a permissive policy and a list-appending sink."""
    policy = PolicyEngine(make_cfg(tmp_path))
    executor = ExecutorV2(
        policy,
        confirm_manager=confirmer,
        chunk_sink=lambda task, body, seq, is_final: chunks.append(body),
    )
    yield executor
    executor.shutdown()


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 1-2. shell_stream
# ---------------------------------------------------------------------------
def test_shell_stream_emits_ordered_chunks_with_single_final(ex: ExecutorV2, chunks: list[dict]):
    cmd = "python -c \"import sys;[print('line',i) or sys.stdout.flush() for i in range(5)]\""
    result = ex.run(FakeTask("t-stream", "shell_stream", {"command": cmd, "timeout_sec": 30}))

    assert result.ok is True
    assert result.exit_code == 0
    assert [c["seq"] for c in chunks] == list(range(len(chunks))), "seq must be gapless and ascending"
    assert [c["is_final"] for c in chunks].count(True) == 1
    assert chunks[-1]["is_final"] is True
    assert chunks[-1]["exit_code"] == 0
    assert chunks[-1]["stats"]["chunks"] == len(chunks) - 1
    # every non-final chunk carries payload text; the concatenation is the output
    streamed = "".join(c["text"] for c in chunks if not c["is_final"])
    assert "line 0" in streamed and "line 4" in streamed
    assert "line 4" in result.stdout


def test_shell_stream_reports_nonzero_exit_and_stderr(ex: ExecutorV2, chunks: list[dict]):
    cmd = "python -c \"import sys;sys.stderr.write('boom\\n');sys.exit(3)\""
    result = ex.run(FakeTask("t-fail", "shell_stream", {"command": cmd, "timeout_sec": 30}))

    assert result.ok is False
    assert result.exit_code == 3
    assert "boom" in result.stderr
    assert any(c.get("stream") == "stderr" for c in chunks)
    assert chunks[-1]["is_final"] is True and chunks[-1]["ok"] is False


def test_shell_stream_times_out(ex: ExecutorV2, chunks: list[dict]):
    result = ex.run(FakeTask("t-slow", "shell_stream", {"command": "python -c \"import time;time.sleep(30)\"", "timeout_sec": 1}))

    assert result.ok is False
    assert result.error == "timeout"
    assert chunks[-1]["is_final"] is True and chunks[-1]["error"] == "timeout"


# ---------------------------------------------------------------------------
# 3. shell sessions
# ---------------------------------------------------------------------------
@pytest.mark.skipif(IS_WINDOWS, reason="uses a POSIX-style python -i REPL")
def test_shell_session_stays_alive_between_writes(ex: ExecutorV2):
    opened = ex.run(FakeTask("t-open", "shell_session_open", {"command": "python -i -u", "idle_timeout_sec": 60}))
    assert opened.ok is True
    sid = opened.metadata["session_id"]
    assert sid in ex.sessions

    first = ex.run(FakeTask("t-w1", "shell_session_write", {"session_id": sid, "input": "x = 21", "read_timeout_sec": 3}))
    assert first.ok is True

    # State from the first write must survive into the second — that is the
    # whole point of a session versus a one-shot shell task.
    second = ex.run(FakeTask("t-w2", "shell_session_write", {"session_id": sid, "input": "print(x * 2)", "read_timeout_sec": 3}))
    assert "42" in (second.stdout + second.stderr)
    assert second.metadata["alive"] is True

    closed = ex.run(FakeTask("t-close", "shell_session_close", {"session_id": sid}))
    assert closed.ok is True and closed.metadata["closed"] is True
    assert sid not in ex.sessions


def test_shell_session_write_unknown_session_fails(ex: ExecutorV2):
    result = ex.run(FakeTask("t-nosess", "shell_session_write", {"session_id": "s-nope", "input": "echo hi"}))
    assert result.ok is False
    assert "no such session" in result.error


def test_shell_session_write_still_honours_block_rules(ex: ExecutorV2):
    opened = ex.run(FakeTask("t-bopen", "shell_session_open", {"command": "python -i -u", "idle_timeout_sec": 60}))
    sid = opened.metadata["session_id"]

    result = ex.run(FakeTask("t-bw", "shell_session_write", {"session_id": sid, "input": "echo my-secret-value"}))
    assert result.decision == "block"
    assert result.ok is False

    ex.run(FakeTask("t-bclose", "shell_session_close", {"session_id": sid}))


def test_shell_session_respects_max_sessions(tmp_path: Path, confirmer: RecordingConfirm):
    ex = ExecutorV2(PolicyEngine(make_cfg(tmp_path)), confirm_manager=confirmer, max_sessions=1)
    try:
        first = ex.run(FakeTask("t-s1", "shell_session_open", {"command": "python -i -u", "idle_timeout_sec": 60}))
        assert first.ok is True
        second = ex.run(FakeTask("t-s2", "shell_session_open", {"command": "python -i -u", "idle_timeout_sec": 60}))
        assert second.ok is False
        assert "session limit reached" in second.error
    finally:
        ex.shutdown()


# ---------------------------------------------------------------------------
# 4. grep
# ---------------------------------------------------------------------------
def test_grep_finds_regex_matches_and_prunes_noise(ex: ExecutorV2, tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "router.py").write_text("def handle_task(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text("def handle_event():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "notes.txt").write_text("def handle_task(y):\n", encoding="utf-8")
    noisy = tmp_path / "node_modules"
    noisy.mkdir()
    (noisy / "junk.py").write_text("def handle_task(z):\n", encoding="utf-8")

    result = ex.run(FakeTask("t-grep", "grep", {
        "path": str(tmp_path), "pattern": r"def\s+handle_\w+", "include": "*.py",
    }))

    assert result.ok is True
    paths = {m["path"] for m in result.metadata["matches"]}
    assert any("router.py" in p for p in paths)
    assert any("other.py" in p for p in paths)
    assert not any("notes.txt" in p for p in paths), "include=*.py must filter by name"
    assert not any("node_modules" in p for p in paths), "noise dirs must be pruned"
    assert result.metadata["count"] == 2
    assert result.metadata["matches"][0]["line_no"] == 1


def test_grep_honours_max_results_ignore_case_and_context(ex: ExecutorV2, tmp_path: Path):
    (tmp_path / "log.txt").write_text("\n".join(["before", "NEEDLE here", "after", "needle again"]), encoding="utf-8")

    capped = ex.run(FakeTask("t-cap", "grep", {
        "path": str(tmp_path), "pattern": "needle", "include": "*.txt",
        "ignore_case": True, "max_results": 1, "context": 1,
    }))
    assert capped.metadata["count"] == 1
    assert capped.metadata["truncated"] is True
    assert capped.metadata["matches"][0]["before"] == ["before"]
    assert capped.metadata["matches"][0]["after"] == ["after"]

    sensitive = ex.run(FakeTask("t-cs", "grep", {"path": str(tmp_path), "pattern": "needle", "include": "*.txt"}))
    assert sensitive.metadata["count"] == 1, "case-sensitive search must skip NEEDLE"


def test_grep_rejects_bad_regex_and_missing_path(ex: ExecutorV2, tmp_path: Path):
    bad = ex.run(FakeTask("t-badrx", "grep", {"path": str(tmp_path), "pattern": "([unclosed"}))
    assert bad.ok is False and "bad regex" in bad.error

    missing = ex.run(FakeTask("t-nopath", "grep", {"path": str(tmp_path / "ghost"), "pattern": "x"}))
    assert missing.ok is False and "path not found" in missing.error


# ---------------------------------------------------------------------------
# 5. glob
# ---------------------------------------------------------------------------
def test_glob_returns_matching_paths_newest_first(ex: ExecutorV2, tmp_path: Path):
    (tmp_path / "src" / "deep").mkdir(parents=True)
    old = tmp_path / "src" / "a.test.ts"
    new = tmp_path / "src" / "deep" / "b.test.ts"
    old.write_text("//", encoding="utf-8")
    new.write_text("//", encoding="utf-8")
    (tmp_path / "src" / "c.ts").write_text("//", encoding="utf-8")
    (tmp_path / "src" / "sub").mkdir()
    import os
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    result = ex.run(FakeTask("t-glob", "glob", {"path": str(tmp_path), "pattern": "*.test.ts"}))

    assert result.ok is True
    assert result.metadata["count"] == 2
    assert result.metadata["paths"][0].endswith("b.test.ts"), "mtime-descending order"
    assert result.metadata["paths"][1].endswith("a.test.ts")
    assert not any(p.endswith("c.ts") for p in result.metadata["paths"])


def test_glob_explicit_doublestar_pattern_and_truncation(ex: ExecutorV2, tmp_path: Path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    for i in range(5):
        (tmp_path / "a" / "b" / f"f{i}.py").write_text("x", encoding="utf-8")

    result = ex.run(FakeTask("t-glob2", "glob", {"path": str(tmp_path), "pattern": "**/*.py", "max_results": 3}))
    assert result.metadata["count"] == 3
    assert result.metadata["truncated"] is True


# ---------------------------------------------------------------------------
# 6. patch_file
# ---------------------------------------------------------------------------
def test_patch_file_applies_edit_and_keeps_backup(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "config.py"
    target.write_text("TIMEOUT = 30\nRETRIES = 2\n", encoding="utf-8")

    result = ex.resume_after_confirm(
        FakeTask("t-patch", "patch_file", {
            "path": str(target), "old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 120",
        }),
        approved=True,
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "TIMEOUT = 120\nRETRIES = 2\n"
    assert result.metadata["replacements"] == 1
    backup = Path(result.metadata["backup_path"])
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "TIMEOUT = 30\nRETRIES = 2\n"
    assert "-TIMEOUT = 30" in result.metadata["diff"]
    assert "+TIMEOUT = 120" in result.metadata["diff"]


def test_patch_file_fails_on_non_unique_unless_replace_all(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "app.py"
    original = "log('a')\nlog('b')\nlog('c')\n"
    target.write_text(original, encoding="utf-8")

    failed = ex.resume_after_confirm(
        FakeTask("t-dup", "patch_file", {"path": str(target), "old_string": "log(", "new_string": "logger.info("}),
        approved=True,
    )
    assert failed.ok is False
    assert "not unique" in failed.error
    assert failed.metadata["occurrences"] == 3
    assert target.read_text(encoding="utf-8") == original, "a failed patch must not touch the file"

    ok = ex.resume_after_confirm(
        FakeTask("t-dup2", "patch_file", {
            "path": str(target), "old_string": "log(", "new_string": "logger.info(", "replace_all": True,
        }),
        approved=True,
    )
    assert ok.ok is True
    assert ok.metadata["replacements"] == 3
    assert target.read_text(encoding="utf-8").count("logger.info(") == 3


def test_patch_file_is_atomic_across_edits(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "multi.py"
    original = "alpha\nbeta\n"
    target.write_text(original, encoding="utf-8")

    result = ex.resume_after_confirm(
        FakeTask("t-atomic", "patch_file", {
            "path": str(target),
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "gamma", "new_string": "GAMMA"},  # not present → whole patch fails
            ],
        }),
        approved=True,
    )

    assert result.ok is False
    assert "not found" in result.error
    assert result.metadata["edit_index"] == 1
    assert target.read_text(encoding="utf-8") == original, "edit[0] must be rolled back with edit[1]"


def test_patch_file_dry_run_leaves_disk_untouched(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "dry.txt"
    target.write_text("one\n", encoding="utf-8")

    result = ex.resume_after_confirm(
        FakeTask("t-dry", "patch_file", {
            "path": str(target), "old_string": "one", "new_string": "two", "dry_run": True,
        }),
        approved=True,
    )

    assert result.ok is True
    assert result.metadata["dry_run"] is True
    assert result.metadata["backup_path"] is None
    assert target.read_text(encoding="utf-8") == "one\n"
    assert list(tmp_path.glob("*.bak-*")) == []


# ---------------------------------------------------------------------------
# 7. watch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("force_polling", [True, False])
def test_watch_start_poll_stop_reports_events(ex: ExecutorV2, tmp_path: Path, force_polling: bool):
    watched = tmp_path / "watched"
    watched.mkdir()

    started = ex.run(FakeTask("t-w", "watch_start", {
        "path": str(watched), "patterns": ["*.txt"],
        "poll_interval_sec": 0.2, "force_polling": force_polling,
    }))
    assert started.ok is True
    wid = started.metadata["watch_id"]
    assert started.metadata["backend"] == ("polling" if force_polling else started.metadata["backend"])

    time.sleep(0.4)
    (watched / "new.txt").write_text("hello", encoding="utf-8")
    (watched / "ignored.log").write_text("nope", encoding="utf-8")

    watch = ex.watches[wid]
    assert wait_for(lambda: len(watch.events) > 0, timeout=6.0), "no filesystem event observed"

    polled = ex.run(FakeTask("t-wp", "watch_poll", {"watch_id": wid}))
    assert polled.ok is True
    assert polled.metadata["count"] >= 1
    assert all(e["path"].endswith(".txt") for e in polled.metadata["events"]), "pattern filter must hold"

    stopped = ex.run(FakeTask("t-ws", "watch_stop", {"watch_id": wid}))
    assert stopped.ok is True and stopped.metadata["stopped"] is True
    assert wid not in ex.watches


def test_watch_poll_and_stop_reject_unknown_ids(ex: ExecutorV2):
    assert "no such watch" in ex.run(FakeTask("t-x", "watch_poll", {"watch_id": "w-nope"})).error
    assert "no such watch" in ex.run(FakeTask("t-y", "watch_stop", {"watch_id": "w-nope"})).error


# ---------------------------------------------------------------------------
# 8. processes
# ---------------------------------------------------------------------------
def test_process_list_returns_this_process(ex: ExecutorV2):
    import os

    result = ex.run(FakeTask("t-ps", "process_list", {"limit": 5000}))

    assert result.ok is True
    pids = {p["pid"] for p in result.metadata["processes"]}
    assert os.getpid() in pids
    assert result.metadata["backend"] in ("psutil", "ps", "tasklist")
    assert all(isinstance(p["pid"], int) for p in result.metadata["processes"])


def test_process_list_filter_and_limit(ex: ExecutorV2):
    unfiltered = ex.run(FakeTask("t-ps2", "process_list", {"limit": 3}))
    assert unfiltered.metadata["count"] <= 3

    nonsense = ex.run(FakeTask("t-ps3", "process_list", {"filter": "zzz-no-such-process-zzz"}))
    assert nonsense.metadata["count"] == 0


def test_process_kill_refuses_self_and_init(ex: ExecutorV2):
    import os

    for pid, expected in ((os.getpid(), "harness itself"), (1, "refusing to kill pid 1"), (0, "refusing to kill pid 0")):
        result = ex.resume_after_confirm(FakeTask(f"t-k{pid}", "process_kill", {"pid": pid}), approved=True)
        assert result.ok is False
        assert expected in result.error


def test_process_kill_terminates_a_real_child(ex: ExecutorV2):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = ex.resume_after_confirm(
            FakeTask("t-kill", "process_kill", {"pid": child.pid, "timeout_sec": 5}), approved=True
        )
        assert result.ok is True
        assert result.metadata["signal"] in ("SIGTERM", "SIGKILL")
        assert wait_for(lambda: child.poll() is not None, timeout=6.0)
    finally:
        if child.poll() is None:  # pragma: no cover - only if the kill failed
            child.kill()
            child.wait(timeout=5)


# ---------------------------------------------------------------------------
# 9. http_local
# ---------------------------------------------------------------------------
def test_http_local_rejects_external_and_non_http_targets(ex: ExecutorV2):
    for url, needle in (
        ("http://93.184.216.34/", "host not local"),
        ("http://8.8.8.8:80/", "host not local"),
        ("file:///etc/passwd", "scheme not allowed"),
        ("http:///nohost", "no host"),
    ):
        result = ex.resume_after_confirm(FakeTask("t-http", "http_local", {"url": url}), approved=True)
        assert result.ok is False, url
        assert needle in result.error, f"{url} → {result.error}"


def test_http_local_local_address_classification():
    assert _is_local_ip("127.0.0.1") is True
    assert _is_local_ip("::1") is True
    assert _is_local_ip("192.168.1.10") is True
    assert _is_local_ip("10.1.2.3") is True
    assert _is_local_ip("172.16.0.9") is True
    assert _is_local_ip("169.254.1.1") is True
    assert _is_local_ip("8.8.8.8") is False
    assert _is_local_ip("172.32.0.1") is False
    assert _assert_local_url("http://127.0.0.1:9/")[0] is True
    assert _assert_local_url("ftp://127.0.0.1/")[0] is False


def test_http_local_round_trips_against_a_loopback_server(ex: ExecutorV2):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        result = ex.run(FakeTask("t-ok", "http_local", {"url": url}))
        assert result.ok is True
        assert result.metadata["status"] == 200
        assert json.loads(result.metadata["body"]) == {"status": "ok"}
        assert result.metadata["truncated"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_http_local_rejects_unknown_method(ex: ExecutorV2):
    result = ex.resume_after_confirm(
        FakeTask("t-m", "http_local", {"url": "http://127.0.0.1:1/", "method": "TRACE"}), approved=True
    )
    assert result.ok is False and "method not allowed" in result.error


# ---------------------------------------------------------------------------
# 10. read_file_chunked
# ---------------------------------------------------------------------------
def test_read_file_chunked_paginates_exactly(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "big.log"
    content = "".join(f"{i:04d}\n" for i in range(500))  # 2500 bytes
    target.write_bytes(content.encode("utf-8"))
    total = len(content.encode("utf-8"))

    collected = ""
    offset: int | None = 0
    reads = 0
    while offset is not None:
        result = ex.run(FakeTask("t-rc", "read_file_chunked", {
            "path": str(target), "offset": offset, "limit_bytes": 512,
        }))
        assert result.ok is True
        assert result.metadata["total_size"] == total
        assert result.metadata["offset"] == offset
        collected += result.metadata["content"]
        offset = result.metadata["next_offset"]
        reads += 1
        assert reads < 20, "pagination did not terminate"

    assert collected == content
    assert reads == 5  # ceil(2500 / 512)


def test_read_file_chunked_flags_eof_and_missing_file(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "small.txt"
    target.write_text("tiny", encoding="utf-8")

    result = ex.run(FakeTask("t-eof", "read_file_chunked", {"path": str(target)}))
    assert result.metadata["eof"] is True
    assert result.metadata["next_offset"] is None
    assert result.metadata["bytes_read"] == 4

    missing = ex.run(FakeTask("t-miss", "read_file_chunked", {"path": str(tmp_path / "ghost.txt")}))
    assert missing.ok is False and "file not found" in missing.error


def test_read_file_chunked_survives_split_multibyte_sequences(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "heb.txt"
    target.write_text("שלום עולם", encoding="utf-8")

    # 3 bytes lands mid-character; errors="replace" must keep this from raising.
    result = ex.run(FakeTask("t-mb", "read_file_chunked", {"path": str(target), "limit_bytes": 3}))
    assert result.ok is True
    assert result.metadata["bytes_read"] == 3
    assert result.metadata["eof"] is False


# ---------------------------------------------------------------------------
# 11. Policy integration
# ---------------------------------------------------------------------------
def test_patch_file_requires_confirmation_by_default(ex: ExecutorV2, tmp_path: Path, confirmer: RecordingConfirm):
    target = tmp_path / "gated.txt"
    target.write_text("before\n", encoding="utf-8")

    result = ex.run(FakeTask("t-gate", "patch_file", {
        "path": str(target), "old_string": "before", "new_string": "after",
    }))

    assert result.decision == "confirm_required"
    assert result.ok is False
    assert confirmer.created and confirmer.created[0][0] == "t-gate"
    assert target.read_text(encoding="utf-8") == "before\n", "nothing written before approval"

    approved = ex.resume_after_confirm(
        FakeTask("t-gate", "patch_file", {"path": str(target), "old_string": "before", "new_string": "after"}),
        approved=True,
    )
    assert approved.ok is True
    assert target.read_text(encoding="utf-8") == "after\n"


def test_denied_confirmation_runs_nothing(ex: ExecutorV2, tmp_path: Path):
    target = tmp_path / "denied.txt"
    target.write_text("keep\n", encoding="utf-8")

    result = ex.resume_after_confirm(
        FakeTask("t-deny", "patch_file", {"path": str(target), "old_string": "keep", "new_string": "gone"}),
        approved=False,
    )
    assert result.ok is False and result.decision == "denied"
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_blocked_command_never_reaches_the_shell(ex: ExecutorV2, chunks: list[dict]):
    result = ex.run(FakeTask("t-block", "shell_stream", {"command": "echo my-secret-value"}))
    assert result.decision == "block"
    assert chunks == []


def test_process_kill_is_confirm_gated(ex: ExecutorV2, confirmer: RecordingConfirm):
    result = ex.run(FakeTask("t-killgate", "process_kill", {"pid": 999_999}))
    assert result.decision == "confirm_required"
    assert confirmer.created[-1][0] == "t-killgate"


def test_policy_read_path_rule_allows_v2_reads(tmp_path: Path):
    """grep/read_chunk under an allowed read root are auto even without a command rule."""
    cfg = make_cfg(tmp_path)
    cfg.auto_allow["commands"] = []  # strip command patterns; only the path rule remains
    policy = PolicyEngine(cfg)

    assert policy.decide(f"grep {tmp_path}/pkg", [f"{tmp_path}/pkg"])[0] is Decision.AUTO
    assert policy.decide(f"read_chunk {tmp_path}/a.txt", [f"{tmp_path}/a.txt"])[0] is Decision.AUTO
    assert policy.decide("grep /somewhere/else", ["/somewhere/else"])[0] is Decision.CONFIRM
    # write verbs never get in through the read rule
    assert policy.decide(f"patch {tmp_path}/a.txt", [f"{tmp_path}/a.txt"])[0] is Decision.CONFIRM


def test_policy_v1_decisions_are_unchanged(tmp_path: Path):
    """Backward compat: the v1 command rules still decide exactly as before."""
    policy = PolicyEngine(make_cfg(tmp_path))
    assert policy.decide("echo hi", [])[0] is Decision.AUTO
    assert policy.decide("pip install requests", [])[0] is Decision.CONFIRM
    assert policy.decide("rm -rf /home", [])[0] is Decision.BLOCK
    assert policy.decide("cat /etc/hosts", ["/root/.env"])[0] is Decision.BLOCK
    assert policy.decide("some-unknown-binary", [])[0] is Decision.CONFIRM


# ---------------------------------------------------------------------------
# 12. v1 router / backward compatibility
# ---------------------------------------------------------------------------
def test_v1_executor_routes_v2_kinds_and_preserves_v1(tmp_path: Path, confirmer: RecordingConfirm):
    target = tmp_path / "v1.txt"
    target.write_text("hello v1\n", encoding="utf-8")
    executor = Executor(PolicyEngine(make_cfg(tmp_path)), confirm_manager=confirmer)
    try:
        # v2 kind → delegated
        v2 = executor.run(FakeTask("t-r1", "glob", {"path": str(tmp_path), "pattern": "*.txt"}))
        assert v2.ok is True and any(p.endswith("v1.txt") for p in v2.metadata["paths"])

        # v1 kind → untouched legacy path
        v1 = executor.run(FakeTask("t-r2", "read_file", {"path": str(target)}))
        assert v1.ok is True and v1.stdout == "hello v1\n"

        # unknown kind → unchanged error contract
        unknown = executor.run(FakeTask("t-r3", "teleport", {}))
        assert unknown.ok is False and unknown.decision == "unknown_kind"
    finally:
        executor.v2.shutdown()


def test_router_caches_executor_v2_so_sessions_persist(tmp_path: Path, confirmer: RecordingConfirm):
    executor = Executor(PolicyEngine(make_cfg(tmp_path)), confirm_manager=confirmer)
    try:
        assert executor.v2 is executor.v2
        assert all(Executor._is_v2_kind(k) for k in V2_KINDS)
        assert not Executor._is_v2_kind("shell")
        assert not Executor._is_v2_kind("write_file")
    finally:
        executor.v2.shutdown()


def test_unknown_v2_kind_is_reported_not_raised(ex: ExecutorV2):
    result = ex.run(FakeTask("t-unknown", "not_a_kind", {}))
    assert result.ok is False and result.decision == "unknown_kind"


# ---------------------------------------------------------------------------
# 13. ReporterV2 chunk protocol
# ---------------------------------------------------------------------------
def test_reporter_v2_writes_ordered_chunk_files(tmp_path: Path):
    reporter = ReporterV2(make_cfg(tmp_path), git_push=False, local_dir=tmp_path / "bridge")
    task = FakeTask("t-rep", "shell_stream", {})

    reporter.send_chunk(task, {"stream": "stdout", "text": "a\n"}, seq=0, is_final=False)
    reporter.send_chunk(task, {"stream": "stdout", "text": "b\n"}, seq=1, is_final=False)
    reporter.send_chunk(task, {"ok": True, "exit_code": 0}, seq=2, is_final=True)

    files = sorted((tmp_path / "bridge" / "results").glob("t-rep-chunk-*.json"))
    assert [f.name for f in files] == ["t-rep-chunk-0.json", "t-rep-chunk-1.json", "t-rep-chunk-2.json"]

    bodies = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    assert [b["seq"] for b in bodies] == [0, 1, 2]
    assert [b["is_final"] for b in bodies] == [False, False, True]
    assert all(b["task_id"] == "t-rep" for b in bodies)
    assert bodies[2]["exit_code"] == 0


def test_reporter_v2_drives_executor_streaming_end_to_end(tmp_path: Path, confirmer: RecordingConfirm):
    reporter = ReporterV2(make_cfg(tmp_path), git_push=False, local_dir=tmp_path / "bridge")
    ex = ExecutorV2(PolicyEngine(make_cfg(tmp_path)), confirm_manager=confirmer, chunk_sink=reporter.chunk_sink)
    try:
        cmd = "python -c \"import sys;[print('row',i) or sys.stdout.flush() for i in range(3)]\""
        result = ex.run(FakeTask("t-e2e", "shell_stream", {"command": cmd, "timeout_sec": 30}))
        assert result.ok is True
    finally:
        ex.shutdown()

    files = sorted(
        (tmp_path / "bridge" / "results").glob("t-e2e-chunk-*.json"),
        key=lambda p: int(p.stem.rsplit("-", 1)[1]),
    )
    bodies = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    assert len(bodies) >= 2
    assert [b["seq"] for b in bodies] == list(range(len(bodies)))
    assert bodies[-1]["is_final"] is True
    assert "row 2" in "".join(b.get("text", "") for b in bodies)


def test_reporter_v2_send_wraps_a_plain_result_as_one_final_chunk(tmp_path: Path):
    reporter = ReporterV2(make_cfg(tmp_path), git_push=False, local_dir=tmp_path / "bridge")
    task = FakeTask("t-single", "grep", {})

    path = reporter.send(task, Result(task_id="t-single", ok=True, decision="auto", stdout="hit"))

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["seq"] == 0 and body["is_final"] is True
    assert body["ok"] is True and body["stdout"] == "hit"


def test_reporter_v2_wraps_amp_tasks_in_harness_result_envelopes(tmp_path: Path):
    amp = pytest.importorskip("amp")
    inbound = amp.build_envelope(
        direction="inbound",
        source_brick="perplexity-computer",
        source_instance="pc-1",
        channel="github",
        identity_canonical="brick:perplexity-computer",
        payload_type="harness_task",
        payload_body={"task_id": "t-amp", "kind": "grep", "payload": {}},
        to_channel="github",
        to_address="brick:perplexity-computer",
    )
    reporter = ReporterV2(make_cfg(tmp_path), git_push=False, local_dir=tmp_path / "bridge")
    task = FakeTask("t-amp", "grep", {}, envelope=inbound)

    path = reporter.send_chunk(task, {"ok": True, "count": 2}, seq=0, is_final=True)
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["payload"]["type"] == "harness_result"
    assert doc["direction"] == "outbound"
    assert doc["payload"]["body"]["task_id"] == "t-amp"
    assert doc["payload"]["body"]["is_final"] is True
    assert doc["reply"]["reply_to_id"] == inbound.id
    assert doc["reply"]["to_address"] == "brick:perplexity-computer"
