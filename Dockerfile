# Fakhra Panel — one container, one port (8080). Caddy front-multiplexes:
# /ray-<token>/<protocol> websockets to xray, everything else to the panel.
FROM python:3.12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl unzip \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Xray-core release pin (same value as scripts/fetch_xray.sh's default —
# bump deliberately, after re-validating the generated configs).
ARG XRAY_VERSION=v26.3.27
COPY scripts/ scripts/
RUN XRAY_VERSION="${XRAY_VERSION}" bash scripts/fetch_xray.sh \
  && cp .bin/xray /usr/local/bin/xray \
  && xray version

COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy

# Dev requirements double as the test image (pytest + grpcio-tools for
# gen_protos.sh); docker-compose's `test` service reuses this same image.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# panel/xray/pb is gitignored: generate the gRPC stubs at image build.
COPY proto/ proto/
RUN bash scripts/gen_protos.sh

COPY pytest.ini Caddyfile.template entrypoint.sh ./
COPY panel/ panel/
COPY tests/ tests/
RUN chmod +x entrypoint.sh

EXPOSE 8080
CMD ["./entrypoint.sh"]
