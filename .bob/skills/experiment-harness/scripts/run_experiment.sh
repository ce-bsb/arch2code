#!/usr/bin/env bash
# run_experiment.sh — validation cycle for the generated prototype.
#
# Usage: bash .bob/skills/experiment-harness/scripts/run_experiment.sh <run-id>
#
# Fails fast and LOUD. A harness that swallows errors produces a report that
# lies, and a report that lies is worse than no report at all.

set -Eeuo pipefail

RUN_ID="${1:?usage: run_experiment.sh <run-id>}"
OUT_DIR=".arch/run/${RUN_ID}"
LOG="${OUT_DIR}/harness.log"
COMPOSE="${COMPOSE_FILE:-docker-compose.yml}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_DELAY="${HEALTH_DELAY:-2}"

mkdir -p "${OUT_DIR}"
: > "${LOG}"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "${LOG}"; }
step() { log ""; log "=== $* ==="; }
run()  { log "\$ $*"; "$@" 2>&1 | tee -a "${LOG}"; return "${PIPESTATUS[0]}"; }

teardown() {
  local rc=$?
  step "TEARDOWN"
  docker compose -f "${COMPOSE}" down -v --remove-orphans >>"${LOG}" 2>&1 || true
  if [ $rc -ne 0 ]; then
    log "FAILED (exit=${rc}). Full log: ${LOG}"
    log "Before you 'fix' anything: is the failure an implementation problem"
    log "(import, port, dependency) or is the AIR wrong? If it is the AIR, go back to stage 2."
  fi
  exit $rc
}
trap teardown EXIT

step "PREREQUISITES"
command -v docker >/dev/null || { log "docker not found"; exit 127; }
[ -f "${COMPOSE}" ] || { log "${COMPOSE} does not exist — did stage 4 finish?"; exit 1; }
run docker compose -f "${COMPOSE}" config -q

step "BUILD"
run docker compose -f "${COMPOSE}" build

step "UP"
run docker compose -f "${COMPOSE}" up -d

step "HEALTH"
# A service that starts is not a service that is ready. Without this wait, the
# test fails on a race and someone blames the architecture for a timing problem.
services=$(docker compose -f "${COMPOSE}" config --services)
for svc in ${services}; do
  port=$(docker compose -f "${COMPOSE}" port "${svc}" 8080 2>/dev/null | cut -d: -f2 || true)
  [ -z "${port}" ] && { log "  ${svc}: no port 8080 exposed, skipping"; continue; }
  ok=0
  for i in $(seq 1 "${HEALTH_RETRIES}"); do
    if curl -fsS "http://localhost:${port}/health" >/dev/null 2>&1; then
      log "  ${svc}: healthy after ${i}x${HEALTH_DELAY}s"; ok=1; break
    fi
    sleep "${HEALTH_DELAY}"
  done
  if [ "${ok}" -eq 0 ]; then
    log "  ${svc}: did NOT become healthy — last lines:"
    docker compose -f "${COMPOSE}" logs --tail=40 "${svc}" | tee -a "${LOG}"
    exit 1
  fi
done

step "HYPOTHESIS TESTS"
if [ -d tests ]; then
  run python3 -m pytest tests -v --tb=short
else
  log "tests/ directory missing — stage 5 has to write the tests first"
  exit 1
fi

step "DONE"
log "Evidence: ${LOG}"
log "Now write ${OUT_DIR}/validation.md with VERIFIED/REFUTED/INCONCLUSIVE"
log "per hypothesis, WITH the real output pasted in. Without pasted output, do not mark VERIFIED."
