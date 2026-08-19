#!/usr/bin/env bash
# What is actually running on the box right now, and is it the commit we think.
#
#   ./infra/deploy/status.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

fyn_ssh true 2>/dev/null || fyn_die "cannot SSH to ${FYN_SSH}."

echo "── commit ─────────────────────────────────"
fyn_ssh "cd ${FYN_REMOTE_DIR} && git log -1 --pretty='%h  %s  (%ci)'"
if git rev-parse --verify --quiet "origin/${FYN_BRANCH}" >/dev/null; then
  server=$(fyn_ssh "cd ${FYN_REMOTE_DIR} && git rev-parse HEAD")
  local_head=$(git rev-parse "origin/${FYN_BRANCH}" 2>/dev/null || echo "")
  if [ -n "$local_head" ] && [ "$server" != "$local_head" ]; then
    echo "  DRIFT: origin/${FYN_BRANCH} is ${local_head:0:7}; the server is ${server:0:7}"
  fi
fi

echo ""
echo "── containers ─────────────────────────────"
fyn_ssh "docker ps --filter name=${FYN_PROJECT}- --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

echo ""
echo "── health ─────────────────────────────────"
printf "  public   %s -> " "${FYN_HEALTH_URL}"
curl -fsS --max-time 15 "${FYN_HEALTH_URL}" 2>/dev/null | head -c 300 || echo "unreachable"
echo ""
printf "  internal %s:%s -> " "${FYN_EDGE_ALIAS}" "${FYN_EDGE_PORT}"
fyn_internal_curl /api/health 2>/dev/null | head -c 300 || echo "unreachable"
echo ""

echo ""
echo "── shared box ─────────────────────────────"
fyn_ssh "free -m | awk 'NR==2 {printf \"  memory   %s MiB used of %s, %s available\\n\", \$3, \$2, \$7}'"
fyn_ssh "docker stats --no-stream --format '  {{.Name}}  {{.MemUsage}}  cpu {{.CPUPerc}}'"
