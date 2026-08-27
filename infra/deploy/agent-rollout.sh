#!/usr/bin/env bash
# Change one latency feature cohort and restart only fyn's API.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source infra/deploy/config.sh

FEATURE="${1:-}"
PERCENT="${2:-}"
case "$FEATURE" in
  delegation) KEY="ANALYSIS_DELEGATION_ROLLOUT_PERCENT" ;;
  semantic) KEY="SEMANTIC_FAST_TOOLS_ROLLOUT_PERCENT" ;;
  enrichment) KEY="AGENT_ENRICHMENT_ROLLOUT_PERCENT" ;;
  *) fyn_die "usage: $0 delegation|semantic|enrichment 0|5|25|100" ;;
esac
case "$PERCENT" in
  0|5|25|100) ;;
  *) fyn_die "rollout must be one of 0, 5, 25, or 100" ;;
esac

REMOTE_ENV="${FYN_REMOTE_DIR}/${FYN_ENV_FILE}"
fyn_ssh "set -eu; test -f '${REMOTE_ENV}'; if grep -q '^${KEY}=' '${REMOTE_ENV}'; then sed -i 's/^${KEY}=.*/${KEY}=${PERCENT}/' '${REMOTE_ENV}'; else printf '\n${KEY}=${PERCENT}\n' >> '${REMOTE_ENV}'; fi"
fyn_compose "up -d --no-deps --force-recreate fyn-backend" >/dev/null
echo "${FEATURE} rollout is now ${PERCENT}% on ${FYN_SSH}"
