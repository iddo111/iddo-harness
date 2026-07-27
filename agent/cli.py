"""
iddo-harness — user-facing CLI.

Thin click wrapper around the existing agent modules (main, poller, executor,
policy, confirm). Installed as the `iddo-harness` console script via
pyproject.toml (`agent.cli:cli`).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import click

try:
    from config import load_config
    from confirm import ConfirmManager
    from policy import PolicyEngine
    from llm_router import LlmRouter
    from llm_loop import LlmDrivenLoop, DEFAULT_MAX_ITERATIONS
    from executor import Executor
    from audit import AuditLog
    from secrets_vault import SecretVault, VaultError
except ImportError:  # pragma: no cover - fallback when installed as a package
    from agent.config import load_config
    from agent.confirm import ConfirmManager
    from agent.policy import PolicyEngine
    from agent.llm_router import LlmRouter
    from agent.llm_loop import LlmDrivenLoop, DEFAULT_MAX_ITERATIONS
    from agent.executor import Executor
    from agent.audit import AuditLog
    from agent.secrets_vault import SecretVault, VaultError

log = logging.getLogger("harness.cli")

LOCK_PATH = Path.home() / ".iddo-harness" / "agent.lock"
AUDIT_LOG_PATH = Path.home() / ".iddo-harness" / "audit.log"


def _setup_logging(verbose: bool = False):
    root = Path.home() / ".iddo-harness"
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(root / "audit.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def _bridge_local_dir(cfg) -> Path:
    repo = cfg.transport["repo"]
    return Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{repo.replace('/', '_')}"


def _sync_bridge(cfg, local: Path):
    if not local.exists():
        log.info(f"Cloning bridge repo {cfg.transport['repo']} into {local}")
        subprocess.run(
            ["gh", "repo", "clone", cfg.transport["repo"], str(local)],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(local), "pull", "--quiet"],
            check=False, capture_output=True,
        )


def _commit_and_push(local: Path, msg: str):
    subprocess.run(["git", "-C", str(local), "add", "-A"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", msg, "--allow-empty"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(local), "push", "--quiet"], check=False, capture_output=True)


# ---------------------------------------------------------------------------
@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.option("--config", default=None, help="Path to policy.yaml (defaults to the usual search path).")
@click.pass_context
def cli(ctx, verbose, config):
    """iddo-harness — CLI for the Iddo Harness agent."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose


# ---------------------------------------------------------------------------
@cli.command()
@click.option("--once", is_flag=True, help="Run a single polling cycle and exit.")
@click.pass_context
def run(ctx, once):
    """Start the main polling agent (blocks until stopped)."""
    try:
        import main as agent_main
    except ImportError:  # pragma: no cover
        from agent import main as agent_main

    argv = []
    if ctx.obj.get("config_path"):
        argv += ["--config", ctx.obj["config_path"]]
    if once:
        argv.append("--once")
    if ctx.obj.get("verbose"):
        argv.append("--verbose")

    old_argv = sys.argv
    sys.argv = ["iddo-harness-agent", *argv]
    try:
        agent_main.main()
    finally:
        sys.argv = old_argv


# ---------------------------------------------------------------------------
@cli.command()
@click.argument("kind", type=click.Choice(["shell", "read_file", "write_file", "list_dir"]))
@click.option("--command", default=None, help="Shell command (for kind=shell).")
@click.option("--path", default=None, help="Target path (for read_file/write_file/list_dir).")
@click.option("--content", default=None, help="File content (for kind=write_file).")
@click.option("--timeout", "timeout_sec", default=300, type=int, help="Timeout in seconds.")
@click.option("--priority", default="normal", help="Task priority.")
@click.option("--no-push", is_flag=True, help="Write the task locally but skip git push.")
@click.pass_context
def submit(ctx, kind, command, path, content, timeout_sec, priority, no_push):
    """Create an AMP task envelope and push it to the bridge repo's tasks/ dir."""
    try:
        import amp
    except ImportError:
        amp = None
        log.warning("agent.amp not available yet — falling back to plain task packet format")

    cfg = load_config(ctx.obj.get("config_path"))

    env_id = str(uuid.uuid4())

    payload: dict = {"timeout_sec": timeout_sec}
    if kind == "shell":
        if not command:
            raise click.UsageError("kind=shell requires --command")
        payload["command"] = command
        payload["paths"] = [path] if path else []
    elif kind == "read_file":
        if not path:
            raise click.UsageError("kind=read_file requires --path")
        payload["path"] = path
    elif kind == "write_file":
        if not path:
            raise click.UsageError("kind=write_file requires --path")
        payload["path"] = path
        payload["content"] = content or ""
    elif kind == "list_dir":
        if not path:
            raise click.UsageError("kind=list_dir requires --path")
        payload["path"] = path

    body = {"kind": kind, "priority": priority, **payload}

    envelope = None
    if amp is not None:
        try:
            envelope = amp.build_envelope(
                direction="outbound",
                source_brick="iddo-harness-cli",
                source_instance="cli-local",
                channel="harness",
                identity_canonical=f"user:{cfg.owner}",
                payload_type="harness_task",
                payload_body=body,
                to_channel="harness",
                to_address="session:cli-local",
                env_id=env_id,
            )
        except Exception as e:
            log.warning(f"build_envelope failed ({e}); falling back to plain task packet")
            envelope = None

    if envelope is not None:
        record = envelope.to_dict()
        task_id = envelope.id
    else:
        task_id = env_id
        record = {"id": task_id, "kind": kind, "priority": priority, "payload": payload}

    local = _bridge_local_dir(cfg)
    task_dir = local / cfg.transport.get("task_dir", "tasks/")
    task_dir.mkdir(parents=True, exist_ok=True)

    if not no_push:
        try:
            _sync_bridge(cfg, local)
        except Exception as e:
            log.warning(f"bridge sync failed, writing locally only: {e}")

    out_path = task_dir / f"{task_id}.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"wrote task {task_id} -> {out_path}")

    if not no_push:
        _commit_and_push(local, f"submit task: {task_id}")

    click.echo(f"Submitted task {task_id} ({kind}) -> {out_path}")


# ---------------------------------------------------------------------------
@cli.command()
@click.option("-n", "--num-results", default=5, type=int, help="Number of recent results to show.")
@click.pass_context
def status(ctx, num_results):
    """Show agent status: running?, last N results, pending confirmation count."""
    cfg = load_config(ctx.obj.get("config_path"))

    running = LOCK_PATH.exists()
    click.echo(f"Agent running: {'yes' if running else 'no'} (lock: {LOCK_PATH})")

    confirm_manager = ConfirmManager(cfg)
    pending = confirm_manager.list_pending()
    click.echo(f"Pending confirmations: {len(pending)}")
    for pc in pending:
        click.echo(f"  - {pc.task_id}: {pc.command!r} ({pc.reason})")

    local = _bridge_local_dir(cfg)
    result_dir = local / cfg.transport.get("result_dir", "results/")
    results = []
    if result_dir.exists():
        results = sorted(result_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    click.echo(f"Last {min(num_results, len(results))} result(s):")
    for p in results[:num_results]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Results may be a plain legacy dict, or a full AMP envelope
            # (payload.type == "harness_result") with the actual result
            # nested at payload.body — see reporter.py.
            body = data.get("payload", {}).get("body", data) if "payload" in data else data
            click.echo(
                f"  - {body.get('task_id', p.stem)}: ok={body.get('ok')} "
                f"decision={body.get('decision')}"
            )
        except Exception as e:
            click.echo(f"  - {p.name}: <unreadable: {e}>")

    task_dir = local / cfg.transport.get("task_dir", "tasks/")
    pending_tasks = 0
    if task_dir.exists():
        pending_tasks = len([
            p for p in task_dir.glob("*.json")
            if not p.name.startswith(("done-", "failed-"))
        ])
    click.echo(f"Pending tasks in bridge repo: {pending_tasks}")


# ---------------------------------------------------------------------------
@cli.command()
@click.argument("task_id")
@click.option("--approve", "decision", flag_value="approve", help="Approve the pending task.")
@click.option("--deny", "decision", flag_value="deny", help="Deny the pending task.")
@click.pass_context
def confirm(ctx, task_id, decision):
    """Respond to a pending confirmation (approve or deny)."""
    if decision is None:
        raise click.UsageError("Specify --approve or --deny")

    cfg = load_config(ctx.obj.get("config_path"))
    approve = decision == "approve"

    # ApprovalManager so the decision lands in the tamper-evident audit chain
    # with who made it. It falls back to plain ConfirmManager behaviour when the
    # audit log cannot be opened, so approving never depends on auditing.
    try:
        try:
            from approval import ApprovalManager
        except ImportError:
            from agent.approval import ApprovalManager
        manager = ApprovalManager(cfg, audit_log=AuditLog.from_config(cfg))
        path = manager.respond(task_id, approve=approve, actor="user")
    except Exception as e:
        log.warning(f"approval manager unavailable ({e}) — using the plain confirm queue")
        path = ConfirmManager(cfg).respond(task_id, approve=approve)

    verb = "approved" if approve else "denied"
    click.echo(f"Task {task_id} {verb}. Written to {path}")


# ---------------------------------------------------------------------------
@cli.command()
@click.option("-n", "--lines", default=50, type=int, help="Number of lines to show.")
@click.option("-f", "--follow", is_flag=True, help="Follow the log file (like tail -f).")
def tail(lines, follow):
    """Tail the audit log (~/.iddo-harness/audit.log)."""
    if not AUDIT_LOG_PATH.exists():
        click.echo(f"No audit log found at {AUDIT_LOG_PATH}")
        return

    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(AUDIT_LOG_PATH))

    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        # tail not available (e.g. some minimal environments) — fallback to Python
        text = AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines()[-lines:]:
            click.echo(line)


# ---------------------------------------------------------------------------
@cli.group(name="policy")
def policy_group():
    """Policy engine utilities."""


@policy_group.command(name="check")
@click.argument("command")
@click.option("--path", "paths", multiple=True, help="Target path(s) to also evaluate.")
@click.pass_context
def policy_check(ctx, command, paths):
    """Run the policy engine dry against COMMAND and show Decision + reason."""
    cfg = load_config(ctx.obj.get("config_path"))
    engine = PolicyEngine(cfg)
    decision, reason = engine.decide(command, list(paths))
    click.echo(f"Decision: {decision.value}")
    click.echo(f"Reason:   {reason}")


@policy_group.command(name="lint")
@click.argument("path", required=False, type=click.Path())
@click.option("--strict", is_flag=True, help="Treat warnings as errors.")
@click.pass_context
def policy_lint_cmd(ctx, path, strict):
    """Lint policy.yaml for empty block lists, bad patterns and contradictions."""
    try:
        from installer.policy_lint import main as lint_main
    except ImportError as e:  # pragma: no cover - installer/ ships with the repo
        raise click.ClickException(f"policy linter unavailable: {e}")

    argv = [path or ctx.obj.get("config_path") or ""]
    argv = [a for a in argv if a]
    if strict:
        argv.append("--strict")
    ctx.exit(lint_main(argv))


# ---------------------------------------------------------------------------
@cli.group(name="secret")
def secret_group():
    """Encrypted secrets vault. Reference values as {{secret:name}} in a task."""


@secret_group.command(name="set")
@click.argument("name")
@click.option(
    "--stdin", "from_stdin", is_flag=True,
    help="Read the value from stdin instead of prompting (for scripts and pipes).",
)
@click.pass_context
def secret_set(ctx, name, from_stdin):
    """Store a secret. The value is never taken from the command line.

    Passing a credential as an argument would put it in the shell history, in
    `ps` output, and in this process's argv — so it is read from a hidden prompt
    or from stdin instead.
    """
    cfg = load_config(ctx.obj.get("config_path"))
    if from_stdin:
        value = sys.stdin.read().rstrip("\n")
    else:
        value = click.prompt(f"Value for {name}", hide_input=True, confirmation_prompt=True)
    if not value:
        raise click.ClickException("refusing to store an empty secret")

    try:
        vault = SecretVault.from_config(cfg, audit_sink=AuditLog.from_config(cfg))
        vault.set(name, value)
    except VaultError as e:
        raise click.ClickException(str(e))
    click.echo(f"Stored secret {name!r}. Use it as {{{{secret:{name}}}}} in a task payload.")


@secret_group.command(name="list")
@click.pass_context
def secret_list(ctx):
    """List secret names. Values are never printed."""
    cfg = load_config(ctx.obj.get("config_path"))
    try:
        vault = SecretVault.from_config(cfg)
        names = vault.names()
    except VaultError as e:
        raise click.ClickException(str(e))
    if not names:
        click.echo("Vault is empty.")
        return
    for n in names:
        click.echo(n)


@secret_group.command(name="rm")
@click.argument("name")
@click.pass_context
def secret_rm(ctx, name):
    """Delete a secret from the vault."""
    cfg = load_config(ctx.obj.get("config_path"))
    try:
        vault = SecretVault.from_config(cfg, audit_sink=AuditLog.from_config(cfg))
        removed = vault.delete(name)
    except VaultError as e:
        raise click.ClickException(str(e))
    click.echo(f"Deleted {name!r}." if removed else f"No such secret: {name!r}")


# ---------------------------------------------------------------------------
@cli.group(name="audit")
def audit_group():
    """The hash-chained audit log (~/.iddo-harness/audit.jsonl)."""


@audit_group.command(name="tail")
@click.option("-n", "--lines", default=20, type=int, help="Number of records to show.")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON lines.")
@click.pass_context
def audit_tail(ctx, lines, as_json):
    """Show the last N audit records."""
    cfg = load_config(ctx.obj.get("config_path"))
    records = AuditLog.from_config(cfg).tail(lines)
    if not records:
        click.echo("No audit records yet.")
        return
    for rec in records:
        if as_json:
            click.echo(json.dumps(rec, ensure_ascii=False))
        else:
            click.echo(
                f"{rec.get('ts', '?')}  {rec.get('outcome', '?'):<5} "
                f"{rec.get('actor', '?')}  {rec.get('action', '?')}  {rec.get('resource', '')}"
            )


@audit_group.command(name="verify")
@click.pass_context
def audit_verify(ctx):
    """Verify the HMAC chain. Reports the first line that does not match."""
    cfg = load_config(ctx.obj.get("config_path"))
    audit_log = AuditLog.from_config(cfg)
    ok, line_no, detail = audit_log.verify_chain()
    if ok:
        click.echo(f"Audit chain intact: {audit_log.path}")
        return
    click.echo(f"Audit chain BROKEN at line {line_no}: {detail}")
    ctx.exit(1)


if __name__ == "__main__":
    cli()


# ---------------------------------------------------------------------------
@cli.command()
@click.argument("role")
@click.argument("prompt")
@click.option(
    "--max-iterations", default=DEFAULT_MAX_ITERATIONS, type=int,
    help=f"Max tool-call round-trips before giving up (default {DEFAULT_MAX_ITERATIONS}).",
)
@click.pass_context
def ask(ctx, role, prompt, max_iterations):
    """One-shot LLM-driven task: iddo-harness ask <role> "<prompt>" """
    cfg = load_config(ctx.obj.get("config_path"))
    router = LlmRouter.from_config(cfg, auto_start=True)
    if not router.roles:
        raise click.ClickException(
            "No llm_backends configured in policy.yaml — see docs/llm_integration.md"
        )
    if role not in router.roles:
        raise click.ClickException(
            f"Unknown role '{role}'. Configured roles: {sorted(router.roles)}"
        )

    policy = PolicyEngine(cfg)
    executor = Executor(policy)

    def _on_confirm_required(tool_call, reason):
        fn = tool_call.get("function", {})
        click.echo(
            f"\n[CONFIRM REQUIRED] tool={fn.get('name')} args={fn.get('arguments')}\n"
            f"reason: {reason}"
        )
        click.echo("Approve with `iddo-harness confirm <task_id> --approve`, then re-run `ask` to continue.")

    loop = LlmDrivenLoop(
        router=router, policy=policy, executor=executor,
        role=role, max_iterations=max_iterations,
        on_confirm_required=_on_confirm_required,
    )

    try:
        result = loop.run(prompt)
    finally:
        router.stop()

    if result.paused:
        click.echo("\n--- PAUSED (awaiting confirmation) ---")
        click.echo(json.dumps(result.pending_confirmation, indent=2, default=str))
        ctx.exit(2)
    elif result.done:
        click.echo("\n--- DONE ---")
        click.echo(result.final_message or "(no final message)")
    else:
        click.echo(f"\n--- STOPPED (max_iterations={max_iterations} reached) ---")
        ctx.exit(3)


# ---------------------------------------------------------------------------
@cli.command(name="models")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@click.pass_context
def models(ctx, as_json):
    """List configured LLM backends (llm_backends in policy.yaml) and their health."""
    cfg = load_config(ctx.obj.get("config_path"))
    router = LlmRouter.from_config(cfg, auto_start=False)
    if not router.roles:
        raise click.ClickException(
            "No llm_backends configured in policy.yaml — see docs/llm_integration.md"
        )
    router.check_all_once()
    rows = router.status()

    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    click.echo(f"{'ROLE':<12} {'SLOT':<10} {'MODEL':<24} {'BASE_URL':<38} {'HEALTHY'}")
    for row in rows:
        healthy = "yes" if row["healthy"] else "NO"
        click.echo(f"{row['role']:<12} {row['slot']:<10} {row['model']:<24} {row['base_url']:<38} {healthy}")
