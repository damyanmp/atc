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
#   4. Sends the value to hermes-host over SSH's stdin stream (never
#      interpolated into a shell string, never passed as an argv entry,
#      never echoed, never written to disk in plaintext on either end)
#      into `atc secrets set <key>`, which prompts for the value and
#      reads it from stdin when no tty is attached.
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
# uv lives under ~/.hermes/bin (and possibly linuxbrew) on hermes-host,
# only on PATH in interactive shells; prepend it explicitly for
# non-interactive ssh commands.
REMOTE_PATH_PREFIX='PATH="$HOME/bin:$HOME/.local/bin:$HOME/.hermes/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"'
KEYCHAIN_SERVICE="atc"

# Single-quote a string for safe embedding in a command line that will
# be re-parsed by the REMOTE shell. `ssh host cmd arg1 arg2` has no way
# to send a true argv array - SSH's exec channel takes one command
# string, so the client concatenates all trailing arguments with spaces
# and the remote login shell re-parses the result. Any value with shell
# metacharacters must be single-quoted for that remote re-parse, not
# just escaped for the local shell.
rq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

echo "Checking hermes-host ($REMOTE) for missing secrets..."
echo

# Ask the remote atc which keys config.yaml references but aren't set yet.
# Built as a single string and passed as ssh's sole trailing argument so
# it survives intact - no `sh -c` wrapper, which would add a second
# concatenate-and-reparse hop and silently drop everything after PATH=.
REMOTE_CMD_SCAN="$REMOTE_PATH_PREFIX \"$REMOTE_ATC\" secrets scan"
MISSING_RAW="$(ssh -n "$REMOTE" -- "$REMOTE_CMD_SCAN" 2>&1 || true)"

KEYS=()
while IFS= read -r line; do
    [ -n "$line" ] && KEYS+=("$line")
done < <(printf '%s\n' "$MISSING_RAW" | sed -n 's/^! Not in keychain: \([^ ]*\).*/\1/p')

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
            # Send the value over ssh's stdin stream, never as a shell
            # argument - `atc secrets set <key>` (value omitted) prompts
            # via getpass, which falls back to reading one line from
            # stdin when there's no tty. This keeps the value out of any
            # shell parsing on either end, regardless of what characters
            # it contains. Nothing touches disk or shell history.
            REMOTE_CMD_SET="$REMOTE_PATH_PREFIX \"$REMOTE_ATC\" secrets set $(rq "$key")"
            if printf '%s\n' "$value" | ssh "$REMOTE" -- "$REMOTE_CMD_SET" \
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
