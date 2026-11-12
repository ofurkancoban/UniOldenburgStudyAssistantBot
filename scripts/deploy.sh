#!/usr/bin/env bash
# Deploy the bot to the server with a health check and automatic rollback.
#
#   scripts/deploy.sh            run checks, push, update the server, verify
#   scripts/deploy.sh --worker   deploy Worker and VPS, restoring both on failure
#
# Settings come from the environment or an untracked .deploy.env file:
#   DEPLOY_HOST   ssh target, e.g. root@203.0.113.10   (required)
#   DEPLOY_DIR    checkout on the server                (default /root/UniAssistant)
#   PM2_APP       PM2 process name                      (default UniAssistant)
#   NODE_BIN      directory holding pm2/node on the server, prepended to PATH
#   HEALTH_TIMEOUT / ROLLBACK_TIMEOUT  seconds to wait for a healthy cycle (900 / 900)
#
# The server keeps the previous commit; if the bot is not up and running
# within HEALTH_TIMEOUT seconds after the restart, it is reset to that commit
# and restarted again, and the script exits non-zero.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh [--worker]

Deploy the committed current branch to the configured VPS.
  --worker  Deploy the Worker and VPS with coordinated rollback.
USAGE
}

DEPLOY_WORKER=0
case "${1:-}" in
  "") ;;
  --worker) DEPLOY_WORKER=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .deploy.env ] && set -a && . ./.deploy.env && set +a

: "${DEPLOY_HOST:?set DEPLOY_HOST (e.g. in .deploy.env)}"
DEPLOY_DIR="${DEPLOY_DIR:-/root/UniAssistant}"
PM2_APP="${PM2_APP:-UniAssistant}"
NODE_BIN="${NODE_BIN:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-900}"
ROLLBACK_TIMEOUT="${ROLLBACK_TIMEOUT:-900}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Local checks"
if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is not clean; commit or stash first." >&2
  exit 1
fi
"$ROOT/.venv/bin/ruff" check .
"$ROOT/.venv/bin/mypy"
TZ=UTC "$PY" -m unittest discover -s tests >/dev/null 2>&1 || {
  echo "Tests failed; run: TZ=UTC $PY -m unittest discover -s tests" >&2
  exit 1
}
echo "ruff, mypy and tests passed"

step "Push"
git push -q origin HEAD:main
COMMIT="$(git rev-parse --short HEAD)"
echo "pushed $COMMIT"

WORKER_CHANGED=0
WORKER_SNAPSHOT=""
WORKER_PREVIOUS_ARGS=()
cleanup_release() {
  local result=$?
  trap - EXIT INT TERM HUP
  if [ "$result" -ne 0 ] && [ "$WORKER_CHANGED" = 1 ]; then
    echo "Release failed; restoring the previous Worker deployment" >&2
    if ! (cd cloudflare_worker && npx --no-install wrangler versions deploy \
        "${WORKER_PREVIOUS_ARGS[@]}" --yes --message "Restore after failed VPS release"); then
      echo "CRITICAL: Worker rollback failed; manual recovery is required" >&2
    fi
  fi
  [ -z "$WORKER_SNAPSHOT" ] || rm -f "$WORKER_SNAPSHOT"
  exit "$result"
}
trap cleanup_release EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ "$DEPLOY_WORKER" = 1 ]; then
  step "Cloudflare Worker"
  WORKER_SNAPSHOT="$(mktemp)"
  (cd cloudflare_worker && npx --no-install wrangler deployments status --json) > "$WORKER_SNAPSHOT"
  "$PY" - "$WORKER_SNAPSHOT" <<'PYTHON'
import json, sys, uuid
versions = json.load(open(sys.argv[1]))["versions"]
assert versions, "Worker has no active versions to restore"
assert abs(sum(float(v["percentage"]) for v in versions) - 100) < 0.001
for v in versions:
    uuid.UUID(v["version_id"])
    assert 0 <= float(v["percentage"]) <= 100
PYTHON
  while IFS= read -r version; do WORKER_PREVIOUS_ARGS+=("$version"); done < <(
    "$PY" -c 'import json,sys; [print(str(v["version_id"])+"@"+str(v["percentage"])) for v in json.load(open(sys.argv[1]))["versions"]]' "$WORKER_SNAPSHOT"
  )
  # A failed CLI request can still have reached Cloudflare; restore on any failure.
  WORKER_CHANGED=1
  (cd cloudflare_worker && npx --no-install wrangler deploy)
fi

step "Server update ($DEPLOY_HOST)"
ssh "$DEPLOY_HOST" \
  DEPLOY_DIR="$DEPLOY_DIR" PM2_APP="$PM2_APP" NODE_BIN="$NODE_BIN" \
  HEALTH_TIMEOUT="$HEALTH_TIMEOUT" ROLLBACK_TIMEOUT="$ROLLBACK_TIMEOUT" EXPECTED="$COMMIT" bash -s <<'REMOTE'
set -euo pipefail
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
cd "$DEPLOY_DIR"
git diff --quiet && git diff --cached --quiet || { echo "Server has tracked changes; refusing deployment" >&2; exit 1; }
PREVIOUS="$(git rev-parse --short HEAD)"
# Read both PM2 logs, starting at byte offsets captured before each restart.
LOG_OFFSETS="$(mktemp)"
trap 'rm -f "$LOG_OFFSETS"' EXIT

new_logs() {
  python3 - "$LOG_OFFSETS" <<'PYTHON'
import json, os, sys
for path, previous in json.load(open(sys.argv[1])).items():
    try:
        with open(path, "rb") as log:
            st = os.fstat(log.fileno())
            offset = previous[1] if st.st_ino == previous[0] and st.st_size >= previous[1] else 0
            log.seek(max(offset, st.st_size - 200000))
            print(log.read().decode("utf-8", errors="replace"))
    except FileNotFoundError:
        pass
PYTHON
}

healthy() {
  # Require polling and a completed healthy scan from the currently running PID.
  local deadline=$((SECONDS + $1)) mode="${2:-current}"
  local pid
  while [ $SECONDS -lt $deadline ]; do
    sleep 5
    pm2 jlist | python3 -c 'import json,os,sys; sys.exit(0 if any(p["name"] == os.environ["PM2_APP"] and p["pm2_env"]["status"] == "online" for p in json.load(sys.stdin)) else 1)' || continue
    pid="$(pm2 jlist | python3 -c 'import json,os,sys; print(next(p["pid"] for p in json.load(sys.stdin) if p["name"] == os.environ["PM2_APP"]))')"
    # Read to the end: grep -q could SIGPIPE the reader under pipefail.
    if new_logs \
        | grep -F "Telegram polling started successfully" >/dev/null \
        && { if [ "$mode" = previous ]; then
               new_logs | grep -E 'Watcher cycle #[0-9]+ completed' >/dev/null
             else
               new_logs | grep -F "Watcher cycle healthy pid=$pid" >/dev/null \
                 && new_logs | grep -F "Deadline reminder cycle healthy pid=$pid" >/dev/null
             fi; }; then
        return 0
    fi
  done
  return 1
}

restart() {
  pm2 jlist | python3 -c 'import json,os,sys; e=next(p["pm2_env"] for p in json.load(sys.stdin) if p["name"] == os.environ["PM2_APP"]); paths=[e["pm_out_log_path"], e["pm_err_log_path"]]; print(json.dumps({p:[os.stat(p).st_ino,os.stat(p).st_size] if os.path.exists(p) else [0,0] for p in paths}))' > "$LOG_OFFSETS"
  # A missing/stopped PM2 entry or a PM2 error must enter the same rollback
  # path as a process that restarted but never became healthy.
  timeout 60 pm2 restart "$PM2_APP" >/dev/null || return 1
  healthy "$1" "${2:-current}"
}

DEPS_CHANGED=0
NODE_DEPS_CHANGED=0
FREEZE=".deploy_freeze_before.txt"
RELEASE_UPDATED=0
remote_cleanup() {
  local result=$?
  trap - EXIT INT TERM HUP
  # Catch unexpected failures and interrupted releases as well as the explicit
  # package/health failure paths below. Never leave a new checkout on failure.
  if [ "$result" -ne 0 ] && [ "$RELEASE_UPDATED" = 1 ] \
      && [ "$(git rev-parse --short HEAD)" != "$PREVIOUS" ]; then
    echo "Interrupted release; restoring $PREVIOUS" >&2
    git reset -q --hard "$PREVIOUS" || echo "CRITICAL: code rollback failed" >&2
    if [ "$DEPS_CHANGED" = 1 ] && [ -s "$FREEZE" ]; then
      .venv/bin/pip install -q -r "$FREEZE" || echo "CRITICAL: package rollback failed" >&2
    fi
    if [ "$NODE_DEPS_CHANGED" = 1 ]; then
      (cd whatsapp_service && npm ci --omit=dev --no-audit --no-fund) || echo "CRITICAL: Node rollback failed" >&2
    fi
    restart "$ROLLBACK_TIMEOUT" previous || echo "CRITICAL: restored process did not become healthy" >&2
  fi
  rm -f "$FREEZE" "$LOG_OFFSETS"
  exit "$result"
}
trap remote_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
RELEASE_UPDATED=1
git pull -q --ff-only
NOW="$(git rev-parse --short HEAD)"
[ "$NOW" = "$EXPECTED" ] || { echo "server is at $NOW, expected $EXPECTED" >&2; git reset -q --hard "$PREVIOUS"; exit 1; }
echo "updated $PREVIOUS -> $NOW"

# Python packages follow requirements.txt when it changed; the versions from
# before are kept so a rollback can put them back.
if ! git diff --quiet "$PREVIOUS" "$NOW" -- requirements.txt; then
  DEPS_CHANGED=1
  .venv/bin/pip freeze > "$FREEZE"
  echo "requirements.txt changed: installing packages"
  if ! { .venv/bin/pip install -q -U "setuptools>=84.1" "wheel>=0.46.2" \
         && .venv/bin/pip install -q -r requirements.txt; }; then
    echo "package install failed; back to $PREVIOUS" >&2
    .venv/bin/pip install -q -r "$FREEZE" || echo "restoring the old packages failed too" >&2
    git reset -q --hard "$PREVIOUS"
    exit 1
  fi
fi

if ! git diff --quiet "$PREVIOUS" "$NOW" -- whatsapp_service/package.json whatsapp_service/package-lock.json; then
  NODE_DEPS_CHANGED=1
  echo "WhatsApp package manifest changed: installing Node packages"
  if ! (cd whatsapp_service && npm ci --omit=dev --no-audit --no-fund); then
    echo "Node package install failed; restoring $PREVIOUS" >&2
    git reset -q --hard "$PREVIOUS"
    if [ "$DEPS_CHANGED" = 1 ]; then
      .venv/bin/pip install -q -r "$FREEZE" || echo "restoring the old Python packages failed" >&2
    fi
    if ! (cd whatsapp_service && npm ci --omit=dev --no-audit --no-fund); then
      echo "restoring the previous Node packages failed too" >&2
    fi
    exit 1
  fi
fi

if restart "$HEALTH_TIMEOUT"; then
  echo "healthy on $NOW"
  exit 0
fi

echo "not healthy after ${HEALTH_TIMEOUT}s; rolling back to $PREVIOUS" >&2
git reset -q --hard "$PREVIOUS"
if [ "$DEPS_CHANGED" = 1 ]; then
  .venv/bin/pip install -q -r "$FREEZE" || echo "restoring the old packages failed" >&2
fi
if [ "$NODE_DEPS_CHANGED" = 1 ]; then
  (cd whatsapp_service && npm ci --omit=dev --no-audit --no-fund) || echo "restoring the old Node packages failed" >&2
fi
if restart "$ROLLBACK_TIMEOUT" previous; then
  echo "rolled back to $PREVIOUS and healthy" >&2
else
  echo "rollback to $PREVIOUS is not healthy either; check pm2 logs" >&2
fi
exit 1
REMOTE
step "Done: $COMMIT is live"
