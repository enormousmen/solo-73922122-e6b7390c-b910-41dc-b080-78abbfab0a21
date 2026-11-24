#!/usr/bin/env bash
# End-to-end demo: build a 3-layer image, merge its top two layers through
# the API, and prove the old and new images expand to identical trees.
#
# The top layer of the demo image masks /app/data with an opaque whiteout
# and then adds new files into that same directory.
#
# Usage:
#   examples/demo.sh           # start the API via Docker Compose
#   DIRECT=1 examples/demo.sh  # start the API directly with uvicorn (no docker)
set -euo pipefail
cd "$(dirname "$0")/.."

LAYOUT=${LAYOUT:-oci-layout}
SRC_TAG=${SRC_TAG:-demo:1.0.1}
DST_TAG=${DST_TAG:-demo:1.0.1-squashed}
PORT=${PORT:-8080}

echo "==> 1/5  Build sample image ${SRC_TAG} (3 uncompressed layers) into ./${LAYOUT}"
python3 examples/make_sample_image.py --force --tag "${SRC_TAG}" "${LAYOUT}"

if [[ "${DIRECT:-0}" == "1" ]]; then
  echo "==> 2/5  Start API directly: uvicorn on :${PORT} (DIRECT=1)"
  # log to a file so the background process does not hold this script's
  # stdout open
  OCI_LAYOUT_DIR="${PWD}/${LAYOUT}" python3 -m uvicorn app.main:app \
    --host 127.0.0.1 --port "${PORT}" > uvicorn-demo.log 2>&1 &
  API_PID=$!
  trap 'kill ${API_PID} 2>/dev/null || true' EXIT
else
  echo "==> 2/5  Start API with Docker Compose on :${PORT}"
  docker compose up -d --build
fi

echo "==> 3/5  Wait for the API to become healthy"
for _ in $(seq 1 30); do
  curl -fsS "http://localhost:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://localhost:${PORT}/healthz"; echo

echo "==> 4/5  POST /merge  ${SRC_TAG} -> ${DST_TAG}"
curl -fsS -X POST "http://localhost:${PORT}/merge" \
  -H 'Content-Type: application/json' \
  -d "{\"source_tag\":\"${SRC_TAG}\",\"target_tag\":\"${DST_TAG}\"}" \
  | python3 -m json.tool

echo "==> 5/5  Verify that ${SRC_TAG} and ${DST_TAG} expand identically"
python3 examples/verify_equal.py "${LAYOUT}" "${SRC_TAG}" "${DST_TAG}"

echo
echo "Demo finished: the top two patch layers were squashed into one,"
echo "and the squashed image expands to the exact same tree."
