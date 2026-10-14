#!/usr/bin/env bash
# Single-container entrypoint: bootstrap (install token + Caddyfile) ->
# panel (spawns xray) -> caddy (front-multiplexes the one public port).
#
# As PID 1 it must reap children and forward SIGTERM/SIGINT so a Railway
# stop kills caddy and the panel (whose lifespan shutdown stops xray).
# Either child dying tears the container down: a half-alive container
# (panel serving loopback only, public port dead) must never look healthy.
set -euo pipefail

# The public port is caddy's: Railway injects PORT (8080 by default).
export CADDY_PORT="${CADDY_PORT:-${PORT:-8080}}"
# AUTO_TLS mode: caddy owns :80 (LE HTTP-01 + redirect) and the
# {$PUBLIC_HOST} site on :443 — the readiness probe targets 443, and the
# rendered Caddyfile no longer references CADDY_PORT at all.
if [ "${AUTO_TLS:-}" = "true" ]; then
  export CADDY_PORT=443
fi
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

stopping=0
caddy_pid=""
panel_pid=""
shutdown() {
  # caddy first (stop accepting), then the panel; the panel's lifespan
  # shutdown stops its xray child via the manager.
  if [ -n "${caddy_pid}" ]; then
    kill "${caddy_pid}" 2>/dev/null || true
  fi
  if [ -n "${panel_pid}" ]; then
    kill "${panel_pid}" 2>/dev/null || true
  fi
}
# `stopping` marks "a signal arrived" — set only by the trap, never by the
# teardown itself, so readiness failures can still exit 1 (not 143).
trap 'stopping=1; shutdown' TERM INT

python3 -m panel.main &
panel_pid=$!
if [ "${stopping}" -eq 1 ]; then
  exit 143 # SIGTERM landed between the trap and the spawn
fi

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
  shutdown
  if [ "${stopping}" -eq 1 ]; then
    exit 143 # SIGTERM during startup, not a readiness failure
  fi
  echo "entrypoint: panel not listening on ${PORT} within 10s" >&2
  exit 1
fi

caddy run --config "${DATA_DIR}/Caddyfile" --adapter caddyfile &
caddy_pid=$!

# Wait (max 10 s) for caddy to accept on the public port; a caddy that
# died at startup (port in use, bad config) fails fast instead of leaving
# a panel-only container that looks healthy but is publicly dead.
caddy_ready=0
for _ in $(seq 1 100); do
  if ! kill -0 "${caddy_pid}" 2>/dev/null; then
    break # dead already; handled by the failure branch below
  fi
  if (exec 4<>"/dev/tcp/127.0.0.1/${CADDY_PORT}") 2>/dev/null; then
    caddy_ready=1
    break
  fi
  sleep 0.1
done
if [ "${caddy_ready}" -ne 1 ]; then
  shutdown
  if [ "${stopping}" -eq 1 ]; then
    exit 143 # SIGTERM during startup, not a readiness failure
  fi
  echo "entrypoint: caddy not listening on ${CADDY_PORT} within 10s" >&2
  exit 1
fi

# Supervise both children (portable monitor loop; no `wait -n` so bash
# 3.2 works too): whichever dies first tears the container down. The exit
# code is the panel's; a caddy death is always a failure. The restart
# decision belongs to Railway's policy, not a half-alive container.
rc=0
while :; do
  if ! kill -0 "${panel_pid}" 2>/dev/null; then
    wait "${panel_pid}" || rc=$? # reap; surface its code (143 on SIGTERM)
    break
  fi
  if ! kill -0 "${caddy_pid}" 2>/dev/null; then
    wait "${caddy_pid}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
      rc=1 # caddy exiting "cleanly" on its own is still an outage
    fi
    break
  fi
  sleep 1
done
shutdown
exit "${rc}"
