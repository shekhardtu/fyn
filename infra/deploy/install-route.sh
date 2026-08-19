#!/usr/bin/env bash
# Install fyn's site file on the shared edge proxy and reload it.
#
# Called by setup-server.sh and by every deploy, so the route on the box always
# matches the one in this repo. Validate-then-reload, never restart: a restart
# would drop every other application's connections, and an invalid config would
# take the whole box's ingress down.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

sed -e "s|{\$FYN_DOMAIN}|${FYN_DOMAIN}|g" infra/deploy/fyn.caddy \
  | fyn_ssh "cat > ${FYN_EDGE_DIR}/sites/fyn.caddy"

if ! fyn_ssh "docker exec ${FYN_EDGE_CONTAINER} caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" >/dev/null 2>&1; then
  fyn_ssh "rm -f ${FYN_EDGE_DIR}/sites/fyn.caddy"
  fyn_die "the edge config is invalid with fyn's route added — it has been removed again and NOT reloaded, so every other site on the box is untouched. Check infra/deploy/fyn.caddy."
fi

fyn_ssh "docker exec -w /etc/caddy ${FYN_EDGE_CONTAINER} caddy reload --config /etc/caddy/Caddyfile" >/dev/null \
  || fyn_die "caddy reload failed. The previous config is still live."
echo "    ${FYN_DOMAIN} -> ${FYN_EDGE_ALIAS}:${FYN_EDGE_PORT} (validated, reloaded)"
