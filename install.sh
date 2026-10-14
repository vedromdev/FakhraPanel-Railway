#!/usr/bin/env bash
# Fakhra Panel — one-command VPS installer (Ubuntu/Debian, run as root).
#
# Installs Docker if needed, writes .env (prompts, or pass ADMIN_USERNAME /
# ADMIN_PASSWORD / PUBLIC_HOST as env vars for non-interactive use), opens
# the firewall, enables BBR + TCP buffers, and starts the TLS stack:
#   docker compose up -d --build vps
# then polls https://<domain>/api/health until green.
#
# Idempotent: re-running keeps an existing .env and just re-syncs the stack.
set -euo pipefail

say()  { printf '\033[1;32m[fakhra]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[fakhra]\033[0m %s\n' "$*" >&2; }
die()  { warn "ERROR: $*"; exit 1; }

cd "$(dirname "$0")"

# --- 1. Preconditions --------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh)"
[ -f /etc/debian_version ] || die "Ubuntu/Debian required (/etc/debian_version missing)"
if ! command -v curl >/dev/null 2>&1; then
  say "installing curl..."
  apt-get update -y >/dev/null && apt-get install -y curl >/dev/null
fi

# --- 2. Docker ---------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 ||
  die "docker compose plugin missing (package: docker-compose-plugin)"

# --- 3. .env (prompts only when missing; env vars skip the prompts) ----------
if [ ! -f .env ]; then
  if [ -z "${ADMIN_USERNAME:-}" ]; then
    read -r -p "Admin username [admin]: " ADMIN_USERNAME || true
  fi
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    read -r -p "Admin password (empty = generate): " ADMIN_PASSWORD || true
  fi
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    ADMIN_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
    say "generated admin password: ${ADMIN_PASSWORD}"
  fi
  if [ -z "${PUBLIC_HOST:-}" ]; then
    read -r -p "Public domain (A record -> this server): " PUBLIC_HOST || true
  fi
  [ -n "${PUBLIC_HOST:-}" ] || die "PUBLIC_HOST is required (point your domain's A record here first)"
  case "${PUBLIC_HOST}" in
    *://*|*/*|*:*) die "PUBLIC_HOST must be a bare hostname, got: ${PUBLIC_HOST}" ;;
  esac
  cat > .env <<EOF
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
PUBLIC_HOST=${PUBLIC_HOST}
AUTO_TLS=true
EOF
  say ".env written"
else
  say ".env exists — keeping it (edit it to change settings)"
  grep -q '^AUTO_TLS=true' .env || warn "AUTO_TLS=true not set in .env — HTTPS mode is off"
  PUBLIC_HOST="$(sed -n 's/^PUBLIC_HOST=//p' .env | head -1)"
  [ -n "${PUBLIC_HOST}" ] || die ".env has no PUBLIC_HOST"
fi

# --- 4. Firewall (ufw only; other firewalls must be opened by hand) ----------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
  say "ufw: opened 80/tcp and 443/tcp"
fi

# --- 5. Kernel tuning: BBR + buffers (optional speed baseline) ---------------
modprobe tcp_bbr 2>/dev/null || true
if sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw bbr; then
  cat > /etc/sysctl.d/99-fakhra-panel.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
EOF
  sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-fakhra-panel.conf >/dev/null
  say "BBR + TCP buffers enabled"
else
  warn "BBR unavailable on this kernel — skipping (tuning is optional)"
fi

# --- 6. Deploy ---------------------------------------------------------------
say "building + starting the vps stack..."
docker compose up -d --build vps

# --- 7. Health poll (Let's Encrypt issuance can take ~30s) -------------------
say "waiting for HTTPS health at https://${PUBLIC_HOST}/api/health ..."
healthy=""
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "https://${PUBLIC_HOST}/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [ -n "${healthy}" ]; then
  say "panel is up: https://${PUBLIC_HOST}/"
  say "health: $(curl -fsS --max-time 3 "https://${PUBLIC_HOST}/api/health" || true)"
  say "VPN links use https://${PUBLIC_HOST}:443 — log in, create users, import their links."
else
  warn "not healthy yet. Check: docker compose logs vps --tail 50"
  warn "usual causes: DNS A record not pointing here, or 80/443 blocked"
  exit 1
fi
