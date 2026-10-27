#!/usr/bin/env bash
set -euo pipefail
plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
agent_home="$HOME/.local/share/nosteam-saves"
mkdir -p "$agent_home" "$HOME/.local/bin" "$HOME/.config/systemd/user"
chmod 700 "$agent_home"
python3 -m venv "$agent_home/venv"
"$agent_home/venv/bin/python" -m pip install "$plugin_dir"
ln -sfn "$agent_home/venv/bin/nosteam" "$HOME/.local/bin/nosteam"
cat > "$HOME/.config/systemd/user/nosteam-saves.service" <<EOF
[Unit]
Description=NoSteam Saves upload retry
After=network-online.target

[Service]
ExecStart="$agent_home/venv/bin/nosteam" daemon
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now nosteam-saves.service
printf '%s\n' 'Agent installed. Connect your server in the Decky panel or run nosteam connect URL.'
