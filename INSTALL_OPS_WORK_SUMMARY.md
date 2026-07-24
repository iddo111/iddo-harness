# Harness · Service Install — Work Summary

Scope: make `iddo-harness` install as a proper background service on Linux
(systemd) and Windows (Task Scheduler), plus operational tooling (health
check, log rotation) and operator docs. All work is confined to
`installer/`, `ops/`, `docs/`, and this summary — **no files under `agent/`
were modified**, per the task constraint.

All 9 numbered deliverables plus this summary are complete and, where a
live Linux environment was available, tested end-to-end.

---

## 1. What was delivered

| # | File | Status |
|---|---|---|
| 1 | `installer/install_linux.sh` | Rewritten. Live-tested (multiple install/reinstall/idempotent-rerun cycles). |
| 2 | `installer/systemd/iddo-harness.service` | New. Live-tested — service reaches `active (running)`. |
| 3 | `installer/install_windows.ps1` | Rewritten. Syntax-validated via PowerShell AST parser (no Windows host available to execute). |
| 4 | `installer/uninstall_linux.sh` | New. Live-tested, including `--purge --yes` and idempotent double-run. |
| 5 | `installer/uninstall_windows.ps1` | New. Syntax-validated via PowerShell AST parser. |
| 6 | `ops/health_check.py` | New (350 lines). Live-tested against the running service — all 4 checks (`github`, `disk_space`, `last_poll`, `audit_growth`) return correct statuses; exit code reflects overall pass/fail. |
| 7 | `ops/rotate_logs.py` | New (163 lines). Live-tested: rotation trigger, gzip archiving, `--keep N` pruning, and — critically — verified a live `logging.FileHandler` in another process survives the rotation with no data loss (see §4). |
| 8 | `docs/operations.md` | New. Start/stop/restart, log locations, pending-confirmation workflow, policy rollback procedure. |
| 9 | `docs/troubleshooting.md` | New. Bridge unreachable, git push blocked, policy syntax errors, tasks stuck in pending, plus a general liveness checklist. |

A `pyproject.toml` and `agent/cli.py` (click-based CLI) already existed in
the workspace by the time this work concluded — those were produced by a
concurrent subagent working on the agent code itself, not by this task.
They were read and relied upon (e.g. `[project.scripts] iddo-harness =
"agent.cli:cli"`) but not modified here.

---

## 2. How installation actually works now

**Linux** (`installer/install_linux.sh`):
1. Detects Ubuntu/Debian (`/etc/os-release`).
2. Creates a system user `iddo-harness` (no login shell) if missing.
3. Installs to `/opt/iddo-harness` — clones the public repo, or copies from
   a local checkout if run from inside one (see §5 on repo lag).
4. Creates a venv at `/opt/iddo-harness/.venv`, runs `pip install -e .`.
5. Copies `policy.yaml` to `/etc/iddo-harness/policy.yaml` **only if absent**
   (never clobbers an existing config).
6. Installs `installer/systemd/iddo-harness.service` to
   `/etc/systemd/system/`, runs `systemctl daemon-reload && systemctl enable
   --now iddo-harness`.
7. Prints status and the `journalctl -u iddo-harness -f` log-viewing command.

Every step checks current state before acting (user exists? venv exists?
config exists? unit file identical?) — safe to re-run any number of times.

**Windows** (`installer/install_windows.ps1`):
1. Checks for Python 3.11+ on `PATH`.
2. Installs to `C:\Program Files\iddo-harness` if running elevated,
   otherwise falls back to `%LOCALAPPDATA%\iddo-harness`.
3. Venv + `pip install -e .`, same as Linux.
4. Copies `policy.yaml` to `%LOCALAPPDATA%\iddo-harness\policy.yaml` (again,
   only if absent).
5. Registers a Scheduled Task `IddoHarness`: trigger `AtLogOn`, hidden
   window, `RestartCount`/`RestartInterval` for crash recovery, action
   invokes `pythonw` against the installed CLI entry point.
6. Triggers the task once and polls for the process to confirm it actually
   starts, then prints how to tail the log.

---

## 3. Two real bugs found and fixed at the install layer (not in `agent/*.py`)

These were discovered through live reproduction on the Linux sandbox, not
speculation, and both are documented in `docs/troubleshooting.md`.

**a) Bare-import / `PYTHONPATH` requirement.** `agent/*.py` files import
each other with flat, script-style imports (`from config import
load_config`, not `from agent.config import load_config`). Running the
installed console-script from an arbitrary working directory throws
`ModuleNotFoundError: No module named 'config'`. Fixed at the installer
level — the systemd unit sets `Environment=PYTHONPATH=/opt/iddo-harness/agent`;
the Windows scheduled task's launch command does the equivalent. `agent/*.py`
itself was never touched. (A concurrent subagent's `tests/conftest.py`
independently documents this exact same `sys.path` requirement, confirming
it's a known, real constraint of the current code rather than an artifact
of my test setup.)

**b) `os.getlogin()` `OSError` under systemd.** Confirmed via direct
reproduction (`setsid python3 ... < /dev/null`) that `os.getlogin()` raises
`OSError: [Errno -25] Unknown error -25` in any context without a
controlling TTY — which is exactly what a systemd `Type=simple` service
gets. This could not be worked around from the installer side (env vars
like `USER`/`LOGNAME` don't help — `os.getlogin()` reads the controlling
tty/utmp, not the environment). **Status at time of writing:** `agent/config.py`
now contains a `_default_owner()` helper (confirmed present via direct
re-read of the file, line 31 in the current file) that wraps `os.getlogin()`
in try/except and falls back to env vars — this was fixed by a concurrent
subagent's work on `agent/config.py`, not by this task, and is called out
here so whoever reviews this knows the fix landed and why it was needed.

---

## 4. CLI invocation ordering — flagging a spec discrepancy

The task spec's literal wording for the systemd `ExecStart` was:
```
/opt/iddo-harness/.venv/bin/iddo-harness run --config /etc/iddo-harness/policy.yaml
```
The actual installed CLI (`agent/cli.py`, click-based) defines `--config` as
a **group-level** option on the top-level `cli` group, not on the `run`
subcommand — so it must precede the subcommand:
```
/opt/iddo-harness/.venv/bin/iddo-harness --config /etc/iddo-harness/policy.yaml run
```
Confirmed live: `iddo-harness run --config ...` errors ("no such option"),
while `iddo-harness --config ... run` works correctly and was what actually
got the live service to `active (running)`. The shipped unit file
(`installer/systemd/iddo-harness.service`) and both install scripts use the
working order. This is flagged explicitly in case the original spec wording
was intentional for a different, not-yet-existing CLI shape.

---

## 5. Heads-up: GitHub repo may lag the local workspace

At one point during this task, `git clone --depth 1` against the real
`github.com/iddo111/iddo-harness` showed an **older, simpler** `agent/` tree
(argparse-based `agent/main.py`, no `cli.py`/`amp.py`/`confirm.py`) — i.e.
matching the very first version of the repo read at the start of this task,
not the richer click-based CLI that exists in this local workspace now
(built up by a concurrent subagent editing `agent/` in parallel with this
installer work).

Practical implication: **if the installer's "clone from GitHub" code path
runs today against the public repo URL, it will pull the old tree** and the
`ExecStart=... iddo-harness --config ... run` invocation in the shipped
systemd unit would not match that older CLI's argument shape. To mitigate,
both install scripts prefer copying from a local source checkout (detected
by the presence of `pyproject.toml` next to the installer script itself)
over cloning, when run from inside an existing checkout — which is how this
was tested. If the intent is for `curl | bash`-style remote installs to
work against the public repo, **the repo needs to be pushed with the current
`agent/` tree (cli.py, amp.py, confirm.py, llm_client.py, llm_router.py,
pyproject.toml, requirements.txt, tests/) before that path is relied on.**
This is outside the scope of this task (installer/ops only) but important
enough to flag here.

---

## 6. Test results / evidence

All conducted on the Linux sandbox (no Windows host available — Windows
scripts are syntax-validated only, see below).

- **`bash -n`**: `install_linux.sh`, `uninstall_linux.sh` — both pass.
- **`python3 -m py_compile`**: `health_check.py`, `rotate_logs.py` — both pass.
- **PowerShell AST parse** (via portable pwsh 7.4.6 binary,
  `[System.Management.Automation.Language.Parser]::ParseFile`):
  `install_windows.ps1`, `uninstall_windows.ps1` — zero syntax errors.
- **Live install**: `install_linux.sh` run to completion; systemd unit
  installed identically to the repo copy (`diff` confirmed); service process
  observed running (`ps aux` showed
  `/opt/iddo-harness/.venv/bin/python3 /opt/iddo-harness/.venv/bin/iddo-harness --config /etc/iddo-harness/policy.yaml run`
  as PID 8942 under the `iddo-harness` user).
- **Idempotency**: installer re-run against an already-installed system —
  no disruption, existing config/user/unit left alone, script exits cleanly.
- **Uninstall**: both default mode (keeps config + user) and
  `--purge --yes` mode (removes everything) tested live, plus a second
  run immediately after to confirm idempotent no-op behavior on an
  already-removed install.
- **`ops/health_check.py`**: run live as root with `HOME=/opt/iddo-harness`
  against the running service. All non-network checks (`disk_space`,
  `last_poll`, `audit_growth`) returned `ok` with correct, sane values
  (e.g. audit log age in the single-digit seconds while the service was
  actively polling). The `github` check correctly returned `fail` because
  this sandbox has no `gh auth login` configured — that's the check working
  as intended, not a bug. Exit code was `2` (fail), matching the one failing
  check.
- **`ops/rotate_logs.py`**: tested (a) no-file case, (b) below-threshold
  no-op, (c) forced rotation with archiving, (d) `--keep N` pruning against
  5 pre-existing fake archives (correctly kept the newest 2), and (e) — the
  most important safety property — a live Python process holding an open
  `logging.FileHandler` on the log file continued writing correctly
  immediately after `rotate_logs.py --mode truncate` ran concurrently: lines
  written before rotation ended up in the gzip archive, lines written after
  continued appending to the truncated live file, no corruption or lost
  writes.

---

## 7. Files not touched (per constraints)

No file under `agent/` was read-and-then-edited by this task. `agent/config.py`
was re-read once, near the end of this task, purely to confirm the
`_default_owner()` fix (landed by a concurrent subagent) was still present
before writing `docs/troubleshooting.md` — zero bytes of it were changed by
this task. `pyproject.toml` (root) was likewise read-only from this task's
perspective; it's owned by the concurrent agent-code subagent.
