#!/usr/bin/env bash
# ============================================================================
# fyn — one-time server bootstrap on the shared Hetzner box
# ============================================================================
#
# Idempotent. Re-run after changing the domain or the site file; it never
# overwrites the server's backend/.env once that exists.
#
#   ./infra/deploy/setup-server.sh
#
# Assumes the shared ingress is already installed — see infra/edge/install.sh.
# It does NOT deploy; run ./infra/deploy/deploy.sh after this.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

echo "fyn server setup"
echo "  host    ${FYN_SSH}"
echo "  dir     ${FYN_REMOTE_DIR}"
echo "  api     ${FYN_DOMAIN}"
echo "  spa     ${FYN_APP_ORIGIN}  (hosted off this box)"
echo ""

# ── 1. Reachability ─────────────────────────────────────────────────────────
echo "==> checking SSH"
fyn_ssh true 2>/dev/null || fyn_die "cannot SSH to ${FYN_SSH}.
  Add your key on the box (from the Hetzner console, as root):
      ssh-import-id-gh shekhardtu"

echo "==> checking server prerequisites"
missing=$(fyn_ssh 'for c in git docker; do command -v $c >/dev/null 2>&1 || echo $c; done' || true)
[ -z "$missing" ] || fyn_die "missing on the server: ${missing//$'\n'/ }"
fyn_ssh 'docker compose version >/dev/null 2>&1' \
  || fyn_die "the docker compose plugin is not installed on the server."
echo "    git, docker, compose: present"

# ── 2. Shared ingress ───────────────────────────────────────────────────────
# fyn contributes one file to edge and reaches it over one network. Both must
# already exist; creating them is edge's job, never an app's.
echo "==> checking shared ingress"
fyn_ssh "docker network inspect ${FYN_EDGE_NETWORK} >/dev/null 2>&1" \
  || fyn_die "the '${FYN_EDGE_NETWORK}' network does not exist. Run ./infra/edge/install.sh first."
fyn_ssh "docker inspect ${FYN_EDGE_CONTAINER} >/dev/null 2>&1" \
  || fyn_die "${FYN_EDGE_CONTAINER} is not running. Run ./infra/edge/install.sh --swap first."
echo "    ${FYN_EDGE_CONTAINER} on network '${FYN_EDGE_NETWORK}'"

# ── 3. Checkout ─────────────────────────────────────────────────────────────
echo "==> checking checkout at ${FYN_REMOTE_DIR}"
if fyn_ssh "[ -d ${FYN_REMOTE_DIR}/.git ]"; then
  echo "    present"
else
  # Deploy key, GitHub host key, and the namespaced SSH alias. All three are
  # idempotent, so a re-run after adding the key on GitHub just proceeds.
  echo "    preparing git access"
  fyn_ssh "mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
    ssh-keygen -F github.com >/dev/null 2>&1 || ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null; \
    [ -f ${FYN_DEPLOY_KEY} ] || ssh-keygen -t ed25519 -N '' -C 'fyn-deploy@shared-box' -f ${FYN_DEPLOY_KEY} >/dev/null; \
    grep -q '^Host ${FYN_SSH_ALIAS}\$' /root/.ssh/config 2>/dev/null || printf '\nHost ${FYN_SSH_ALIAS}\n  HostName github.com\n  User git\n  IdentityFile ${FYN_DEPLOY_KEY}\n  IdentitiesOnly yes\n' >> /root/.ssh/config; \
    chmod 600 /root/.ssh/config"

  echo "    cloning ${FYN_REPO_URL}"
  if ! fyn_ssh "mkdir -p $(dirname ${FYN_REMOTE_DIR}) && git clone --branch ${FYN_BRANCH} ${FYN_REPO_URL} ${FYN_REMOTE_DIR}"; then
    echo ""
    echo "  The server cannot read ${FYN_REPO_SLUG} yet. Add this as a"
    echo "  READ-ONLY deploy key at https://github.com/${FYN_REPO_SLUG}/settings/keys :"
    echo ""
    fyn_ssh "cat ${FYN_DEPLOY_KEY}.pub"
    echo ""
    fyn_die "then re-run this script."
  fi
fi

# ── 4. Server environment file ──────────────────────────────────────────────
echo "==> checking ${FYN_REMOTE_DIR}/${FYN_ENV_FILE}"
if fyn_ssh "[ -f ${FYN_REMOTE_DIR}/${FYN_ENV_FILE} ]"; then
  echo "    present — left untouched"
else
  echo "    seeding from .env.example with production defaults"
  # Secrets are generated ON the server so they never transit this machine's
  # shell history or scrollback.
  fyn_ssh "cd ${FYN_REMOTE_DIR} && \
    cp backend/.env.example ${FYN_ENV_FILE} && \
    AUTH=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))') && \
    PGPW=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') && \
    sed -i \
      -e \"s|^ENVIRONMENT=.*|ENVIRONMENT=production|\" \
      -e \"s|^AUTH_SECRET=.*|AUTH_SECRET=\$AUTH|\" \
      -e \"s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=true|\" \
      -e \"s|^OTP_DEBUG_ECHO=.*|OTP_DEBUG_ECHO=false|\" \
      -e \"s|^CORS_ORIGINS=.*|CORS_ORIGINS=${FYN_APP_ORIGIN}|\" \
      -e \"s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://finance:\$PGPW@postgres:5432/finance|\" \
      ${FYN_ENV_FILE} && \
    printf '\n# Added by infra/deploy/setup-server.sh\nPOSTGRES_PASSWORD=%s\n' \"\$PGPW\" >> ${FYN_ENV_FILE} && \
    chmod 600 ${FYN_ENV_FILE}"
  echo ""
  echo "    !! Still blank and required before the app is usable:"
  echo "         OPENAI_API_KEY        — every agent turn needs it"
  echo "       Optional, each disables a feature while blank:"
  echo "         GOOGLE_CLIENT_ID      — Google sign-in (the SPA build needs the same value)"
  echo "         MSG91_AUTH_KEY/…      — SMS one-time codes"
  echo "         POSTMARK_SERVER_TOKEN — email one-time codes"
  echo "       Edit with:  ssh ${FYN_SSH} 'nano ${FYN_REMOTE_DIR}/${FYN_ENV_FILE}'"
  echo ""
fi

# ── 5. Route on the shared proxy ────────────────────────────────────────────
echo "==> checking DNS for ${FYN_DOMAIN}"
resolved=$(dig +short "${FYN_DOMAIN}" A | tail -1 || true)
if [ "$resolved" = "$FYN_HOST" ]; then
  echo "    -> ${FYN_HOST}"
else
  echo "    warning: resolves to '${resolved:-nothing}', not ${FYN_HOST}."
  echo "    The route is installed anyway, but Caddy cannot obtain a certificate"
  echo "    until an A record points here. Keep the record DNS-only (grey cloud):"
  echo "    Cloudflare's proxy buffers the agent's server-sent events."
fi

echo "==> installing the fyn route into ${FYN_EDGE_DIR}/sites"
bash infra/deploy/install-route.sh
echo ""
echo "setup complete. Next: ./infra/deploy/deploy.sh"
