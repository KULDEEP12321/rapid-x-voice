#!/usr/bin/env bash
# Pull the latest pushed code and restart the always-on voice services.

set -Eeuo pipefail

APP_DIR="${DEPLOY_REPO_DIR:-${DEPLOY_APP_DIR:-/opt/livekit-ai-voice}}"
BRANCH="${DEPLOY_BRANCH:-main}"
REMOTE="${DEPLOY_REMOTE:-origin}"
WORKER_SERVICE="${DEPLOY_WORKER_SERVICE:-livekit-agent.service}"
DASHBOARD_SERVICE="${DEPLOY_DASHBOARD_SERVICE:-livekit-dashboard.service}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-/tmp/livekit-ai-voice-deploy.lock}"
EXPECTED_LIVEKIT_REGION="${EXPECTED_LIVEKIT_REGION:-India West}"
export EXPECTED_LIVEKIT_REGION

timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
    printf '[%s] %s\n' "$(timestamp)" "$*"
}

cd "$APP_DIR"
mkdir -p logs
mkdir -p "${VOICE_RECORDINGS_DIR:-${APP_DIR}/recordings}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another deployment is already running"
    exit 75
fi

log "deploy start: ${REMOTE}/${BRANCH} commit=${GITHUB_DEPLOY_COMMIT:-unknown}"

git fetch "$REMOTE" "$BRANCH"
git reset --hard "$REMOTE/$BRANCH"

if [ ! -x venv/bin/python ]; then
    log "creating Python virtualenv"
    python3 -m venv venv
fi

venv/bin/python -m pip install -r requirements.txt
log "downloading agent model/plugin files"
venv/bin/python agent.py download-files

systemctl restart "$WORKER_SERVICE"
systemctl restart "$DASHBOARD_SERVICE"

systemctl is-active --quiet "$WORKER_SERVICE"
systemctl is-active --quiet "$DASHBOARD_SERVICE"

sleep 5
./status.sh

log "deploy complete"
