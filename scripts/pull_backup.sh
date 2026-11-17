#!/usr/bin/env bash
# Pull the bot's encryption key and its database backups from the server to this machine.
#
# Every student's private records are encrypted with a key derived from the server's .vault_key.
# The daily database backups on the server are useless without that key, and both sit on the same
# disk. This copies them somewhere else.
#
#   scripts/pull_backup.sh        run once now
#
# Settings (environment, or DEST/config.env, or the untracked .deploy.env next to this repo):
#   DEPLOY_HOST   ssh target, e.g. root@203.0.113.10   (required)
#   DEPLOY_DIR    checkout on the server                (default /root/UniAssistant)
#   BACKUP_DEST   where the copies go                   (default ~/Library/Application Support/UniAssistantBackup)
#   KEEP_DAYS     delete local database copies older than this (default 14; the server keeps 7)
#
# DEST is private (mode 0700): vault_key (0600), db/bot_data_<day>.sqlite3.gz, pull.log.
# When the key on the server changes, the old one is kept as vault_key.<day>.
# A failure shows a macOS notification and exits non-zero. Nothing secret is ever printed.
set -euo pipefail
umask 077

DEST="${BACKUP_DEST:-$HOME/Library/Application Support/UniAssistantBackup}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for config in "$DEST/config.env" "$HERE/../.deploy.env"; do
  [ -f "$config" ] && set -a && . "$config" && set +a && break
done
: "${DEPLOY_HOST:?set DEPLOY_HOST (in $DEST/config.env or the environment)}"
DEPLOY_DIR="${DEPLOY_DIR:-/root/UniAssistant}"
KEEP_DAYS="${KEEP_DAYS:-14}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

mkdir -p "$DEST/db"
chmod 700 "$DEST" "$DEST/db"
LOG="$DEST/pull.log"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
fail() {
  log "FAILED: $*"
  osascript -e "display notification \"$*\" with title \"UniAssistant backup failed\"" >/dev/null 2>&1 || true
  echo "backup failed: $*" >&2
  exit 1
}

# 1. the key: copied to a temporary file, checked, then moved into place
tmp="$(mktemp "$DEST/.key.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
scp -q "${SSH_OPTS[@]}" "$DEPLOY_HOST:$DEPLOY_DIR/.vault_key" "$tmp" || fail "could not copy the key"
[ -s "$tmp" ] || fail "the copied key is empty"
remote_sum="$(ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "sha256sum '$DEPLOY_DIR/.vault_key' | cut -d' ' -f1")" || fail "could not check the key on the server"
local_sum="$(shasum -a 256 "$tmp" | cut -d' ' -f1)"
[ "$remote_sum" = "$local_sum" ] || fail "the copied key does not match the server's"
key_note="key unchanged"
if [ -f "$DEST/vault_key" ] && ! cmp -s "$tmp" "$DEST/vault_key"; then
  cp -p "$DEST/vault_key" "$DEST/vault_key.$(date +%F)"
  key_note="key CHANGED on the server (old one kept)"
fi
chmod 600 "$tmp"
mv "$tmp" "$DEST/vault_key"
trap - EXIT

# 2. the database backups the server has and this machine does not
remote_files="$(ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "ls -1 '$DEPLOY_DIR/backups' | grep -E '^bot_data_.*\.sqlite3\.gz$'")" || fail "could not list the server's backups"
fetched=0
while IFS= read -r name; do
  [ -n "$name" ] || continue
  if [ ! -f "$DEST/db/$name" ]; then
    scp -q -p "${SSH_OPTS[@]}" "$DEPLOY_HOST:$DEPLOY_DIR/backups/$name" "$DEST/db/$name.part" || fail "could not copy $name"
    gzip -t "$DEST/db/$name.part" || { rm -f "$DEST/db/$name.part"; fail "$name is not a valid archive"; }
    chmod 600 "$DEST/db/$name.part"
    mv "$DEST/db/$name.part" "$DEST/db/$name"
    fetched=$((fetched + 1))
  fi
done <<< "$remote_files"

newest="$(ls -1 "$DEST/db"/bot_data_*.sqlite3.gz 2>/dev/null | tail -1 || true)"
[ -n "$newest" ] || fail "no database backup on this machine"
gzip -t "$newest" || fail "the newest local backup is damaged"
find "$DEST/db" -name 'bot_data_*.sqlite3.gz' -mtime +"$KEEP_DAYS" -delete
count="$(ls -1 "$DEST/db"/bot_data_*.sqlite3.gz | wc -l | tr -d ' ')"
log "ok: $key_note; $fetched new backup(s), $count kept, newest $(basename "$newest")"
echo "ok: $key_note; $fetched new backup(s), $count kept, newest $(basename "$newest")"
