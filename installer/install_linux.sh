#!/usr/bin/env bash
# Iddo Harness — Linux installer (systemd background service)
#
# usage:
#   sudo bash install_linux.sh
#   curl -sSL https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_linux.sh | sudo bash
#
# What this does:
#   1. Verifies OS is Ubuntu/Debian (apt-based).
#   2. Creates a dedicated, unprivileged system user `iddo-harness` (no login shell).
#   3. Clones/updates the repo into /opt/iddo-harness.
#   4. Creates a Python venv there and `pip install -e .`.
#   5. Copies policy.yaml to /etc/iddo-harness/policy.yaml (never clobbers an existing one).
#   6. Installs & enables the iddo-harness systemd service (auto-restart, boots at network-online).
#
# Safe to re-run: every step below is idempotent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------
REPO_URL="${IDDO_HARNESS_REPO_URL:-https://github.com/iddo111/iddo-harness.git}"
INSTALL_DIR="/opt/iddo-harness"
CONFIG_DIR="/etc/iddo-harness"
CONFIG_FILE="$CONFIG_DIR/policy.yaml"
SERVICE_USER="iddo-harness"
SERVICE_GROUP="iddo-harness"
SERVICE_NAME="iddo-harness"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="$INSTALL_DIR/.venv"
# Directory this script lives in — used so a local (non-piped) run can
# install from the already-checked-out source tree instead of re-cloning.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_LOCAL="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { echo "  $*"; }
step() { echo ""; echo "[$1/8] $2"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; }
die()  { echo "  ✗ $*" >&2; exit 1; }

echo "=============================================="
echo "  Iddo Harness — Linux service installer"
echo "=============================================="

# ---------------------------------------------------------------------------
# [0] Must run as root (creates system user, writes to /opt and /etc, systemd)
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  die "this installer must run as root — try: sudo bash $0"
fi

# ---------------------------------------------------------------------------
# [1/8] Detect Ubuntu/Debian
# ---------------------------------------------------------------------------
step 1 "Detecting OS..."
if [ ! -f /etc/os-release ]; then
  die "/etc/os-release not found — cannot verify this is Ubuntu/Debian"
fi
# shellcheck disable=SC1091
. /etc/os-release
DISTRO_ID="${ID:-unknown}"
DISTRO_LIKE="${ID_LIKE:-}"
case "$DISTRO_ID $DISTRO_LIKE" in
  *debian*|*ubuntu*)
    ok "detected: ${PRETTY_NAME:-$DISTRO_ID}"
    ;;
  *)
    die "unsupported distro '${PRETTY_NAME:-$DISTRO_ID}' — this installer targets Ubuntu/Debian (apt-based) systems"
    ;;
esac

command -v apt-get >/dev/null 2>&1 || die "apt-get not found — this installer requires an apt-based system"

# ---------------------------------------------------------------------------
# [2/8] Prerequisites (git, python3, venv module)
# ---------------------------------------------------------------------------
step 2 "Checking / installing prerequisites..."
NEEDED_PKGS=()
command -v git >/dev/null 2>&1 || NEEDED_PKGS+=("git")
command -v python3 >/dev/null 2>&1 || NEEDED_PKGS+=("python3")
python3 -c "import ensurepip, venv" >/dev/null 2>&1 || NEEDED_PKGS+=("python3-venv")
python3 -c "import pip" >/dev/null 2>&1 || NEEDED_PKGS+=("python3-pip")

if [ "${#NEEDED_PKGS[@]}" -gt 0 ]; then
  log "installing missing packages: ${NEEDED_PKGS[*]}"
  apt-get update -qq
  apt-get install -y -qq "${NEEDED_PKGS[@]}"
else
  ok "git, python3, venv, pip already present"
fi

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python3: $(python3 --version), venv module OK"

if ! command -v gh >/dev/null 2>&1; then
  warn "GitHub CLI ('gh') not found — the agent's poller/reporter use it to talk to the bridge repo."
  warn "Install it later with: type -p curl >/dev/null && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && apt-get update && apt-get install gh"
  warn "Then run: sudo -u $SERVICE_USER gh auth login"
fi

# ---------------------------------------------------------------------------
# [3/8] Create dedicated system user (idempotent)
# ---------------------------------------------------------------------------
step 3 "Setting up service user '$SERVICE_USER'..."
if id "$SERVICE_USER" >/dev/null 2>&1; then
  ok "user '$SERVICE_USER' already exists"
else
  useradd --system --create-home --home-dir "$INSTALL_DIR" \
          --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
  ok "created system user '$SERVICE_USER' (no login shell)"
fi

# ---------------------------------------------------------------------------
# [4/8] Clone / update repo into /opt/iddo-harness
# ---------------------------------------------------------------------------
step 4 "Installing source to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  log "existing checkout found — updating..."
  git -C "$INSTALL_DIR" fetch --quiet --depth 1 origin || warn "git fetch failed (offline?) — continuing with existing checkout"
  git -C "$INSTALL_DIR" reset --quiet --hard origin/HEAD 2>/dev/null || true
  ok "repo updated"
elif [ -f "$REPO_ROOT_LOCAL/pyproject.toml" ] && [ "$REPO_ROOT_LOCAL" != "$INSTALL_DIR" ]; then
  # Running from a local checkout (e.g. this workspace) rather than via curl|bash.
  log "installing from local source tree: $REPO_ROOT_LOCAL"
  rsync -a --delete --exclude ".venv" --exclude ".git" "$REPO_ROOT_LOCAL"/ "$INSTALL_DIR"/ 2>/dev/null \
    || cp -a "$REPO_ROOT_LOCAL"/. "$INSTALL_DIR"/
  ok "copied local source into $INSTALL_DIR"
else
  log "cloning $REPO_URL ..."
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  ok "cloned to $INSTALL_DIR"
fi

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# [5/8] Python venv + editable install
# ---------------------------------------------------------------------------
step 5 "Setting up Python virtual environment..."
if [ ! -x "$VENV_DIR/bin/python" ]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
  ok "created venv at $VENV_DIR"
else
  ok "venv already exists at $VENV_DIR"
fi

log "installing iddo-harness (pip install -e .)..."
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet --upgrade pip
if [ -f "$INSTALL_DIR/pyproject.toml" ] || [ -f "$INSTALL_DIR/setup.py" ]; then
  sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet -e "$INSTALL_DIR"
elif [ -f "$INSTALL_DIR/requirements.txt" ]; then
  warn "no pyproject.toml/setup.py found in $INSTALL_DIR — installing requirements.txt only (no console script)"
  sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
else
  warn "no pyproject.toml/setup.py/requirements.txt found in $INSTALL_DIR — falling back to a minimal dependency set"
  sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet pyyaml click requests
fi
ok "python dependencies installed into venv"

if [ -x "$VENV_DIR/bin/iddo-harness" ]; then
  ok "console script available: $VENV_DIR/bin/iddo-harness"
  if sudo -u "$SERVICE_USER" "$VENV_DIR/bin/iddo-harness" --help >/dev/null 2>&1; then
    ok "'iddo-harness --help' runs cleanly"
  else
    warn "'iddo-harness --help' failed to run — check venv / dependency install above before relying on the service"
  fi
else
  warn "no 'iddo-harness' console script produced by pip install -e . — service ExecStart may fail."
  warn "Falling back is NOT automatic; check pyproject.toml [project.scripts] in the repo."
fi

# ---------------------------------------------------------------------------
# [6/8] Config: /etc/iddo-harness/policy.yaml (never clobber)
# ---------------------------------------------------------------------------
step 6 "Setting up config at $CONFIG_FILE..."
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  ok "policy.yaml already exists at $CONFIG_FILE — leaving it untouched"
else
  if [ -f "$INSTALL_DIR/policy.yaml" ]; then
    cp "$INSTALL_DIR/policy.yaml" "$CONFIG_FILE"
    ok "copied policy.yaml → $CONFIG_FILE"
  else
    die "no policy.yaml found in $INSTALL_DIR — cannot seed default config"
  fi
fi
chown -R root:"$SERVICE_GROUP" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chmod 640 "$CONFIG_FILE"

# Runtime state dir owned by the service user (audit log, queue cache, last_poll, etc.)
mkdir -p "$INSTALL_DIR/.iddo-harness"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.iddo-harness"

# ---------------------------------------------------------------------------
# [7/8] Install systemd unit
# ---------------------------------------------------------------------------
step 7 "Installing systemd service..."
UNIT_SRC="$SCRIPT_DIR/systemd/iddo-harness.service"
if [ ! -f "$UNIT_SRC" ]; then
  # Fall back to the copy inside the installed tree (e.g. when piped via curl,
  # $SCRIPT_DIR is a temp file location without siblings — use $INSTALL_DIR instead).
  UNIT_SRC="$INSTALL_DIR/installer/systemd/iddo-harness.service"
fi
[ -f "$UNIT_SRC" ] || die "could not find iddo-harness.service unit file (looked in $SCRIPT_DIR/systemd and $INSTALL_DIR/installer/systemd)"

if [ -f "$UNIT_PATH" ] && cmp -s "$UNIT_SRC" "$UNIT_PATH"; then
  ok "unit file already up to date at $UNIT_PATH"
else
  cp "$UNIT_SRC" "$UNIT_PATH"
  chmod 644 "$UNIT_PATH"
  ok "installed unit → $UNIT_PATH"
fi

# ---------------------------------------------------------------------------
# [8/8] Enable + start
# ---------------------------------------------------------------------------
step 8 "Starting service..."
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

sleep 1
STATUS="$(systemctl is-active "$SERVICE_NAME" || true)"

echo ""
echo "=============================================="
if [ "$STATUS" = "active" ]; then
  echo "  Iddo Harness installed and RUNNING"
else
  echo "  Iddo Harness installed but status = '$STATUS'"
  echo "  Check logs below for details."
fi
echo "=============================================="
echo ""
systemctl status "$SERVICE_NAME" --no-pager -l || true
echo ""
echo "  Install dir:  $INSTALL_DIR"
echo "  Config:       $CONFIG_FILE"
echo "  Runtime data: $INSTALL_DIR/.iddo-harness/  (audit.log, queue/, results/)"
echo ""
echo "  Status:       systemctl status $SERVICE_NAME"
echo "  Logs (live):  journalctl -u $SERVICE_NAME -f"
echo "  Logs (tail):  journalctl -u $SERVICE_NAME -n 100 --no-pager"
echo "  Restart:      sudo systemctl restart $SERVICE_NAME"
echo "  Stop:         sudo systemctl stop $SERVICE_NAME"
echo "  Uninstall:    sudo bash installer/uninstall_linux.sh"
echo ""
