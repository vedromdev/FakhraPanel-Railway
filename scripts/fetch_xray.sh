#!/usr/bin/env bash
# Download the Xray-core CLI for this OS/arch into .bin/xray (idempotent).
#
#   XRAY_VERSION=<tag>   release tag to fetch (e.g. v26.3.27), or "latest"
#                        (default: latest stable release)
set -euo pipefail

XRAY_VERSION="${XRAY_VERSION:-latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BIN_DIR="${ROOT_DIR}/.bin"
BIN="${BIN_DIR}/xray"

# Idempotent: reuse an existing working binary.
if [ -x "${BIN}" ] && "${BIN}" version >/dev/null 2>&1; then
  echo "already present: $("${BIN}" version | head -1)"
  exit 0
fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "${os}" in
  darwin) os="macos" ;;
  linux)  os="linux" ;;
  *) echo "unsupported OS: ${os}" >&2; exit 1 ;;
esac
case "${arch}" in
  x86_64 | amd64) arch="64" ;;
  arm64 | aarch64) arch="arm64-v8a" ;;
  *) echo "unsupported arch: ${arch}" >&2; exit 1 ;;
esac

asset="Xray-${os}-${arch}.zip"
if [ "${XRAY_VERSION}" = "latest" ]; then
  url="https://github.com/XTLS/Xray-core/releases/latest/download/${asset}"
else
  url="https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${asset}"
fi

mkdir -p "${BIN_DIR}"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "downloading ${url}"
curl -fsSL -o "${tmp}/xray.zip" "${url}"
unzip -p "${tmp}/xray.zip" xray > "${BIN}"
chmod +x "${BIN}"

echo "installed $( "${BIN}" version | head -1 )"
"${BIN}" version
