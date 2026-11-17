#!/usr/bin/env bash
# Copy the bot's newest database backup and its encryption key to Google Drive, encrypted.
#
# Runs on the server (cron, a little after the nightly database backup). Everything is encrypted
# with `age` for the PUBLIC key in .offsite_age_recipient before it leaves the machine: Google, and
# anyone who gets into the Drive account or the server, cannot read it. Only the private key,
# which is not on the server, opens it.
#
#   scripts/offsite_backup.sh
#
# Settings (environment, all optional):
#   BOT_DIR            checkout of the bot                  (default: the folder above this script)
#   OFFSITE_REMOTE     rclone remote and folder             (default gdrive:UniAssistantBackup)
#   OFFSITE_KEEP_DAYS  delete uploads older than this       (default 7, like the server's own backups)
#
# Needs: rclone with a remote named like OFFSITE_REMOTE (scope drive.file is enough) and age.
# Result: backups/offsite_status.json ({"ok", "at", ...}) which the bot's health check reads,
# and backups/offsite.log. Nothing secret is ever printed.
set -euo pipefail
umask 077

BOT_DIR="${BOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${OFFSITE_REMOTE:-gdrive:UniAssistantBackup}"
KEEP_DAYS="${OFFSITE_KEEP_DAYS:-7}"
BACKUPS="$BOT_DIR/backups"
STATUS="$BACKUPS/offsite_status.json"
LOG="$BACKUPS/offsite.log"
RECIPIENT_FILE="$BOT_DIR/.offsite_age_recipient"
KEY_FILE="$BOT_DIR/.vault_key"
export HOME="${HOME:-/root}"

mkdir -p "$BACKUPS"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
write_status() {  # ok(true|false) detail files newest
  printf '{"ok": %s, "at": %s, "detail": "%s", "files": %s, "newest": "%s"}\n' \
    "$1" "$(date +%s)" "$2" "${3:-0}" "${4:-}" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}
fail() {
  log "FAILED: $*"
  write_status false "$(printf '%s' "$*" | tr -d '"\\' | cut -c1-120)" 0 ""
  echo "offsite backup failed: $*" >&2
  exit 1
}

command -v rclone >/dev/null || fail "rclone is not installed"
command -v age >/dev/null || fail "age is not installed"
[ -s "$RECIPIENT_FILE" ] || fail "no public key in $RECIPIENT_FILE"
[ -s "$KEY_FILE" ] || fail "no .vault_key to back up"
newest="$(ls -1 "$BACKUPS"/bot_data_*.sqlite3.gz 2>/dev/null | tail -1 || true)"
[ -n "$newest" ] || fail "no database backup to upload"
gzip -t "$newest" || fail "the newest database backup is damaged"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
name="$(basename "$newest")"
day="$(date +%F)"

# the database backup and the key, both encrypted for the public key
age -R "$RECIPIENT_FILE" -o "$work/$name.age" "$newest" || fail "could not encrypt the database backup"
age -R "$RECIPIENT_FILE" -o "$work/vault_key_$day.age" "$KEY_FILE" || fail "could not encrypt the key"

rclone mkdir "$REMOTE" || fail "could not reach the Drive folder"
rclone copyto "$work/$name.age" "$REMOTE/$name.age" || fail "could not upload the database backup"
rclone copyto "$work/vault_key_$day.age" "$REMOTE/vault_key_$day.age" || fail "could not upload the key"

# what is on Drive must be as big as what was sent
for f in "$name.age" "vault_key_$day.age"; do
  remote_size="$(rclone size --json "$REMOTE/$f" 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["bytes"])' || echo -1)"
  [ "$remote_size" = "$(wc -c < "$work/$f" | tr -d ' ')" ] || fail "the uploaded $f does not have the size that was sent"
done

# keep a week, like the server
rclone delete "$REMOTE" --min-age "${KEEP_DAYS}d" || log "could not prune old uploads"
count="$(rclone lsf "$REMOTE" 2>/dev/null | grep -c '\.age$' || true)"
log "ok: uploaded $name.age and vault_key_$day.age, $count file(s) on Drive"
write_status true "uploaded $name.age" "$count" "$name.age"
echo "ok: uploaded $name.age and vault_key_$day.age, $count file(s) on Drive"
