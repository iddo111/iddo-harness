#!/usr/bin/env bash
# Iddo Harness — Linux/DGX installer
# usage:  curl -sSL https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_linux.sh | bash

set -euo pipefail

echo "=============================================="
echo "  Iddo Harness — Linux installer"
echo "=============================================="

INSTALL_DIR="$HOME/.iddo-harness"
REPO_DIR="$INSTALL_DIR/src"
mkdir -p "$INSTALL_DIR"

# --- prerequisites
echo ""
echo "[1/5] Checking prerequisites..."
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
command -v git >/dev/null || { echo "git required"; exit 1; }
command -v gh >/dev/null || { echo "gh CLI required — install: https://cli.github.com/"; exit 1; }
echo "  ✓ python3: $(python3 --version)"
echo "  ✓ git:     $(git --version)"
echo "  ✓ gh:      $(gh --version | head -1)"

# --- clone / update
echo ""
echo "[2/5] Cloning iddo-harness..."
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" pull --quiet
else
  git clone --depth 1 https://github.com/iddo111/iddo-harness.git "$REPO_DIR"
fi

# --- deps
echo ""
echo "[3/5] Installing Python dependencies..."
python3 -m pip install --user --quiet pyyaml requests

# --- config
echo ""
echo "[4/5] Setting up config..."
CFG="$INSTALL_DIR/policy.yaml"
if [ ! -f "$CFG" ]; then
  cp "$REPO_DIR/policy.yaml" "$CFG"
  echo "  policy.yaml → $CFG"
else
  echo "  policy.yaml already exists"
fi

# --- systemd service
echo ""
echo "[5/5] Registering systemd user service..."
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/iddo-harness.service" <<EOF
[Unit]
Description=Iddo Harness agent
After=network-online.target

[Service]
Type=simple
ExecStart=$(command -v python3) $REPO_DIR/agent/main.py --config $CFG
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now iddo-harness.service

echo ""
echo "=============================================="
echo "  Iddo Harness installed and running!"
echo "=============================================="
echo ""
echo "  Config:    $CFG"
echo "  Audit log: $INSTALL_DIR/audit.log"
echo "  Status:    systemctl --user status iddo-harness"
echo "  Logs:      journalctl --user -u iddo-harness -f"
echo "  Stop:      systemctl --user stop iddo-harness"
echo "  Uninstall: systemctl --user disable --now iddo-harness && rm -rf $INSTALL_DIR"
echo ""
