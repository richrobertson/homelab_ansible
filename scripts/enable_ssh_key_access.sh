#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-}"
USER_NAME="${2:-}"
PUB_KEY="${3:-$HOME/.ssh/id_ed25519.pub}"

if [[ -z "$HOST" || -z "$USER_NAME" ]]; then
  echo "Usage: $0 <host_or_ip> <ssh_user> [public_key_path]"
  echo "Example: $0 server.example.com admin ~/.ssh/id_ed25519.pub"
  exit 1
fi

echo "Target: ${USER_NAME}@${HOST}"

# Ensure a key exists
if [[ ! -f "$PUB_KEY" ]]; then
  echo "No public key found at $PUB_KEY, generating one..."
  ssh-keygen -t ed25519 -f "${PUB_KEY%.pub}" -N ""
fi

# Install key on remote host (prompts once for password)
if command -v ssh-copy-id >/dev/null 2>&1; then
  ssh-copy-id -i "$PUB_KEY" "${USER_NAME}@${HOST}"
else
  KEY_CONTENT="$(cat "$PUB_KEY")"
  ssh "${USER_NAME}@${HOST}" \
    "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; \
     grep -qxF '$KEY_CONTENT' ~/.ssh/authorized_keys || echo '$KEY_CONTENT' >> ~/.ssh/authorized_keys"
fi

# Verify non-interactive SSH works
ssh -o BatchMode=yes -o ConnectTimeout=8 "${USER_NAME}@${HOST}" "echo 'SSH key access OK on' \$(hostname)"