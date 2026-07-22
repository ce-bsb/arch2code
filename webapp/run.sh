#!/usr/bin/env bash
#
# arch2code webapp — single entry point.
#
#   ./run.sh
#
# No build step, no package manager, no network. Everything the front end needs
# is plain files under webapp/static.
#
# This script starts the LOCAL tool. It binds to 127.0.0.1 and ships no
# authentication of its own, so do not put it on a public interface as it is.
#
# Bob's licence (clause 53d) permits use by the licensee, its employees and its
# contractors, and forbids providing hosting or a commercial service to third
# parties. Read it before putting this behind a public address.

set -euo pipefail

WEBAPP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT_DEFAULT="$(cd "${WEBAPP_DIR}/.." && pwd)"

# The repo root is the CWD contract for Bob: it is the directory whose
# .bob/custom_modes.yaml defines the six arch2code chat modes.
export ARCH2CODE_PROJECT_ROOT="${ARCH2CODE_PROJECT_ROOT:-${PROJECT_ROOT_DEFAULT}}"

# The system python3 on this machine is 3.9.6 and has none of the required
# libraries. Everything is launched with this interpreter explicitly.
ARCH2CODE_PYTHON="${ARCH2CODE_PYTHON:-/opt/anaconda3/bin/python}"
export ARCH2CODE_PYTHON

if [ ! -x "${ARCH2CODE_PYTHON}" ]; then
  echo "ERROR: interpreter not found or not executable: ${ARCH2CODE_PYTHON}" >&2
  echo "Remedy: set ARCH2CODE_PYTHON to a Python 3.10+ that has fastapi, uvicorn," >&2
  echo "        pydantic, mcp, httpx and pillow installed. Check with:" >&2
  echo "        <interpreter> -c 'import fastapi, uvicorn, pydantic, mcp, httpx, PIL'" >&2
  exit 1
fi

if ! "${ARCH2CODE_PYTHON}" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "ERROR: ${ARCH2CODE_PYTHON} cannot import fastapi/uvicorn." >&2
  echo "Remedy: ${ARCH2CODE_PYTHON} -m pip install -r ${WEBAPP_DIR}/requirements.txt" >&2
  exit 1
fi

HOST="${ARCH2CODE_HOST:-127.0.0.1}"
PORT="${ARCH2CODE_PORT:-8765}"
export ARCH2CODE_HOST="${HOST}" ARCH2CODE_PORT="${PORT}"

mkdir -p "${WEBAPP_DIR}/runs" "${WEBAPP_DIR}/uploads" "${WEBAPP_DIR}/static"

echo "arch2code webapp"
echo "  project root : ${ARCH2CODE_PROJECT_ROOT}"
echo "  interpreter  : ${ARCH2CODE_PYTHON}"
echo "  bob binary   : ${ARCH2CODE_BOB_BIN:-<auto: \`bob\` on PATH>}"
echo "  listening on : http://${HOST}:${PORT}"
echo

# One worker only, and that is deliberate: run coordination is a per-run
# asyncio.Lock inside this process. A second worker would not see the first's
# locks and two of them could drive the same run.
cd "${WEBAPP_DIR}"
exec "${ARCH2CODE_PYTHON}" -m uvicorn app.main:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers 1 \
  --no-access-log \
  "$@"
