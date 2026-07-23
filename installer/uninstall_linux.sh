#!/usr/bin/env bash
# Iddo Harness — Linux uninstaller
#
# usage: sudo bash uninstall_linux.sh [--purge]
#
#   (no flags)  Stops & disables the service, removes the systemd unit and
#               /opt/iddo-harness. Leaves /etc/iddo-harness (your policy.yaml)
#               and the iddo-harness system user in place.
#   --purge     Also removes /etc/iddo-harness and deletes the iddo-harness
#               system user. Interactive confirmation unless --yes is given.
#   --yes       Skip interactive confirmation prompts (for scripted use).
#
# Safe to re-run — every step is a no-op if already removed.

set -euo pipefail

SERVICE_NAME="iddo-harness"
SERVICE_USER="iddo-harness"
INSTALL_DIR="/opt/iddo-harness"
CONFIG_DIR="/etc/iddo-harness"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 1 ;;
  esac
done

log()  { echo "  $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; }

confirm() {
  local prompt="$1"
  if [ "$ASSUME_YES" -eq 1 ]; then
    return 0
  fi
  read -r -p "$prompt [y/N] " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

echo "=============================================="
echo "  Iddo Harness — Linux uninstaller"
echo "=============================================="

if [ "$(id -u)" -ne 0 ]; then
  echo "  ✗ this uninstaller must run as root — try: sudo bash $0" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Stop + disable service
# ---------------------------------------------------------------------------
echo ""
echo "[1/4] Stopping service..."
if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1 && \
   systemctl list-unit-files "${SERVICE_NAME}.service" | grep -q "${SERVICE_NAME}.service"; then
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  ok "service stopped and disabled"
else
  ok "service not registered — nothing to stop"
fi

# ---------------------------------------------------------------------------
# 2. Remove systemd unit
# ---------------------------------------------------------------------------
echo ""
echo "[2/4] Removing systemd unit..."
if [ -f "$UNIT_PATH" ]; then
  rm -f "$UNIT_PATH"
  rm -f "/etc/systemd/system/multi-user.target.wants/${SERVICE_NAME}.service"
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
  ok "unit removed: $UNIT_PATH"
else
  ok "unit file not present — nothing to remove"
fi

# ---------------------------------------------------------------------------
# 3. Remove install dir (/opt/iddo-harness — venv, cloned source, audit log)
# ---------------------------------------------------------------------------
echo ""
echo "[3/4] Removing install directory..."
if [ -d "$INSTALL_DIR" ]; then
  rm -rf "$INSTALL_DIR"
  ok "removed $INSTALL_DIR"
else
  ok "$INSTALL_DIR not present — nothing to remove"
fi

# ---------------------------------------------------------------------------
# 4. Config + system user (only with --purge)
# ---------------------------------------------------------------------------
echo ""
echo "[4/4] Config and service user..."
if [ "$PURGE" -eq 1 ]; then
  if [ -d "$CONFIG_DIR" ]; then
    if confirm "Delete $CONFIG_DIR (your policy.yaml) permanently?"; then
      rm -rf "$CONFIG_DIR"
      ok "removed $CONFIG_DIR"
    else
      warn "kept $CONFIG_DIR"
    fi
  else
    ok "$CONFIG_DIR not present"
  fi

  if id "$SERVICE_USER" >/dev/null 2>&1; then
    if confirm "Delete system user '$SERVICE_USER'?"; then
      userdel "$SERVICE_USER" 2>/dev/null || true
      ok "removed user '$SERVICE_USER'"
    else
      warn "kept user '$SERVICE_USER'"
    fi
  else
    ok "user '$SERVICE_USER' not present"
  fi
else
  ok "kept $CONFIG_DIR and user '$SERVICE_USER' (re-run with --purge to remove them too)"
fi

echo ""
echo "=============================================="
echo "  Iddo Harness uninstalled"
echo "=============================================="
echo ""
if [ "$PURGE" -eq 0 ]; then
  echo "  Config/policy and the '$SERVICE_USER' user were left in place."
  echo "  Run 'sudo bash $0 --purge' to remove those too."
fi
echo ""
