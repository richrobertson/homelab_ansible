#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-}"
SSH_USER="${2:-}"     # SSH login user
SUDO_USER="${3:-}"    # User that should get NOPASSWD sudo

if [[ -z "$HOST" || -z "$SSH_USER" || -z "$SUDO_USER" ]]; then
	echo "Usage: $0 <host_or_ip> <ssh_user> <sudo_user>"
	echo "Example: $0 server.example.com admin admin"
	exit 1
fi

SUDOERS_FILE="/etc/sudoers.d/90-${SUDO_USER}-nopasswd"
RULE="${SUDO_USER} ALL=(ALL:ALL) NOPASSWD: ALL"

echo "Configuring passwordless sudo for '${SUDO_USER}' on ${HOST} (via ${SSH_USER})..."

ssh -tt -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "${SSH_USER}@${HOST}" "
set -euo pipefail
tmp_file=\$(mktemp)

printf '%s\n' '${RULE}' > \"\$tmp_file\"

# Prompt once for sudo password, then require non-interactive sudo for the rest
sudo -v
sudo -n chown root:root \"\$tmp_file\"
sudo -n chmod 0440 \"\$tmp_file\"

# Validate syntax before installing
sudo -n visudo -cf \"\$tmp_file\"

# Fix permissions on any existing sudoers.d files that are not 0440
sudo -n find /etc/sudoers.d/ -type f ! -perm 0440 -exec chmod 0440 {} \;

# Install and validate full sudoers config
sudo -n install -o root -g root -m 0440 \"\$tmp_file\" '${SUDOERS_FILE}'
sudo -n rm -f \"\$tmp_file\"
sudo -n visudo -c

# Verify non-interactive sudo works
sudo -n true
echo 'Passwordless sudo is enabled for ${SUDO_USER}'
"

echo "Done."