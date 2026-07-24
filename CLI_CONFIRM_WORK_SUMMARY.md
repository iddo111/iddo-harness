# CLI + Confirmation Flow — Work Summary

Scope: build a user-facing CLI (`iddo-harness`) and a working confirmation
flow for the `iddo-harness` agent, per the task spec. All work lives in
`/home/user/workspace/harness-v1/`.

## What was built

### 1. `agent/cli.py` — click-based CLI
A `click` group with subcommands:

- `iddo-harness run [--once]` — imports `agent/main.py` and calls `main.main()`, forwarding `--config`/`--verbose`/`--once`.
- `iddo-harness submit <kind> [--command] [--path] [--content] [--timeout] [--priority] [--no-push]` — builds a task record (AMP envelope via `agent/amp.py`'s `build_envelope`, with a plain-dict fallback if AMP isn't importable/valid) and writes it to the bridge repo's `tasks/` dir, then commits+pushes (unless `--no-push`).
- `iddo-harness status [-n]` — reports: agent running (checks `~/.iddo-harness/agent.lock`), last N results from the bridge `results/` dir (handles both legacy and AMP-wrapped result shapes), pending-confirmation count/details, and pending-task count in the bridge repo.
- `iddo-harness confirm <task-id> [--approve|--deny]` — writes `approved-<id>.json`/`denied-<id>.json` via `ConfirmManager.respond()`, both locally and to the bridge repo.
- `iddo-harness tail [-n] [-f]` — tails `~/.iddo-harness/audit.log`.
- `iddo-harness policy check <command> [--path ...]` — runs `PolicyEngine.decide()` dry and prints Decision + reason.

All logging goes through `logging.getLogger("harness.cli")`.

### 2. `agent/confirm.py` — confirmation subsystem
`ConfirmManager` + `PendingConfirmation`:

- `create(task, reason)` — writes `pending-confirm-<id>.json` to `~/.iddo-harness/pending/` **and** mirrors it into the bridge repo's `pending/` folder (commit+push), with a mobile-friendly one-liner: `"Approve task <id>? Command: <cmd>. Reply with `iddo-harness confirm <id> --approve` or reject."`
- `respond(task_id, approve)` — CLI-side write of `approved-<id>.json` / `denied-<id>.json` to both local and bridge `pending/` dirs.
- `list_pending()` — pending confirmations with no response yet.
- `check_response(task_id)` — looks in both local and bridge dirs for a response file.
- `sweep_timeouts()` — auto-denies pending confirmations older than `confirm.timeout_minutes` (default 30, read from `policy.yaml`).

Logging via `logging.getLogger("harness.confirm")`.

### 3. `agent/executor.py` (updated)
- `Executor.__init__` now takes an optional `confirm_manager` (defaults to a fresh `ConfirmManager`).
- On `Decision.CONFIRM` (both `_run_shell` and `_write_file` paths), instead of just refusing, the executor now calls `confirm_manager.create(task, reason)` and returns a `confirm_required` result carrying the mobile one-liner in `metadata["message"]`.
- Added `resume_after_confirm(task, approved: bool) -> Result`: if denied, returns a `denied` Result without running anything; if approved, dispatches to the real action (`_exec_shell` / `_exec_write_file` / `_read_file` / `_list_dir`), bypassing the policy CONFIRM gate since a human already approved it.
- Actual command/file execution was factored out into `_exec_shell` / `_exec_write_file` so both the normal auto-path and `resume_after_confirm` share the same execution code.

### 4. `agent/poller.py` (updated — additive only, per constraints)
Added `GithubPoller.scan_pending(confirm_manager)`:
- Syncs the bridge repo, runs `confirm_manager.sweep_timeouts()`, then scans the local pending queue for `pending-confirm-<id>.json` files that now have a matching `approved-<id>.json`/`denied-<id>.json`.
- Returns `[(Task, approved), ...]` for the main loop to feed into `Executor.resume_after_confirm()`, and removes the picked-up pending marker.

Note: another agent concurrently added AMP-envelope parsing (`Task.envelope`, `_task_from_amp`/`_task_from_legacy`) to this same file. Both changes coexist cleanly — verified via the full test run (`tests/test_amp.py` + the new tests all pass together).

### 5. `agent/main.py` (updated)
- Added `_write_lock()` / `_remove_lock()` writing the agent's PID to `~/.iddo-harness/agent.lock` on startup and removing it on exit (used by `iddo-harness status` to determine "agent running").
- Wired a `ConfirmManager` instance shared between `Executor` and the poller.
- At the top of every polling cycle, calls `poller.scan_pending(confirm_manager)` first and runs `executor.resume_after_confirm(...)` + `reporter.send(...)` for anything resolved, **before** fetching brand-new tasks — so approved/denied confirmations are always drained first.

### 6. `policy.yaml` / `agent/config.py` (updated)
- Added a `confirm: { timeout_minutes: 30 }` section to `policy.yaml`.
- Added `Config.confirm: dict` and wired `load_config()` to populate it (defaulting to `{"timeout_minutes": 30}` if absent).
- Also fixed a latent bug in `config.py`: `os.getlogin()` raises `OSError` in ttyless/sandboxed environments (hit while writing tests); added a safe `_default_owner()` fallback (`os.getlogin()` → `$USER`/`$USERNAME` → `"unknown"`). This is a robustness fix, not a behavior change for normal interactive use.

### 7. `tests/test_cli.py`
Click `CliRunner`-based smoke tests: `policy check` (auto/block/confirm decisions), `submit` (writes a task file to the bridge dir; validates required-option errors for shell/write_file), `status` (not-running vs. running-via-lock-file cases). Uses a throwaway `policy.yaml` and monkeypatches `Path.home()`/`tempfile.gettempdir()` so no test touches the real `~/.iddo-harness` or a shared `/tmp` bridge checkout.

### 8. `tests/test_confirm.py`
Covers all three required scenarios plus edges:
- (a) `Decision.CONFIRM` → executor writes `pending-confirm-<id>.json` with the correct one-liner.
- (b) An `approved-<id>.json` file → `poller.scan_pending()` surfaces the task → `executor.resume_after_confirm()` actually runs it (verified via captured stdout of a real `echo` subprocess call); a `denied-<id>.json` file drops the task instead.
- (c) A pending confirmation backdated past 30 minutes is auto-denied by `sweep_timeouts()`; a fresh one is not; a custom `confirm.timeout_minutes` value is respected.

### 9. `requirements.txt` (repo root)
```
PyYAML>=6.0
click>=8.1
pytest>=7
```

### 10. `pyproject.toml` (repo root)
Minimal setuptools-based config; `packages = ["agent"]`; console script entry point `iddo-harness = "agent.cli:cli"`. Verified `pip install -e .` produces a working `iddo-harness` executable.

### Other changes
- Added `agent/__init__.py` (empty package marker) so `agent` is importable as a package for the console-script entry point, while all existing modules keep their original flat, script-style imports (`from config import ...` etc.) for backward compatibility — `cli.py`/`executor.py`/`confirm.py` use `try: from X import Y / except ImportError: from agent.X import Y` so they work both when run flat (agent/ on `sys.path`, as `systemd`'s `PYTHONPATH=/opt/iddo-harness/agent` does) and when imported as the `agent` package.

## Verification
- `python3 -m pytest tests/` → **56 passed** (includes the other agent's pre-existing `tests/test_amp.py`, confirming no regressions).
- `pip install -e .` succeeds; `iddo-harness --help`, `iddo-harness --config policy.yaml policy check "ls -la"`, and `iddo-harness --config policy.yaml status` all run correctly end-to-end.
- Cross-checked against concurrent work from other agents:
  - `agent/amp.py`, `agent/poller.py`'s AMP-envelope parsing, and `agent/reporter.py`'s AMP-wrapped results were already present/updated by another agent; `cli.py`'s `submit` and `status` were adjusted to build valid AMP envelopes (`build_envelope(...)`, using a `session:cli-local` reply address to satisfy AMP's canonical-identity format) and to read AMP-wrapped or legacy result shapes respectively.
  - `installer/install_linux.sh` and its systemd unit already assume `pip install -e .` yields an `iddo-harness` console script and run it as `iddo-harness --config /etc/iddo-harness/policy.yaml run` with `PYTHONPATH=/opt/iddo-harness/agent` — this matches the CLI and packaging built here with no changes needed on the installer side.

## Known follow-up for the parent agent
- `patches/cli_llm_commands.py` was left by another agent as instructions to merge `ask`/`models` subcommands into `agent/cli.py`, written under the assumption that `cli.py` is **argparse**-based. Since this task explicitly required a **click**-based CLI, that patch's exact code (argparse `subparsers.add_parser`, `set_defaults(func=...)`) is not directly compatible with the click `Group` built here. The underlying handler logic (`cmd_ask`/`cmd_models` bodies) is reusable, but they'll need to be re-wired as `@cli.command()` functions using click decorators rather than merged as-is. Flagging this for the parent agent / the LLM-integration agent to reconcile rather than silently converting their patch myself.
