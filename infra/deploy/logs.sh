#!/usr/bin/env bash
# Tail the fyn containers on the shared box.
#
#   ./infra/deploy/logs.sh            # all services, follow
#   ./infra/deploy/logs.sh backend    # one service, follow
#   ./infra/deploy/logs.sh backend 200  # last 200 lines, follow
#
# Ctrl-C detaches; nothing on the server is affected.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

SERVICE="${1:-}"
TAIL="${2:-100}"

exec ssh -t "$FYN_SSH" \
  "cd ${FYN_REMOTE_DIR} && docker compose -p ${FYN_PROJECT} -f ${FYN_COMPOSE_FILE} logs -f --tail ${TAIL} ${SERVICE}"
