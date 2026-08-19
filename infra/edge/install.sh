#!/usr/bin/env bash
# ============================================================================
# edge — install the shared ingress proxy, and cut over to it
# ============================================================================
#
#   ./infra/edge/install.sh           prepare only (no downtime, idempotent)
#   ./infra/edge/install.sh --swap    prepare, then cut over from jitraa's Caddy
#
# Prepare creates the shared network, copies the certificate store out of
# jitraa's volume, installs /opt/edge, and validates the combined config —
# all while the existing proxy keeps serving.
#
# --swap is the only step with downtime. It builds jitraa's new web image
# first, so the actual gap is one container recreate plus one container start,
# a few seconds. Rollback is printed if anything fails.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

SWAP=false
[ "${1:-}" = "--swap" ] && SWAP=true

# Bootstrap convenience: edge must already know jitraa's routes before it takes
# over :443, or jitraa.com would 404 the moment it starts. After this one time,
# jitraa ships its own file from its own deploy.
JITRAA_REPO="${JITRAA_REPO:-/Users/hari/2026/jitraa}"

echo "edge install on ${FYN_SSH}"
echo ""

# ── 1. Preflight ────────────────────────────────────────────────────────────
echo "==> preflight"
fyn_ssh true 2>/dev/null || fyn_die "cannot SSH to ${FYN_SSH}."
fyn_ssh 'docker compose version >/dev/null 2>&1' || fyn_die "docker compose plugin missing on the server."
ACME_EMAIL=$(fyn_ssh "grep -E '^ACME_EMAIL=' /opt/jitraa/deploy/caddy.env | cut -d= -f2-" || true)
[ -n "$ACME_EMAIL" ] || fyn_die "could not read ACME_EMAIL from /opt/jitraa/deploy/caddy.env. Set it in /opt/edge/.env by hand."
echo "    acme email: ${ACME_EMAIL}"

# ── 2. Shared network ───────────────────────────────────────────────────────
echo "==> shared network '${FYN_EDGE_NETWORK}'"
if fyn_ssh "docker network inspect ${FYN_EDGE_NETWORK} >/dev/null 2>&1"; then
  echo "    exists"
else
  fyn_ssh "docker network create ${FYN_EDGE_NETWORK}" >/dev/null
  echo "    created"
fi

# ── 3. Certificate store ────────────────────────────────────────────────────
# Copied, never moved: jitraa's volume stays intact as the rollback path. Only
# copies into an empty destination, so re-running cannot clobber renewals edge
# has since performed.
echo "==> certificate store"
fyn_ssh "docker volume create edge_caddy-data >/dev/null"
if fyn_ssh "docker run --rm -v edge_caddy-data:/to alpine sh -c '[ -d /to/caddy/certificates ]'" 2>/dev/null; then
  echo "    edge_caddy-data already holds certificates — left alone"
else
  fyn_ssh "docker run --rm -v jitraa_caddy-data:/from -v edge_caddy-data:/to alpine sh -c 'cp -a /from/. /to/ 2>/dev/null || true'"
  count=$(fyn_ssh "docker run --rm -v edge_caddy-data:/to alpine sh -c 'ls /to/caddy/certificates/*/* 2>/dev/null | wc -l'" || echo 0)
  echo "    copied from jitraa_caddy-data (${count} entries)"
fi

# ── 4. Install /opt/edge ────────────────────────────────────────────────────
echo "==> installing ${FYN_EDGE_DIR}"
fyn_ssh "mkdir -p ${FYN_EDGE_DIR}/sites"
fyn_ssh "cat > ${FYN_EDGE_DIR}/docker-compose.yml" < infra/edge/docker-compose.yml
fyn_ssh "cat > ${FYN_EDGE_DIR}/Caddyfile" < infra/edge/Caddyfile
fyn_ssh "printf 'ACME_EMAIL=%s\n' '${ACME_EMAIL}' > ${FYN_EDGE_DIR}/.env && chmod 600 ${FYN_EDGE_DIR}/.env"
if [ -f "${JITRAA_REPO}/deploy/edge/jitraa.caddy" ]; then
  fyn_ssh "cat > ${FYN_EDGE_DIR}/sites/jitraa.caddy" < "${JITRAA_REPO}/deploy/edge/jitraa.caddy"
  echo "    seeded sites/jitraa.caddy from ${JITRAA_REPO}"
elif ! fyn_ssh "[ -f ${FYN_EDGE_DIR}/sites/jitraa.caddy ]"; then
  fyn_die "no sites/jitraa.caddy and no jitraa repo at ${JITRAA_REPO}. Edge would take over :443 without jitraa's routes."
fi

# ── 5. Validate before anything binds a port ────────────────────────────────
echo "==> validating combined config"
fyn_ssh "docker run --rm \
  -v ${FYN_EDGE_DIR}/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v ${FYN_EDGE_DIR}/sites:/etc/caddy/sites:ro \
  -e ACME_EMAIL='${ACME_EMAIL}' \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" \
  || fyn_die "edge config is invalid. Nothing was changed; the current proxy is still serving."
echo "    valid"

if [ "$SWAP" != true ]; then
  echo ""
  echo "prepared. Nothing has been cut over — jitraa's Caddy is still serving."
  echo "Run again with --swap to take over :80/:443."
  exit 0
fi

# ── 6. Cutover ──────────────────────────────────────────────────────────────
echo ""
echo "==> cutover"
fyn_ssh "grep -q '80:80' /opt/jitraa/deploy/docker-compose.yml" \
  && fyn_die "/opt/jitraa/deploy/docker-compose.yml still publishes :80. Deploy the updated jitraa repo first (its web service must join the edge network instead)."

echo "    building jitraa web image (old container keeps serving)"
fyn_ssh "cd /opt/jitraa/deploy && docker compose build web" >/dev/null \
  || fyn_die "jitraa web build failed. Nothing was cut over."

echo "    swapping — downtime starts here"
swap_start=$(date +%s)
fyn_ssh "cd /opt/jitraa/deploy && docker compose up -d --no-deps web" >/dev/null \
  || fyn_die "recreating jitraa-web failed. ROLLBACK: restore deploy/docker-compose.yml and deploy/Caddyfile in the jitraa repo, then 'docker compose up -d --no-deps --build web'."
fyn_ssh "cd ${FYN_EDGE_DIR} && docker compose up -d" >/dev/null \
  || fyn_die "edge failed to start and :443 is now unbound. ROLLBACK immediately: revert the jitraa repo's deploy/ files and run 'docker compose up -d --no-deps --build web' in /opt/jitraa/deploy."
echo "    swapped in $(( $(date +%s) - swap_start ))s"

echo "==> verifying"
sleep 5
for url in https://jitraa.com/ https://admin.jitraa.com/ https://api.jitraa.com/health; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$url" || echo "000")
  printf "    %-34s %s\n" "$url" "$code"
done
echo ""
echo "If any of those regressed, roll back with:"
echo "  git -C ${JITRAA_REPO} checkout deploy/docker-compose.yml deploy/Caddyfile"
echo "  ssh ${FYN_SSH} 'cd ${FYN_EDGE_DIR} && docker compose down'"
echo "  cd ${JITRAA_REPO} && JITRAA_SSH_KEY=~/.ssh/id_ed25519 ./deploy/deploy.sh web"
