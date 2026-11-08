#!/usr/bin/env bash
# Deploy the bot to the server with a health check and automatic rollback.
#
#   scripts/deploy.sh            run checks, push, update the server, verify
#   scripts/deploy.sh --worker   also redeploy the Cloudflare Worker first
#
# Settings come from the environment or an untracked .deploy.env file:
#   DEPLOY_HOST   ssh target, e.g. root@203.0.113.10   (required)
#   DEPLOY_DIR    checkout on the server                (default /root/UniAssistant)
#   PM2_APP       PM2 process name                      (default UniAssistant)
#   NODE_BIN      directory holding pm2/node on the server, prepended to PATH
#   HEALTH_TIMEOUT / ROLLBACK_TIMEOUT  seconds to wait for a healthy start (90 / 120)
#
# The server keeps the previous commit; if the bot is not up and running
# within HEALTH_TIMEOUT seconds after the restart, it is reset to that commit
# and restarted again, and the script exits non-zero.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .deploy.env ] && set -a && . ./.deploy.env && set +a

: "${DEPLOY_HOST:?set DEPLOY_HOST (e.g. in .deploy.env)}"
DEPLOY_DIR="${DEPLOY_DIR:-/root/UniAssistant}"
PM2_APP="${PM2_APP:-UniAssistant}"
NODE_BIN="${NODE_BIN:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
ROLLBACK_TIMEOUT="${ROLLBACK_TIMEOUT:-120}"
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

if [ "${1:-}" = "--worker" ]; then
  step "Cloudflare Worker"
  cmp -s cloudflare_worker/worker.js worker.js || { echo "worker.js copies differ" >&2; exit 1; }
  (cd cloudflare_worker && npx --no-install wrangler deploy)
fi

step "Push"
git push -q origin HEAD:main
COMMIT="$(git rev-parse --short HEAD)"
echo "pushed $COMMIT"

step "Server update ($DEPLOY_HOST)"
ssh "$DEPLOY_HOST" \
  DEPLOY_DIR="$DEPLOY_DIR" PM2_APP="$PM2_APP" NODE_BIN="$NODE_BIN" \
  HEALTH_TIMEOUT="$HEALTH_TIMEOUT" ROLLBACK_TIMEOUT="$ROLLBACK_TIMEOUT" EXPECTED="$COMMIT" bash -s <<'REMOTE'
set -euo pipefail
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
cd "$DEPLOY_DIR"
PREVIOUS="$(git rev-parse --short HEAD)"
# the bot logs to stderr, PM2 keeps that in the error log: read both
LOGS="$(pm2 jlist | python3 -c 'import json,os,sys; e=next(p["pm2_env"] for p in json.load(sys.stdin) if p["name"] == os.environ["PM2_APP"]); print(e["pm_out_log_path"], e["pm_err_log_path"])')"

healthy() {
  # the bot is up once the watcher reports a successful start after `since`
  local since="$1" deadline=$((SECONDS + $2))
  while [ $SECONDS -lt $deadline ]; do
    sleep 5
    pm2 jlist | python3 -c 'import json,os,sys; sys.exit(0 if any(p["name"] == os.environ["PM2_APP"] and p["pm2_env"]["status"] == "online" for p in json.load(sys.stdin)) else 1)' || continue
    # shellcheck disable=SC2086
    if for f in $LOGS; do tail -c 200000 "$f"; done | awk -v s="$since" '$1" "$2 >= s' | grep -q "Unified watcher controller started successfully"; then
      return 0
    fi
  done
  return 1
}

restart() {
  local since
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  timeout 60 pm2 restart "$PM2_APP" >/dev/null
  healthy "$since" "$1"
}

git pull -q --ff-only
NOW="$(git rev-parse --short HEAD)"
[ "$NOW" = "$EXPECTED" ] || { echo "server is at $NOW, expected $EXPECTED" >&2; exit 1; }
echo "updated $PREVIOUS -> $NOW"

if restart "$HEALTH_TIMEOUT"; then
  echo "healthy on $NOW"
  exit 0
fi

echo "not healthy after ${HEALTH_TIMEOUT}s; rolling back to $PREVIOUS" >&2
git reset -q --hard "$PREVIOUS"
if restart "$ROLLBACK_TIMEOUT"; then
  echo "rolled back to $PREVIOUS and healthy" >&2
else
  echo "rollback to $PREVIOUS is not healthy either; check pm2 logs" >&2
fi
exit 1
REMOTE
step "Done: $COMMIT is live"
