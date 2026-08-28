#!/usr/bin/env bash
# One-command localhost browser quality and latency release gate.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

API_URL="${VITE_API_URL:-http://localhost:8000}"
APP_URL="${PLAYWRIGHT_BASE_URL:-http://localhost:3000}"
ARTIFACT_DIR="frontend/test-results/agent-quality"
BROWSER_RESULTS="${ARTIFACT_DIR}/browser.json"
RELEASE_RESULTS="${ARTIFACT_DIR}/release.json"
QUALITY_PHONE="+919000000098"
BACKEND_PID=""
FRONTEND_PID=""

case "${API_URL}" in
  http://localhost:*|http://127.0.0.1:*) ;;
  *) echo "error: agent quality gate only runs against a localhost API" >&2; exit 1 ;;
esac
case "${APP_URL}" in
  http://localhost:*|http://127.0.0.1:*) ;;
  *) echo "error: agent quality gate only runs against a localhost frontend" >&2; exit 1 ;;
esac

mkdir -p "${ARTIFACT_DIR}" frontend/e2e/.auth
rm -f \
  frontend/e2e/.auth/quality-thread.json \
  "${BROWSER_RESULTS}" \
  "${RELEASE_RESULTS}"

cleanup() {
  if [ "${AGENT_QUALITY_KEEP_FIXTURE:-1}" != "1" ]; then
    (cd backend && ../.venv/bin/python scripts/seed_agent_quality_eval.py --cleanup) >/dev/null 2>&1 || true
    rm -f frontend/e2e/.auth/quality-session.json frontend/e2e/.auth/quality-thread.json
  fi
  if [ -n "${FRONTEND_PID}" ]; then kill "${FRONTEND_PID}" >/dev/null 2>&1 || true; fi
  if [ -n "${BACKEND_PID}" ]; then kill "${BACKEND_PID}" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT INT TERM

echo "==> preparing localhost database"
make db migrate
(cd backend && ../.venv/bin/python scripts/seed_agent_quality_eval.py)

if ! curl -fsS --max-time 2 "${API_URL}/health" >/dev/null 2>&1; then
  echo "==> starting backend"
  (cd backend && exec ../.venv/bin/uvicorn app.main:app) >"${ARTIFACT_DIR}/backend.log" 2>&1 &
  BACKEND_PID="$!"
fi
if ! curl -fsS --max-time 2 "${APP_URL}" >/dev/null 2>&1; then
  echo "==> starting frontend"
  (cd frontend && exec yarn dev) >"${ARTIFACT_DIR}/frontend.log" 2>&1 &
  FRONTEND_PID="$!"
fi

wait_for_url() {
  local label="$1"
  local url="$2"
  local deadline=$(( $(date +%s) + 90 ))
  until curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
      echo "error: ${label} did not become ready at ${url}" >&2
      return 1
    fi
    sleep 1
  done
}

wait_for_url "backend" "${API_URL}/health"
wait_for_url "frontend" "${APP_URL}"

echo "==> running real-browser quality corpus"
set +e
(
  cd frontend
  FYN_E2E_TEST_PHONE="${QUALITY_PHONE}" \
  FYN_E2E_STORAGE_STATE="e2e/.auth/quality-session.json" \
  FYN_E2E_THREAD_STATE="e2e/.auth/quality-thread.json" \
  AGENT_QUALITY_RESULTS_PATH="test-results/agent-quality/browser.json" \
  PLAYWRIGHT_BASE_URL="${APP_URL}" \
  VITE_API_URL="${API_URL}" \
  yarn test:e2e:quality
)
browser_status="$?"
set -e

if [ ! -f "${BROWSER_RESULTS}" ]; then
  echo "error: browser corpus produced no gradeable artifact" >&2
  exit "${browser_status:-1}"
fi

echo "==> judging answers and enforcing combined release gates"
set +e
(
  cd backend
  ../.venv/bin/python scripts/report_agent_quality.py \
    --browser-results "../${BROWSER_RESULTS}" \
    --output "../${RELEASE_RESULTS}"
)
report_status="$?"
set -e

if [ "${browser_status}" -ne 0 ]; then
  echo "error: Playwright infrastructure failed; see ${ARTIFACT_DIR}" >&2
  exit "${browser_status}"
fi
exit "${report_status}"
