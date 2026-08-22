#!/usr/bin/env bash
# Take a secret from the macOS clipboard and write it to Vault, without the
# value ever appearing in argv, shell history, or a transcript.
#
#   usage: clip-to-vault.sh <vault-kv-path> <field> [expected-prefix]
#   e.g.   clip-to-vault.sh secret/monitoring/gotify/prod token A
#
# Prints only the length and a sha256 prefix, which is enough to confirm the
# right thing landed in the right place. Deliberately no format/length gate:
# a length rule inferred from old credentials rejected a valid new Cloudflare
# token on 2026-08-22 and sent everyone chasing a copy/paste bug that did not
# exist. Validate against the provider instead, outside this script.
set -euo pipefail

VPATH="${1:?usage: $0 <vault-kv-path> <field> [expected-prefix]}"
FIELD="${2:?usage: $0 <vault-kv-path> <field> [expected-prefix]}"
PREFIX="${3:-}"
export VAULT_ADDR="${VAULT_ADDR:-https://vault.myrobertson.net:8200}"

command -v vault >/dev/null || { echo "vault CLI not on PATH" >&2; exit 1; }
vault token lookup >/dev/null 2>&1 || { echo "not logged in to Vault ($VAULT_ADDR)" >&2; exit 1; }

VAL=$(pbpaste | tr -d '[:space:]')
[ -n "$VAL" ] || { echo "clipboard is empty, nothing written" >&2; exit 1; }

if [ -n "$PREFIX" ] && [ "${VAL:0:${#PREFIX}}" != "$PREFIX" ]; then
  echo "clipboard does not start with '$PREFIX' -- wrong content, nothing written" >&2
  echo "  (length was ${#VAL})" >&2
  exit 1
fi

echo "  length: ${#VAL}  sha256: $(printf '%s' "$VAL" | shasum -a256 | cut -c1-16)"

# patch, not put: preserves other fields at this path.
if vault kv get "$VPATH" >/dev/null 2>&1; then
  vault kv patch "$VPATH" "$FIELD=$VAL" >/dev/null
else
  vault kv put "$VPATH" "$FIELD=$VAL" >/dev/null
fi

BACK=$(vault kv get -field="$FIELD" "$VPATH")
[ "$BACK" = "$VAL" ] || { echo "readback MISMATCH" >&2; exit 1; }
echo "  wrote $VPATH ($FIELD), readback matches"

# Stamp rotated_at, or the credential-age alert keeps firing on a credential
# that was in fact just rotated. metadata patch merges server-side; the admin
# policy gained the patch capability on 2026-08-22.
#
# ROTATED_AT overrides the default of today, and exists for ADOPTING an existing
# credential rather than rotating one. Recording today's date for a credential
# minted a year ago would reset its age to zero and hide it -- the opposite of
# why it is being tracked. Pass the earliest date the credential is known to
# have existed.
TODAY="${ROTATED_AT:-$(date -u +%Y-%m-%d)}"
if vault kv metadata patch -mount=secret -custom-metadata=rotated_at="$TODAY" "${VPATH#secret/}" >/dev/null 2>&1; then
  echo "  stamped rotated_at=$TODAY"
else
  echo "  WARNING: could not stamp rotated_at; the age alert will stay firing" >&2
fi
unset VAL BACK
