#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required. Install it with: sudo dnf install -y jq" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate BRIDGE_TOKEN." >&2
  exit 1
fi

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  bridge_token="$(openssl rand -hex 32)"
  sed -i "s/^BRIDGE_TOKEN=.*/BRIDGE_TOKEN=$bridge_token/" .env
  chmod 600 .env
  echo "Created .env with a generated BRIDGE_TOKEN."
  echo "Fill in DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, and DISCORD_ALLOWED_USER_IDS."
fi

mkdir -p ~/.local/state/claude-discord-bridge
chmod +x notify.sh
service_path="$(pwd)/claude-discord-bridge.service"
if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl --user link "$service_path"; then
    echo "Could not link $service_path automatically; run systemctl --user link $service_path" >&2
  fi
else
  echo "systemctl not found; run systemctl --user link $service_path on the target host" >&2
fi

echo "Done. Next:"
echo "  1. Edit .env"
echo "  2. systemctl --user daemon-reload"
echo "  3. systemctl --user enable --now claude-discord-bridge.service"
