#!/usr/bin/env bash
# Installs voice_car_control_groq.py as a systemd service: starts on boot,
# restarts automatically if it crashes (capped at 5 restarts/60s so it
# won't crash-loop forever on a genuine problem like a bad API key).
#
# Usage (from the Pi that has the WhisPlay HAT attached):
#   sudo ./install_service.sh
#
# Requires a run.sh next to this script that launches the app with
# GROQ_API_KEY / PICO_HOST / PICO_PORT set, e.g.:
#   #!/bin/bash
#   cd ~/voice_groq && GROQ_API_KEY=gsk_... PICO_HOST=192.168.x.x PICO_PORT=8765 python3 voice_car_control_groq.py
# (run.sh isn't in this repo since it holds a live API key - create your own.)

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo ./install_service.sh" >&2
  exit 1
fi

# Who the service runs as - defaults to whoever invoked sudo. Override with:
#   sudo SERVICE_USER=someuser ./install_service.sh
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(whoami)}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run.sh"
UNIT_NAME="voice-car-control.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

if [[ ! -f "$RUN_SCRIPT" ]]; then
  echo "error: $RUN_SCRIPT not found." >&2
  echo "Create it first, e.g.:" >&2
  echo "  #!/bin/bash" >&2
  echo "  cd $SCRIPT_DIR && GROQ_API_KEY=gsk_... PICO_HOST=192.168.x.x PICO_PORT=8765 python3 voice_car_control_groq.py" >&2
  exit 1
fi
chmod +x "$RUN_SCRIPT"

echo "Installing $UNIT_NAME"
echo "  user:       $SERVICE_USER"
echo "  directory:  $SCRIPT_DIR"
echo "  run script: $RUN_SCRIPT"

cat > "$UNIT_PATH" << EOF
[Unit]
Description=Voice-controlled robot car (Groq STT) - WhisPlay HAT app
After=multi-user.target network-online.target sound.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$SCRIPT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash $RUN_SCRIPT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo
echo "Installed and started. Useful commands:"
echo "  systemctl status $UNIT_NAME"
echo "  journalctl -u $UNIT_NAME -f"
echo "  sudo systemctl stop $UNIT_NAME       # do this before running the app manually"
echo "  sudo systemctl restart $UNIT_NAME"
echo "  sudo systemctl disable --now $UNIT_NAME   # to remove it from boot"
