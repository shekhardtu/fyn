#!/usr/bin/env bash
# Shared settings for every script in infra/deploy.
#
# Everything is overridable from the environment so a second box, a staging
# hostname, or a different checkout path needs no edit here:
#   FYN_HOST=1.2.3.4 ./infra/deploy/deploy.sh

# ── Target box ───────────────────────────────────────────────────────────────
# A *shared* Hetzner host (2 vCPU / 3.7 GiB): the `jitraa` stack runs here too.
# This box runs fyn's API and database ONLY — the SPA is built and served
# elsewhere. Every fyn resource is namespaced under the compose project below,
# nothing is published to a host port, and only the API container touches the
# shared ingress network. See infra/edge/README.md for the boundary contract.
FYN_HOST="${FYN_HOST:-49.13.87.106}"
FYN_SSH_USER="${FYN_SSH_USER:-root}"
FYN_SSH="${FYN_SSH_USER}@${FYN_HOST}"

# Checkout on the server. Deploys only ever fast-forward this to origin/main.
FYN_REMOTE_DIR="${FYN_REMOTE_DIR:-/opt/fyn}"
# Cloned through a namespaced SSH alias, not github.com directly: the box is
# shared, and a bare `Host github.com` entry would hand fyn's deploy key to
# every other application that ever clones something here. setup-server.sh
# writes the alias into /root/.ssh/config.
FYN_REPO_SLUG="${FYN_REPO_SLUG:-shekhardtu/fyn}"
FYN_SSH_ALIAS="${FYN_SSH_ALIAS:-github-fyn}"
FYN_REPO_URL="${FYN_REPO_URL:-git@${FYN_SSH_ALIAS}:${FYN_REPO_SLUG}.git}"
FYN_DEPLOY_KEY="${FYN_DEPLOY_KEY:-/root/.ssh/fyn_deploy}"
FYN_BRANCH="${FYN_BRANCH:-main}"

# ── Compose ──────────────────────────────────────────────────────────────────
# The project name namespaces containers, network, and volumes on the shared
# box (fyn-backend-1, fyn_finance-postgres, …) so a neighbouring stack can
# never collide with ours.
FYN_PROJECT="${FYN_PROJECT:-fyn}"
FYN_COMPOSE_FILE="${FYN_COMPOSE_FILE:-infra/deploy/docker-compose.prod.yml}"
FYN_ENV_FILE="${FYN_ENV_FILE:-backend/.env}"

# ── Shared ingress ───────────────────────────────────────────────────────────
# The `edge` compose project owns :80/:443 for the whole box. fyn contributes
# exactly one file to it and reloads it — never restarts it.
FYN_EDGE_DIR="${FYN_EDGE_DIR:-/opt/edge}"
FYN_EDGE_CONTAINER="${FYN_EDGE_CONTAINER:-edge-caddy-1}"
FYN_EDGE_NETWORK="${FYN_EDGE_NETWORK:-edge}"
# The alias fyn's API answers to on that network. Must match the compose file.
FYN_EDGE_ALIAS="${FYN_EDGE_ALIAS:-fyn-backend}"
FYN_EDGE_PORT="${FYN_EDGE_PORT:-8000}"

# ── Public surface ───────────────────────────────────────────────────────────
# The API's own hostname. Must stay DNS-only (grey cloud) in Cloudflare: the
# orange proxy buffers the agent's server-sent events.
FYN_DOMAIN="${FYN_DOMAIN:-api.fynai.co}"
FYN_HEALTH_URL="${FYN_HEALTH_URL:-https://${FYN_DOMAIN}/api/health}"

# Where the SPA is served from — the apex, built and hosted off this box.
# Becomes CORS_ORIGINS on the server. The browser sends the session cookie
# only because this shares a registrable domain with FYN_DOMAIN above; moving
# the SPA to an unrelated domain would make the pair cross-site and force
# SameSite=None.
FYN_APP_ORIGIN="${FYN_APP_ORIGIN:-https://fynai.co}"

# ── Helpers ──────────────────────────────────────────────────────────────────
# Non-interactive by design: a deploy must never block on a passphrase or a
# host-key prompt inside an agent session.
fyn_ssh() {
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$FYN_SSH" "$@"
}

fyn_compose() {
  # Runs compose on the server, from the checkout root.
  fyn_ssh "cd ${FYN_REMOTE_DIR} && docker compose -p ${FYN_PROJECT} -f ${FYN_COMPOSE_FILE} --env-file ${FYN_ENV_FILE} $*"
}

# Reaches the app from inside the shared network — the only way in, now that
# nothing is published to a host port. caddy:2-alpine carries busybox wget.
fyn_internal_curl() {
  fyn_ssh "docker exec ${FYN_EDGE_CONTAINER} wget -qO- --timeout=20 http://${FYN_EDGE_ALIAS}:${FYN_EDGE_PORT}$1"
}

fyn_die() { echo "error: $*" >&2; exit 1; }
