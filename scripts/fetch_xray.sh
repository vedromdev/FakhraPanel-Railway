#!/usr/bin/env bash
# Download the Xray-core CLI for this OS/arch into .bin/xray (idempotent).
#
#   XRAY_VERSION=<tag>   release tag to fetch (e.g. v26.3.27), or "latest"
#                        to explicitly track the newest stable release.
#
# Default: PINNED to v26.3.27 — the version validated by the real-binary
# config tests (`xray run -test`). A pinned default keeps builds reproducible;
# bump it deliberately, only after re-validating the generated configs with:
#   bash scripts/fetch_xray.sh   # (or XRAY_VERSION=<new-tag> ...)
#   XRAY_BIN=.bin/xray python3 -m pytest tests/test_config_builder.py -v
set -euo pipefail

XRAY_VERSION="${XRAY_VERSION:-v26.3.27}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BIN_DIR="${ROOT_DIR}/.bin"
BIN="${BIN_DIR}/xray"

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

# Idempotent: reuse the existing binary only when it satisfies the request.
# The version probe is guarded so a corrupt binary triggers a re-download
# instead of aborting the script under `set -euo pipefail`.
installed=""
if [ -x "${BIN}" ]; then
  installed="$("${BIN}" version 2>/dev/null | head -1 || true)"
fi
if [ -n "${installed}" ]; then
  if [ "${XRAY_VERSION}" = "latest" ]; then
    echo "already present: ${installed}"
    exit 0
  fi
  installed_tag="$(awk '{print $2}' <<< "${installed}")"
  if [ "${XRAY_VERSION#v}" = "${installed_tag}" ]; then
    echo "already present: ${installed}"
    exit 0
  fi
  echo "replacing ${installed} with requested ${XRAY_VERSION}" >&2
fi
# Remove the old binary before fetching so a failed download leaves neither
# a stale nor a truncated binary in place; publish is atomic (mv from tmp).
rm -f "${BIN}"

mkdir -p "${BIN_DIR}"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "downloading ${url}"
curl -fsSL -o "${tmp}/xray.zip" "${url}"
unzip -p "${tmp}/xray.zip" xray > "${tmp}/xray.new"
chmod +x "${tmp}/xray.new"
mv "${tmp}/xray.new" "${BIN}"

echo "installed $( "${BIN}" version | head -1 )"
"${BIN}" version
