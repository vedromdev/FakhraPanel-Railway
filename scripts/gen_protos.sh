#!/usr/bin/env bash
# Generate the Python gRPC stubs for Xray's StatsService into panel/xray/pb/.
#
# Regenerate (run once per checkout, and after changing anything under proto/):
#   bash scripts/gen_protos.sh
#
# Requires grpcio-tools (see requirements-dev.txt):
#   python3 -m grpc_tools.protoc ...
#
# proto/app/stats/command.proto is vendored verbatim from XTLS/Xray-core tag
# v26.3.27 (same pin as scripts/fetch_xray.sh, so the stubs match the binary
# we ship). At that tag the file has no imports; if a future pin adds some,
# list them here too so their *_pb2 modules get generated.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

OUT_DIR="panel/xray/pb"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

python3 -m grpc_tools.protoc \
  -I proto \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  proto/app/stats/command.proto

# protoc mirrors the proto file path, so the stubs land in
# ${OUT_DIR}/app/stats/. The panel imports them as panel.xray.pb.command_pb2,
# so flatten them into the pb root and rewrite the stub's intra-package import
# (sed -i.bak works with both BSD and GNU sed, unlike bare `sed -i`).
mv "${OUT_DIR}/app/stats/command_pb2.py" "${OUT_DIR}/"
mv "${OUT_DIR}/app/stats/command_pb2_grpc.py" "${OUT_DIR}/"
rm -rf "${OUT_DIR}/app"
sed -i.bak 's/^from app\.stats import command_pb2/from panel.xray.pb import command_pb2/' \
  "${OUT_DIR}/command_pb2_grpc.py"
rm -f "${OUT_DIR}/command_pb2_grpc.py.bak"

touch "${OUT_DIR}/__init__.py"

echo "generated ${OUT_DIR}/command_pb2.py and ${OUT_DIR}/command_pb2_grpc.py"
