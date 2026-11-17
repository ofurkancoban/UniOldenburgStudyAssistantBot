#!/usr/bin/env bash
# A second copy of the bot on the same server to try a change before it goes to production.
#
#   scripts/staging.sh setup     once: a separate checkout, virtualenv and .env for the staging bot
#   scripts/staging.sh deploy    push HEAD to the `staging` branch, update the staging copy, restart it
#   scripts/staging.sh status    is it running, which commit, what did it log
#
# The staging bot is a different Telegram bot (create one with @BotFather: /newbot) with its own token,
# its own database, its own ports and no public tunnel. It shares nothing secret with production: the only
# thing copied from the production .env is the proxy address (UNI_PROXY_URL, a local address).
# `deploy` refuses to start it with the production bot's token (two bots on one token fight for the
# updates and the real students would lose messages).
#
# Settings (environment or the untracked .deploy.env, like scripts/deploy.sh):
#   DEPLOY_HOST          ssh target (required)
#   DEPLOY_DIR           production checkout          (default /root/UniAssistant)
#   STAGING_DIR          staging checkout              (default /root/UniAssistantStaging)
#   STAGING_PM2_APP      PM2 name of the staging bot   (default UniAssistantStaging)
#   NODE_BIN             directory with pm2 on the server
#   HEALTH_TIMEOUT       seconds to wait for the start (default 120)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .deploy.env ] && set -a && . ./.deploy.env && set +a

: "${DEPLOY_HOST:?set DEPLOY_HOST (e.g. in .deploy.env)}"
PROD_DIR="${DEPLOY_DIR:-/root/UniAssistant}"
STAGING_DIR="${STAGING_DIR:-/root/UniAssistantStaging}"
STAGING_APP="${STAGING_PM2_APP:-UniAssistantStaging}"
NODE_BIN="${NODE_BIN:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
SSH="${STAGING_SSH:-ssh}"   # the tests put a stand-in here

remote() {  # remote <script on stdin>
  "$SSH" "$DEPLOY_HOST" \
    PROD_DIR="$PROD_DIR" STAGING_DIR="$STAGING_DIR" STAGING_APP="$STAGING_APP" NODE_BIN="$NODE_BIN" \
    HEALTH_TIMEOUT="$HEALTH_TIMEOUT" STAGING_SKIP_PIP="${STAGING_SKIP_PIP:-0}" EXPECTED="${EXPECTED:-}" bash -s
}

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

cmd_setup() {
  remote <<'REMOTE'
set -euo pipefail
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
if [ ! -d "$STAGING_DIR/.git" ]; then
  branch="$(git -C "$PROD_DIR" rev-parse --abbrev-ref HEAD)"   # the branch production runs, whatever the remote's HEAD says
  git clone -q -b "$branch" "$(git -C "$PROD_DIR" remote get-url origin)" "$STAGING_DIR"
  echo "cloned the code into $STAGING_DIR"
fi
cd "$STAGING_DIR"
[ -d .venv ] || python3 -m venv .venv
[ "$STAGING_SKIP_PIP" = 1 ] || .venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  umask 077
  cp .env.example .env
  proxy="$(grep -E '^UNI_PROXY_URL=' "$PROD_DIR/.env" 2>/dev/null | tail -1 || true)"
  {
    echo ""
    echo "# --- staging: written by scripts/staging.sh, keeps this copy apart from production ---"
    echo "BOT_DB_FILE=bot_data.sqlite3"
    echo "PORT=3939"
    echo "LOGIN_PORT=8898"
    echo "CLOUDFLARE_TUNNEL="
    [ -z "$proxy" ] || echo "$proxy"
  } >> .env
  echo "created $STAGING_DIR/.env"
fi
echo
echo "Next: create the staging bot with @BotFather (/newbot), then put into $STAGING_DIR/.env:"
echo "  TELEGRAM_TOKEN=<the staging bot's own token>"
echo "  ALLOWED_USER_IDS=<your Telegram id>"
echo "Then run: scripts/staging.sh deploy"
REMOTE
}

cmd_deploy() {
  echo "==> push HEAD to the staging branch"
  git push -q origin HEAD:refs/heads/staging || {
    echo "The staging branch has commits HEAD does not contain. If you are sure, update it by hand:" >&2
    echo "  git push --force-with-lease origin HEAD:refs/heads/staging" >&2
    exit 1
  }
  EXPECTED="$(git rev-parse --short HEAD)"
  echo "==> update and restart the staging copy ($DEPLOY_HOST)"
  EXPECTED="$EXPECTED" remote <<'REMOTE'
set -euo pipefail
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
cd "$STAGING_DIR" 2>/dev/null || { echo "No staging copy yet: run scripts/staging.sh setup" >&2; exit 1; }
[ -f .env ] || { echo "No $STAGING_DIR/.env: run scripts/staging.sh setup" >&2; exit 1; }
token() { { grep -E '^TELEGRAM_TOKEN=' "$1" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d "\"' "; }
staging_token="$(token .env)"
prod_token="$(token "$PROD_DIR/.env")"
[ -n "$staging_token" ] || { echo "Put the staging bot's own TELEGRAM_TOKEN into $STAGING_DIR/.env first (@BotFather: /newbot)." >&2; exit 1; }
if [ -n "$prod_token" ] && [ "${staging_token%%:*}" = "${prod_token%%:*}" ]; then
  echo "The staging copy has the PRODUCTION bot's token. Two bots on one token fight for the updates and" >&2
  echo "the students would lose messages. Create a second bot with @BotFather and use its token." >&2
  exit 1
fi
git diff --quiet && git diff --cached --quiet || { echo "The staging copy has local changes; refusing" >&2; exit 1; }
git fetch -q origin staging
git checkout -q -f FETCH_HEAD
[ "$STAGING_SKIP_PIP" = 1 ] || .venv/bin/pip install -q -r requirements.txt
COMMIT="$(git rev-parse --short HEAD)"
[ -z "$EXPECTED" ] || [ "$COMMIT" = "$EXPECTED" ] || { echo "The server has $COMMIT, expected $EXPECTED" >&2; exit 1; }
LOG="$HOME/.pm2/logs/${STAGING_APP}-error.log"
offset=0; [ -f "$LOG" ] && offset="$(wc -c < "$LOG" | tr -d ' ')"
if pm2 describe "$STAGING_APP" >/dev/null 2>&1; then
  pm2 restart "$STAGING_APP" --update-env >/dev/null
else
  pm2 start .venv/bin/python --name "$STAGING_APP" --interpreter none -- studip_bot.py >/dev/null
fi
echo "waiting for the staging bot to start ($COMMIT) ..."
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
  if [ -f "$LOG" ] && tail -c +"$((offset + 1))" "$LOG" | grep -q "Unified watcher controller started successfully"; then
    echo "OK: the staging bot runs $COMMIT. Try it in Telegram, then release with scripts/deploy.sh."
    exit 0
  fi
  sleep 1
done
echo "The staging bot did not report a healthy start within ${HEALTH_TIMEOUT}s. Last warnings:" >&2
tail -c +"$((offset + 1))" "$LOG" 2>/dev/null | grep -E "ERROR|WARNING|Traceback" | tail -5 >&2 || true
exit 1
REMOTE
}

cmd_status() {
  remote <<'REMOTE'
set -euo pipefail
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
cd "$STAGING_DIR" 2>/dev/null || { echo "No staging copy yet: run scripts/staging.sh setup" >&2; exit 1; }
echo "commit: $(git log --oneline -1 | cut -c1-70)"
if pm2 describe "$STAGING_APP" >/dev/null 2>&1; then
  echo "pm2: $(pm2 jlist | python3 -c "import sys,json; print([p['pm2_env']['status'] for p in json.load(sys.stdin) if p['name']=='$STAGING_APP'])")"
else
  echo "pm2: not started"
fi
LOG="$HOME/.pm2/logs/${STAGING_APP}-error.log"
echo "last warnings:"; { [ -f "$LOG" ] && grep -E "WARNING|ERROR" "$LOG" | tail -5 | cut -c1-160; } || echo "  none"
REMOTE
}

case "${1:-}" in
  setup) cmd_setup ;;
  deploy) cmd_deploy ;;
  status) cmd_status ;;
  -h|--help|help|"") usage ;;
  *) usage >&2; exit 2 ;;
esac
