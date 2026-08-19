#!/usr/bin/env bash
# ============================================================================
# fyn — deploy origin/main to the shared Hetzner box
# ============================================================================
#
#   ./infra/deploy/deploy.sh              # everything (api + postgres)
#   ./infra/deploy/deploy.sh api          # backend only (migrations run here)
#   ./infra/deploy/deploy.sh --no-cache   # force a clean image build
#   ./infra/deploy/deploy.sh --server     # skip the local git checks
#
# The server only ever runs code from origin/main. Nothing is copied from this
# machine — the box pulls, so what is deployed is always what is on GitHub.
#
# Migrations: the backend image's CMD runs `alembic upgrade head` before
# uvicorn, so they run inside the new container on start. A failed migration
# leaves the container restarting and the health wait below fails the deploy.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

TARGET="all"
MODE="full"
BUILD_ARGS=""
for arg in "$@"; do
  case "$arg" in
    all|api)     TARGET="$arg" ;;
    --server)    MODE="server" ;;
    --no-cache)  BUILD_ARGS="--no-cache" ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *)           fyn_die "unknown argument: $arg" ;;
  esac
done

case "$TARGET" in
  all) SERVICES="" ;;             # empty = every service in the file
  api) SERVICES="backend" ;;
esac

started=$(date +%s)
echo "deploying ${TARGET} to ${FYN_SSH} (${FYN_DOMAIN})"
echo ""

# ── 1. Local guards ─────────────────────────────────────────────────────────
if [ "$MODE" != "server" ]; then
  echo "==> local checks"
  branch=$(git rev-parse --abbrev-ref HEAD)
  [ "$branch" = "${FYN_BRANCH}" ] \
    || fyn_die "on branch '${branch}', not '${FYN_BRANCH}'. The server only deploys ${FYN_BRANCH}."

  git diff --quiet && git diff --cached --quiet \
    || fyn_die "uncommitted changes. Commit or stash them — the server deploys origin/${FYN_BRANCH}, so anything uncommitted would silently not ship."

  git fetch --quiet origin "${FYN_BRANCH}"
  local_sha=$(git rev-parse HEAD)
  remote_sha=$(git rev-parse "origin/${FYN_BRANCH}")
  [ "$local_sha" = "$remote_sha" ] \
    || fyn_die "local ${FYN_BRANCH} ($(git rev-parse --short HEAD)) differs from origin/${FYN_BRANCH} ($(git rev-parse --short origin/${FYN_BRANCH})). Push first."
  echo "    ${FYN_BRANCH} @ $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"
fi

# ── 2. Reachability ─────────────────────────────────────────────────────────
fyn_ssh true 2>/dev/null || fyn_die "cannot SSH to ${FYN_SSH}. Run ./infra/deploy/setup-server.sh for the fix."
fyn_ssh "[ -f ${FYN_REMOTE_DIR}/${FYN_ENV_FILE} ]" \
  || fyn_die "${FYN_REMOTE_DIR}/${FYN_ENV_FILE} is missing on the server. Run ./infra/deploy/setup-server.sh first."

# ── 3. Sync the server checkout ─────────────────────────────────────────────
echo "==> syncing ${FYN_REMOTE_DIR} to origin/${FYN_BRANCH}"
fyn_ssh "cd ${FYN_REMOTE_DIR} && git fetch --prune origin ${FYN_BRANCH} && git reset --hard origin/${FYN_BRANCH}" >/dev/null \
  || fyn_die "server-side git sync failed. If it is an auth error the deploy key has expired — see setup-server.sh."

server_sha=$(fyn_ssh "cd ${FYN_REMOTE_DIR} && git rev-parse HEAD")
server_subject=$(fyn_ssh "cd ${FYN_REMOTE_DIR} && git log -1 --pretty=%s")
echo "    ${server_sha:0:7} — ${server_subject}"

if [ "$MODE" != "server" ] && [ "$server_sha" != "$local_sha" ]; then
  fyn_die "server landed on ${server_sha:0:7} but local ${FYN_BRANCH} is ${local_sha:0:7}. Something else pushed mid-deploy; re-run."
fi

# ── 4. Build and start ──────────────────────────────────────────────────────
echo "==> building images (${TARGET})"
fyn_compose "build ${BUILD_ARGS} ${SERVICES}" || fyn_die "image build failed on the server. Output above is the diagnostic; fix, push, re-run.
  If it was killed rather than failing with an error, the box ran out of memory
  (2 vCPU / 3.7 GiB, shared with jitraa): check 'ssh ${FYN_SSH} free -m'."

echo "==> starting containers"
# --remove-orphans keeps a renamed service from lingering on the shared box.
fyn_compose "up -d --remove-orphans ${SERVICES}" || fyn_die "compose up failed."

# ── 4b. Route ───────────────────────────────────────────────────────────────
# Kept in step with every deploy so the file on the shared proxy always matches
# this repo. Validates before reloading; a bad route never reaches the box.
echo "==> installing the route on the shared proxy"
bash infra/deploy/install-route.sh

# ── 5. Wait for health ──────────────────────────────────────────────────────
echo "==> waiting for the backend to become healthy (migrations run here)"
deadline=$(( $(date +%s) + 300 ))
status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  status=$(fyn_ssh "docker inspect --format '{{.State.Health.Status}}' ${FYN_PROJECT}-backend-1 2>/dev/null" || echo "missing")
  case "$status" in
    healthy) echo "    healthy"; break ;;
    unhealthy)
      echo ""
      fyn_compose "logs --tail 60 backend" || true
      fyn_die "backend reported unhealthy — the log above shows why. Two common causes: a failed alembic migration, or the production auth gate refusing to boot (config.py require_production_auth_config) when ENVIRONMENT=production and a provider credential is missing. Postgres and its data are untouched either way."
      ;;
  esac
  sleep 5
done
[ "$status" = "healthy" ] || {
  fyn_compose "logs --tail 60 backend" || true
  fyn_die "backend did not become healthy within 300s (last status: ${status})."
}

# ── 6. Verify from outside ──────────────────────────────────────────────────
echo "==> health check"
public_ok=false
if body=$(curl -fsS --max-time 20 "${FYN_HEALTH_URL}" 2>/dev/null); then
  public_ok=true
  echo "    ${FYN_HEALTH_URL} -> $(printf '%s' "$body" | head -c 200)"
else
  echo "    ${FYN_HEALTH_URL} unreachable — falling back to the internal check"
  # Nothing is published to a host port, so the only way to reach the app is
  # from inside the shared network. This separates "the app is broken" from
  # "DNS is not pointed here yet".
  if body=$(fyn_internal_curl /api/health 2>/dev/null); then
    echo "    ${FYN_EDGE_ALIAS}:${FYN_EDGE_PORT}/api/health -> $(printf '%s' "$body" | head -c 200)"
    echo "    the app is up; the public hostname is what is not wired."
    echo "    Point ${FYN_DOMAIN} at ${FYN_HOST} (DNS-only, grey cloud) and it will serve."
  else
    fyn_die "the app is not answering inside the network either. Logs: ./infra/deploy/logs.sh"
  fi
fi

# ── 7. Summary ──────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "  DEPLOY COMPLETE — ${TARGET}"
echo "────────────────────────────────────────────"
echo "  commit    ${server_sha:0:7} — ${server_subject}"
echo "  host      ${FYN_SSH}:${FYN_REMOTE_DIR}"
echo "  api       https://${FYN_DOMAIN}$([ "$public_ok" = true ] || echo '   (not reachable yet)')"
echo "  spa       ${FYN_APP_ORIGIN}   (hosted off this box)"
echo "  duration  $(( $(date +%s) - started ))s"
echo "────────────────────────────────────────────"
