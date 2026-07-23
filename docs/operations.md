# Iddo Harness — Operator Handbook

This is the day-2 operations reference: starting/stopping the service,
finding logs, managing pending confirmations, and rolling back a bad
policy. For first-time installation, see [`../README.md`](../README.md)
and [`quickstart.md`](quickstart.md). For failure diagnosis, see
[`troubleshooting.md`](troubleshooting.md).

---

## 1. Where things live

| What | Linux | Windows |
|---|---|---|
| Installed source + venv | `/opt/iddo-harness` | `C:\Program Files\iddo-harness\src` (admin install) or `%LOCALAPPDATA%\iddo-harness\src` |
| Policy config | `/etc/iddo-harness/policy.yaml` | `%LOCALAPPDATA%\iddo-harness\policy.yaml` |
| Runtime state (audit log, queue cache, pending confirmations, lock file) | `~iddo-harness/.iddo-harness/` (i.e. `/opt/iddo-harness/.iddo-harness/`) | `%USERPROFILE%\.iddo-harness\` |
| Service definition | `/etc/systemd/system/iddo-harness.service` | Task Scheduler task `IddoHarness` |
| Service account | dedicated system user `iddo-harness` (no login shell) | the interactive user who ran the installer |

Key runtime files inside the state dir:

- `audit.log` — every decision + action the agent takes, human-readable.
- `agent.lock` — present (with the running PID) while the agent loop is alive.
- `pending/pending-confirm-<id>.json` — tasks awaiting your approval.
- `queue/`, `results/` — local caches mirrored to/from the bridge repo.

---

## 2. Start / stop / restart

### Linux (systemd)

```bash
sudo systemctl start iddo-harness
sudo systemctl stop iddo-harness
sudo systemctl restart iddo-harness
sudo systemctl status iddo-harness       # current state + last few log lines
sudo systemctl enable iddo-harness       # ensure it starts on boot (installer already does this)
sudo systemctl disable iddo-harness      # stop it from auto-starting on boot
```

The unit has `Restart=always` (`installer/systemd/iddo-harness.service`), so
a crashed agent process comes back on its own within ~10 seconds. You do not
need to babysit it.

### Windows (Task Scheduler)

```powershell
Start-ScheduledTask -TaskName IddoHarness
Stop-ScheduledTask  -TaskName IddoHarness
# "restart" = stop then start:
Stop-ScheduledTask -TaskName IddoHarness; Start-ScheduledTask -TaskName IddoHarness

Get-ScheduledTaskInfo -TaskName IddoHarness   # LastRunTime / LastTaskResult
Get-ScheduledTask     -TaskName IddoHarness   # State: Ready / Running / Disabled
```

The task is registered with `RestartCount 999` / `RestartInterval 1 minute`
and triggers `AtLogOn`, so it survives both crashes and reboots/relogins
automatically.

---

## 3. Viewing logs

### Linux

```bash
journalctl -u iddo-harness -f              # live tail (what systemd captured on stdout/stderr)
journalctl -u iddo-harness -n 200 --no-pager
sudo tail -f /opt/iddo-harness/.iddo-harness/audit.log   # the agent's own structured audit log
```

### Windows

```powershell
Get-Content "$env:USERPROFILE\.iddo-harness\audit.log" -Tail 100 -Wait
```

Or, using the CLI directly (works on both platforms, from inside the venv):

```bash
iddo-harness tail -n 100          # tail audit.log
iddo-harness tail -f              # follow, like tail -f
```

---

## 4. Viewing / resolving pending confirmations

Some actions (per `policy.yaml`'s `require_confirm` section — e.g.
`pip install`, `git push`, `systemctl start/stop`) don't run immediately.
They're parked as a **pending confirmation** and mirrored to the bridge
repo's `pending/` folder so you (or another AMP brick) can approve or deny.

Check what's waiting:

```bash
iddo-harness --config /etc/iddo-harness/policy.yaml status
```

This prints: whether the agent is running, the count + list of pending
confirmations (task id, command, reason), the last few results, and the
count of still-open tasks in the bridge repo.

Approve or deny a specific task:

```bash
iddo-harness --config /etc/iddo-harness/policy.yaml confirm <task_id> --approve
iddo-harness --config /etc/iddo-harness/policy.yaml confirm <task_id> --deny
```

Pending confirmations older than `confirm.timeout_minutes` in `policy.yaml`
(default 30) are **auto-denied** by the agent on its next poll cycle — you
don't need to manually clean up abandoned requests.

You can also dry-run the policy engine against any command to see how it
would be classified, without submitting a real task:

```bash
iddo-harness policy check "git push origin main"
# Decision: confirm
# Reason:   requires confirmation: git push*
```

---

## 5. Rolling back a bad policy

`policy.yaml` is loaded fresh on every restart of the agent (not hot-reloaded
mid-run), so the safest rollback procedure is:

1. **Stop the service first** so nothing runs against the broken policy while
   you fix it:
   ```bash
   sudo systemctl stop iddo-harness          # Linux
   Stop-ScheduledTask -TaskName IddoHarness  # Windows
   ```
2. **Restore a known-good copy.** Keep your own backups — the installer
   never overwrites an existing `policy.yaml`, but it also doesn't version
   it for you. A simple habit that works well:
   ```bash
   sudo cp /etc/iddo-harness/policy.yaml /etc/iddo-harness/policy.yaml.bak.$(date +%Y%m%d-%H%M%S)
   # ...edit...
   # if it goes wrong:
   sudo cp /etc/iddo-harness/policy.yaml.bak.<timestamp> /etc/iddo-harness/policy.yaml
   ```
   If the repo itself is a git checkout (it is, at `/opt/iddo-harness`), you
   can also diff/restore the shipped default from there:
   ```bash
   diff /etc/iddo-harness/policy.yaml /opt/iddo-harness/policy.yaml
   ```
3. **Validate before restarting.** Confirm the YAML parses and a couple of
   sample commands classify the way you expect:
   ```bash
   python3 -c "import yaml; yaml.safe_load(open('/etc/iddo-harness/policy.yaml'))" && echo "YAML OK"
   iddo-harness --config /etc/iddo-harness/policy.yaml policy check "rm -rf /"
   # Decision: block
   ```
4. **Restart and watch the logs** for a clean startup:
   ```bash
   sudo systemctl start iddo-harness
   journalctl -u iddo-harness -f
   ```
   A healthy startup logs `Iddo Harness starting up` followed by
   `Polling <repo> every <N>s` with no traceback immediately after.

If you're not sure the new policy is safe, run the agent once in the
foreground with `--once` first, against a scratch config, before pointing
the real service at it:

```bash
sudo -u iddo-harness /opt/iddo-harness/.venv/bin/iddo-harness \
  --config /path/to/candidate-policy.yaml run --once -v
```

---

## 6. Health checks & log rotation (ops/ scripts)

Two standalone scripts live in `ops/` — they don't import anything from
`agent/`, so they work even if the agent itself is down.

### `ops/health_check.py`

Runs four independent checks and prints one JSON blob: GitHub/bridge repo
reachability, disk space on the task-queue volume, time since the last poll,
and audit-log growth in the last hour. Exits non-zero if anything is failing.

```bash
/opt/iddo-harness/.venv/bin/python3 ops/health_check.py --config /etc/iddo-harness/policy.yaml --pretty
echo "exit code: $?"        # 0 = ok, 1 = warn (only with --strict), 2 = fail
```

Wire it into your monitoring of choice (cron + alert on non-zero, a
Nagios/Zabbix check, a simple systemd timer that pages you, etc.).

### `ops/rotate_logs.py`

Gzips and archives `audit.log` once it exceeds 100MB, keeping the 7 most
recent archives (both configurable). Default mode is `--mode truncate`,
which is safe to run **while the service is live** — it archives the
current bytes then truncates the same open file in place, so the running
process's file handle keeps working with no restart needed.

```bash
/opt/iddo-harness/.venv/bin/python3 ops/rotate_logs.py --pretty
# add to root's crontab (Linux):
0 3 * * * /opt/iddo-harness/.venv/bin/python3 /opt/iddo-harness/ops/rotate_logs.py >> /var/log/iddo-harness-rotate.log 2>&1
```

---

## 7. Uninstalling

```bash
# Linux — keeps policy.yaml + the iddo-harness user by default
sudo bash installer/uninstall_linux.sh
sudo bash installer/uninstall_linux.sh --purge --yes   # also wipes config + deletes the user

# Windows
powershell -ExecutionPolicy Bypass -File .\installer\uninstall_windows.ps1
powershell -ExecutionPolicy Bypass -File .\installer\uninstall_windows.ps1 -Purge -Yes
```

Both are safe to re-run — every step no-ops if already removed.
