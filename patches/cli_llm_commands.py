"""
patches/cli_llm_commands.py — PATCH, not a standalone module.

agent/cli.py is owned by another agent and is NOT modified directly by
this change (per task constraints). This file contains the exact `click`
commands to merge into agent/cli.py to add:

    iddo-harness ask <role> "<prompt>"   — one-shot LlmDrivenLoop invocation
    iddo-harness models                  — list configured LLM backends + health

agent/cli.py (as of this patch) is a `click.group()` named `cli`, with
existing commands `run`, `submit`, `status`, `confirm`, `tail`, and a
`policy` subgroup. It uses flat imports (`from config import load_config`)
with an `agent.`-prefixed ImportError fallback, and a shared
`ctx.obj["config_path"]` set by the top-level group.

HOW TO MERGE
============
1. Add the imports below to agent/cli.py's existing `try/except ImportError`
   import block (same pattern already used there for config/confirm/policy).
2. Paste the two `@cli.command()` functions anywhere after the existing
   commands (e.g. right after `tail`, before the `policy` group).
3. No other wiring needed — click auto-registers commands decorated with
   `@cli.command()` onto the `cli` group.

Nothing here modifies existing commands or their behavior.
"""

# =============================================================================
# 1. IMPORTS TO ADD to agent/cli.py's existing try/except ImportError block
# =============================================================================
IMPORTS_TO_ADD = '''
try:
    from llm_router import LlmRouter
    from llm_loop import LlmDrivenLoop, DEFAULT_MAX_ITERATIONS
    from executor import Executor
except ImportError:  # pragma: no cover - fallback when installed as a package
    from agent.llm_router import LlmRouter
    from agent.llm_loop import LlmDrivenLoop, DEFAULT_MAX_ITERATIONS
    from agent.executor import Executor
'''

# =============================================================================
# 2. CLICK COMMANDS TO ADD (paste verbatim into agent/cli.py)
# =============================================================================
COMMANDS_TO_ADD = '''
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
    """One-shot LLM-driven task: iddo-harness ask <role> \\"<prompt>\\"

    ROLE must match a role under `llm_backends:` in policy.yaml (e.g.
    coder, reasoner, planner). The model is given the harness's shell /
    read_file / write_file / list_dir tools and runs in a loop, subject to
    the same policy engine as everything else — a CONFIRM decision pauses
    the loop and prints instructions for approving via `iddo-harness confirm`.
    """
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
            f"\\n[CONFIRM REQUIRED] tool={fn.get('name')} args={fn.get('arguments')}\\n"
            f"reason: {reason}"
        )
        click.echo("Approve with `iddo-harness confirm <task_id> --approve`, then re-run `ask` to continue.")

    loop = LlmDrivenLoop(
        router=router,
        policy=policy,
        executor=executor,
        role=role,
        max_iterations=max_iterations,
        on_confirm_required=_on_confirm_required,
    )

    try:
        result = loop.run(prompt)
    finally:
        router.stop()

    if result.paused:
        click.echo("\\n--- PAUSED (awaiting confirmation) ---")
        click.echo(json.dumps(result.pending_confirmation, indent=2, default=str))
        ctx.exit(2)
    elif result.done:
        click.echo("\\n--- DONE ---")
        click.echo(result.final_message or "(no final message)")
    else:
        click.echo(f"\\n--- STOPPED (max_iterations={max_iterations} reached) ---")
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
'''
