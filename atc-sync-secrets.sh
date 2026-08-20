#!/usr/bin/env bash
# atc-sync-secrets.sh
#
# Run this ON YOUR MAC. It:
#   1. SSHes to hermes-host and asks atc which secret keys config.yaml
#      references but that aren't in its keychain yet.
#   2. For each missing key, looks it up in YOUR MAC'S Keychain
#      (same service/account naming atc itself uses: service "atc",
#      account <key-name> - i.e. wherever you (or the local atc CLI)
#      already stored it via `security add-generic-password` or
#      `atc secrets set` run locally).
#   3. Shows you what it found and lets you approve copying each one
#      over, individually - nothing is sent without your go-ahead.
#   4. Sends the value to hermes-host over SSH as an argv entry passed
#      through ssh's own argument vector (never interpolated into a
#      shell string, never echoed, never written to disk in plaintext
#      on either end) into `atc secrets set <key> <value>`.
#
# Nothing is cached, logged, or left in shell history: values live only
# in memory for the few milliseconds it takes to pipe them over ssh.
#
# Usage:
#   ./atc-sync-secrets.sh [user@host]
#
# Defaults to dmp@192.168.1.132 if no host given.

set -euo pipefail

REMOTE="${1:-dmp@192.168.1.132}"
REMOTE_CLI_DIR="repos/pentagon-management/infrastructure/cli"
REMOTE_ATC="\$HOME/$REMOTE_CLI_DIR/atc"   # absolute path - non-interactive
                                          # ssh commands don't source
                                          # .zshrc/.bashrc, so PATH can't
                                          # be relied on here.
# uv lives under linuxbrew on hermes-host, only on PATH in interactive
# shells; prepend it explicitly for non-interactive ssh commands.
REMOTE_PATH_PREFIX='PATH="$HOME/bin:$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"'
KEYCHAIN_SERVICE="atc"

echo "Checking hermes-host ($REMOTE) for missing secrets..."
echo

# Ask the remote atc which keys config.yaml references but aren't set yet.
MISSING_RAW="$(ssh "$REMOTE" -- sh -c "$REMOTE_PATH_PREFIX '$REMOTE_ATC' secrets scan" 2>&1 || true)"

mapfile -t KEYS < <(printf '%s\n' "$MISSING_RAW" | sed -n 's/^! Not in keychain: \([^ ]*\).*/\1/p')

if [ "${#KEYS[@]}" -eq 0 ]; then
    echo "Remote reports no missing secrets. Nothing to do."
    echo "(Output was: $MISSING_RAW)"
    exit 0
fi

echo "Remote is missing ${#KEYS[@]} secret(s):"
for k in "${KEYS[@]}"; do echo "  - $k"; done
echo

SENT=0
SKIPPED=0
NOT_FOUND_LOCALLY=0

for key in "${KEYS[@]}"; do
    echo "──────────────────────────────────────────"
    echo "Key: $key"

    # Look it up in this Mac's Keychain without printing it.
    if ! value="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$key" -w 2>/dev/null)"; then
        echo "  Not found in this Mac's Keychain (service=$KEYCHAIN_SERVICE, account=$key)."
        echo "  Skipping - set it manually on hermes-host later with:"
        echo "    ssh $REMOTE $REMOTE_ATC secrets set $key"
        NOT_FOUND_LOCALLY=$((NOT_FOUND_LOCALLY + 1))
        echo
        continue
    fi

    echo "  Found locally. Length: ${#value} chars."
    read -r -p "  Copy to hermes-host? [y/N/q(uit)] " ans
    case "$ans" in
        [Yy]*)
            # Pass key/value as real argv positions to the remote shell
            # (via sh -c '...' _ "$key" "$value") so nothing needs manual
            # quoting/escaping locally, regardless of special characters
            # in the secret value. Nothing touches disk or shell history
            # on either end.
            if ssh "$REMOTE" -- \
                sh -c "$REMOTE_PATH_PREFIX; exec '$REMOTE_ATC' secrets set \"\$1\" \"\$2\"" \
                _ "$key" "$value" \
                > /dev/null 2>&1; then
                echo "  ✅  Sent."
                SENT=$((SENT + 1))
            else
                echo "  ❌  Failed to set on remote - check manually."
            fi
            ;;
        [Qq]*)
            echo "Stopping early."
            unset value
            break
            ;;
        *)
            echo "  Skipped."
            SKIPPED=$((SKIPPED + 1))
            ;;
    esac
    unset value
    echo
done

echo "──────────────────────────────────────────"
echo "Done. Sent: $SENT   Skipped: $SKIPPED   Not found locally: $NOT_FOUND_LOCALLY"
echo
echo "Re-run this script anytime - it only asks about keys still missing."
