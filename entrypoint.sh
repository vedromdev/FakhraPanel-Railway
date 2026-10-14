#!/usr/bin/env bash
# Single-container entrypoint: bootstrap (install token + Caddyfile) ->
# panel (spawns xray) -> caddy (front-multiplexes the one public port).
#
# As PID 1 it must reap children and forward SIGTERM/SIGINT so a Railway
# stop kills caddy and the panel (whose lifespan shutdown stops xray).
set -euo pipefail

# The public port is caddy's: Railway injects PORT (8080 by default).
export CADDY_PORT="${CADDY_PORT:-${PORT:-8080}}"
# The panel stays on the loopback backend port the Caddyfile hardcodes.
export PORT="${PANEL_PORT:-8000}"

# Mirror panel.config.Settings.data_dir(): explicit DATA_DIR, else /data
# when present (the panel service mounts ./data there), else ./data.
if [ -z "${DATA_DIR:-}" ]; then
  if [ -d /data ]; then
    DATA_DIR=/data
  else
    DATA_DIR=./data
  fi
fi
export DATA_DIR

# ensure_runtime creates the DB and $DATA_DIR/Caddyfile, guarantees the
# install token, and prints it (its only stdout line).
VPN_TOKEN="$(python3 -m panel.bootstrap)"
export VPN_TOKEN
export CADDY_PORT="${CADDY_PORT:-$PORT}"

python3 -m panel.main &
panel_pid=$!

caddy_pid=""
shutdown() {
  # caddy first (stop accepting), then the panel; the panel's lifespan
  # shutdown stops its xray child via the manager.
  if [ -n "${caddy_pid}" ]; then
    kill "${caddy_pid}" 2>/dev/null || true
  fi
  kill "${panel_pid}" 2>/dev/null || true
}
trap shutdown TERM INT

# Wait (max 10 s) for the Caddyfile and the panel port before caddy starts.
ready=0
for _ in $(seq 1 100); do
  if [ -f "${DATA_DIR}/Caddyfile" ] &&
    (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.1
done
if [ "${ready}" -ne 1 ]; then
  echo "entrypoint: panel not ready within 10s; see logs" >&2
  shutdown
  exit 1
fi

caddy run --config "${DATA_DIR}/Caddyfile" --adapter caddyfile &
caddy_pid=$!

# Wait on the panel: when it exits (crash or forwarded signal), tear the
# rest down and surface its exit code — Railway restarts the container.
rc=0
wait "${panel_pid}" || rc=$?
shutdown
exit "${rc}"
