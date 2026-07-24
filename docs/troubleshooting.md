# Iddo Harness — Troubleshooting

Common failure modes and how to fix them. For routine operations
(start/stop/logs/rollback), see [`operations.md`](operations.md).

Quick first step for almost everything below:

```bash
# Linux
journalctl -u iddo-harness -n 100 --no-pager
sudo tail -100 /opt/iddo-harness/.iddo-harness/audit.log

# Windows
Get-Content "$env:USERPROFILE\.iddo-harness\audit.log" -Tail 100
```

And run the standalone probe — it checks the four most common root causes
in one shot:

```bash
python3 ops/health_check.py --config /etc/iddo-harness/policy.yaml --pretty
```

---

## 1. Bridge repo unreachable

**Symptom:** audit log / journal repeats:

```
harness.poller: Bridge sync failed: Command '['gh', 'repo', 'clone', ...]' returned non-zero exit status 4.
```

or, on later cycles once a local clone exists:

```
harness.poller: Bridge sync failed during scan_pending: ...
```

The agent treats this as non-fatal — it logs a warning and simply finds
zero pending tasks that cycle, then retries next cycle. It will **not**
crash the service. But if it persists, tasks never arrive.

**Common causes & fixes:**

- **`gh` isn't authenticated.** Exit status 4 from `gh repo clone` almost
  always means missing/expired auth. Fix:
  ```bash
  sudo -u iddo-harness gh auth login          # Linux — run as the service user
  gh auth login                               # Windows — run as the interactive user
  gh auth status                              # verify
  ```
  On Linux, remember `gh auth login` stores credentials per-user — since the
  service runs as the dedicated `iddo-harness` system user, you must
  authenticate *as that user*, not as yourself.

- **No network / DNS / proxy blocking github.com.** Test directly:
  ```bash
  curl -sS -o /dev/null -w "%{http_code}\n" https://api.github.com
  gh repo view iddo111/iddo-harness-bridge
  ```
  If corporate proxy/firewall is involved, `gh`/`git` need `HTTPS_PROXY`
  configured in the service's environment (add `Environment=HTTPS_PROXY=...`
  to the systemd unit, or a system-wide env var on Windows).

- **Repo renamed/deleted/permissions changed.** Check `transport.repo` in
  `policy.yaml` matches a repo the authenticated account can actually see:
  ```bash
  iddo-harness --config /etc/iddo-harness/policy.yaml status
  ```

- **Stale local clone in a bad state** (interrupted rebase, corrupted
  `.git`, etc). The poller/reporter clone into a temp dir
  (`$TMPDIR/iddo-harness-bridge-<repo>`), not inside the install dir — safe
  to delete and let it re-clone:
  ```bash
  rm -rf /tmp/iddo-harness-bridge-iddo111_iddo-harness-bridge   # Linux
  Remove-Item -Recurse -Force "$env:TEMP\iddo-harness-bridge-iddo111_iddo-harness-bridge"  # Windows
  sudo systemctl restart iddo-harness
  ```

---

## 2. Git push blocked

**Symptom:** results/task-status updates don't show up in the bridge repo
even though the agent logs `reported <task_id>` / `mark done` — the
underlying `git push --quiet` failed silently (poller.py/reporter.py run it
with `check=False`, so a push failure won't crash the agent, but it *will*
mean your Perplexity session never sees the result).

**Common causes & fixes:**

- **Branch protection / required reviews on the bridge repo.** The agent
  pushes directly to whatever branch is checked out — if that branch is
  protected (required PR, required status checks), a direct push is
  rejected. Either relax protection for the bot's push path, or have the
  bridge repo's default branch be an unprotected "inbox" branch.

- **Diverged history** (someone else pushed to the same branch, or the
  bridge repo state drifted). Because the poller only does a quiet
  `git pull` before writing, a genuine conflict will make the subsequent
  push fail. Fix by clearing the temp clone (see §1) so it re-clones clean
  on the next cycle — any as-yet-unpushed local commits in the stale clone
  are lost, so only do this if you don't need those specific writes
  recovered.

- **Auth token lacks `repo` scope / push permission**, especially fine-grained
  PATs. Verify with:
  ```bash
  gh auth status
  gh repo view iddo111/iddo-harness-bridge --json viewerPermission
  ```
  Needs at least `WRITE` permission on the bridge repo.

- **Detached HEAD / wrong branch checked out** in the temp clone (rare,
  usually only after manual poking around in `/tmp/iddo-harness-bridge-*`).
  Clear the temp clone (see §1) to reset it.

To confirm push actually works end to end, try it manually as the service
user:

```bash
sudo -u iddo-harness bash -c '
  cd /tmp/iddo-harness-bridge-iddo111_iddo-harness-bridge &&
  git commit --allow-empty -m "manual push test" &&
  git push
'
```

---

## 3. Policy syntax error

**Symptom:** agent fails to start, journal/log shows a traceback ending in
something like:

```
yaml.scanner.ScannerError: ...
```
or
```
FileNotFoundError: No policy.yaml found in [...]
```
or a `KeyError`/`AttributeError` deeper in `config.py` if a *required* key
(`transport.repo`, etc.) is missing entirely.

**Fixes:**

1. **Validate the YAML in isolation** before touching the service:
   ```bash
   python3 -c "import yaml; yaml.safe_load(open('/etc/iddo-harness/policy.yaml'))" && echo "YAML OK"
   ```
   Common breakers: tabs instead of spaces, an unquoted string containing
   `:` (e.g. a Windows path like `C:\Users\...` needs to stay double-quoted
   as it already is in the shipped `policy.yaml`), or a duplicate top-level
   key.

2. **Confirm the file is actually where the agent looks.** Load order is:
   explicit `--config` path (what the systemd unit / scheduled task pass)
   → `~/.iddo-harness/policy.yaml` → the repo's own `policy.yaml` as a last
   resort. If you edited the wrong copy, the agent may be silently running
   on the shipped default instead of your edits.
   ```bash
   iddo-harness --config /etc/iddo-harness/policy.yaml policy check "ls -la"
   ```
   If this errors instead of printing a Decision, the file at that exact
   path is the problem.

3. **Roll back** to a known-good copy — see
   [`operations.md` §5](operations.md#5-rolling-back-a-bad-policy).

4. **Restart only after validating**, then watch the first startup cycle:
   ```bash
   sudo systemctl restart iddo-harness
   journalctl -u iddo-harness -f
   ```

---

## 4. Task stuck in pending

**Symptom:** `iddo-harness status` shows a pending confirmation that never
resolves, or a task file sits in the bridge repo's `tasks/` folder forever
without a `done-`/`failed-` prefix appearing.

**Diagnosis steps:**

1. **Is it actually a pending *confirmation*, or just an unpolled task?**
   ```bash
   iddo-harness --config /etc/iddo-harness/policy.yaml status
   ```
   Look at "Pending confirmations" vs. "Pending tasks in bridge repo" — they're
   different queues. A require_confirm-classified command shows up in the
   first; a plain unprocessed task packet shows up in the second (and should
   clear within one poll interval, default 5s, if the agent is healthy).

2. **If it's a pending confirmation:** it auto-denies after
   `confirm.timeout_minutes` (default 30) — so "stuck forever" usually means
   either the agent isn't running at all (see §5 below) or you genuinely
   haven't responded yet:
   ```bash
   iddo-harness --config /etc/iddo-harness/policy.yaml confirm <task_id> --approve
   # or
   iddo-harness --config /etc/iddo-harness/policy.yaml confirm <task_id> --deny
   ```

3. **If it's an unpolled task sitting in `tasks/`:** the poller isn't
   running or can't reach the bridge repo. Check:
   ```bash
   sudo systemctl status iddo-harness       # is it even active?
   ls -la ~iddo-harness/.iddo-harness/agent.lock   # present + recent mtime = alive
   ```
   Then work through §1 (bridge unreachable).

4. **Malformed task packet.** If the JSON in `tasks/<id>.json` doesn't parse
   or doesn't match the expected shape (see
   [`task_packet_spec.md`](task_packet_spec.md) / AMP envelope in
   [`amp_alignment.md`](amp_alignment.md)), the poller logs `Bad task <path>: <error>`
   and skips it — it will never be picked up until the file is fixed or
   removed by hand.

5. **Task exceeded `task_timeout_seconds`** (policy.yaml `polling` section,
   default 600s) while actually running — it will be marked `failed-` with a
   `"timeout"` error, not stuck; if you see it truly wedged with no
   done/failed marker at all after several minutes, that points back to #3
   (agent not polling) rather than a slow task.

---

## 5. "Is the agent even running?" — quick liveness checklist

```bash
# Linux
systemctl is-active iddo-harness
sudo ls -la /opt/iddo-harness/.iddo-harness/agent.lock   # exists + recent mtime while alive

# Windows
(Get-ScheduledTask -TaskName IddoHarness).State          # should be "Running"
Get-ScheduledTaskInfo -TaskName IddoHarness | Select LastRunTime, LastTaskResult
```

Or just run the health probe, which checks last-poll age + audit log growth
for you and gives one clear pass/fail:

```bash
python3 ops/health_check.py --config /etc/iddo-harness/policy.yaml --pretty
```

If `last_poll` or `audit_growth` come back `warn`/`fail` while the service
shows `active (running)`, the process is alive but wedged — restart it:

```bash
sudo systemctl restart iddo-harness
```
