#!/usr/bin/env bash
# End-to-end demo for non-root entrypoint verification.
#
# Builds three tags in ./oci-layout:
#   entrydemo:1.0  -> bare `serve` selects /opt/bin1/serve
#   entrydemo:1.1  -> upper layer chmods /opt/bin1 to 0700, so the checker
#                     reports /opt/bin1 blocked (permission_denied) and
#                     re-selects /opt/bin2/serve
#   entrydemo:wd   -> WorkingDir /app is locked 0700; cwd check fails first
#
# Then calls POST /verify-entrypoint for each tag as uid=1000 gid=1000.
# The same requests can be fired from the Swagger UI at /docs.
#
# Usage:
#   examples/demo_entrypoint.sh           # Docker Compose
#   DIRECT=1 examples/demo_entrypoint.sh  # local uvicorn (no docker)
set -euo pipefail
cd "$(dirname "$0")/.."

LAYOUT=${LAYOUT:-oci-layout}
UID_RUN=${UID_RUN:-1000}
GID_RUN=${GID_RUN:-1000}
PORT=${PORT:-8080}

echo "==> 1/4  Build entrypoint demo tags into ./${LAYOUT}"
python3 examples/make_entrypoint_demo.py --force --uid "${UID_RUN}" --gid "${GID_RUN}" "${LAYOUT}"

if [[ "${DIRECT:-0}" == "1" ]]; then
  echo "==> 2/4  Start API directly: uvicorn on :${PORT} (DIRECT=1)"
  OCI_LAYOUT_DIR="${PWD}/${LAYOUT}" python3 -m uvicorn app.main:app \
    --host 127.0.0.1 --port "${PORT}" > uvicorn-entrypoint-demo.log 2>&1 &
  API_PID=$!
  trap 'kill ${API_PID} 2>/dev/null || true' EXIT
else
  echo "==> 2/4  Start API with Docker Compose on :${PORT} (non-root)"
  APP_UID="$(id -u)" APP_GID="$(id -g)" HOST_PORT="${PORT}" docker compose up -d --build
fi

echo "==> 3/4  Wait for /healthz"
for _ in $(seq 1 40); do
  curl -fsS "http://localhost:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://localhost:${PORT}/healthz"; echo
echo "    Swagger UI: http://localhost:${PORT}/docs"

summarize() {
  python3 -c '
import json, sys
r = json.load(sys.stdin)
print("    ok         :", r["ok"], "(", r["status"], ")")
print("    entrypoint :", r["entrypoint"])
wd = r["working_dir"]
print("    workdir    :", wd["path"], "ok=" + str(wd["ok"]),
      ("blocked " + str(wd["blocked_at"]) + " " + str(wd["code"])) if wd["blocked_at"] else "")
for c in r["candidates"]:
    if c["ok"]:
        print("    candidate  :", c["path"], "RUNNABLE via", c.get("matched_class"))
    else:
        print("    candidate  :", c["path"], "BLOCKED at", c["blocked_at"], "-", c["code"])
'
}

for TAG in entrydemo:1.0 entrydemo:1.1 entrydemo:wd; do
  echo "==> 4/4  POST /verify-entrypoint  ${TAG} (uid=${UID_RUN} gid=${GID_RUN})"
  curl -fsS -X POST "http://localhost:${PORT}/verify-entrypoint" \
    -H 'Content-Type: application/json' \
    -d "{\"tag\":\"${TAG}\",\"uid\":${UID_RUN},\"gid\":${GID_RUN}}" | summarize
  echo
done

echo "Demo finished: upper-layer chmod of /opt/bin1 made the checker move the"
echo "selected entry from /opt/bin1/serve to /opt/bin2/serve."
