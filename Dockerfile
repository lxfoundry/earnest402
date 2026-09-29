# --- stage 1: the browser client ----------------------------------------
# Node pinned to web/.nvmrc, so the image and CI build with the same toolchain.
FROM node:22.18.0-slim AS web

WORKDIR /web

# Dependencies first, so a source-only change reuses this layer.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./

# Compiled INTO the bundle, not read at runtime: an image is built for one
# network and cannot be repointed at another by changing the host's
# environment. The values live in the deployment's Fly config as build args.
ARG VITE_NETWORK
ARG VITE_ASSET_ID
ARG VITE_APP_ID
ARG VITE_FACILITATOR_URL
ARG VITE_RESOURCE_HOST
ARG VITE_MAX_FILE_BYTES
ARG VITE_ALGOD_URL
ARG VITE_INDEXER_URL
ARG VITE_IPFS_GATEWAY

# Which client the bundle is, and for the travel brand the offer it renders,
# passed whole as base64 of its JSON so it fits in one build argument. Both
# have a default the index brand is complete with, so neither is in the
# required list below.
ARG VITE_BRAND=index
ARG VITE_OFFER_B64=

# The client refuses to start without every one of these, and it would do so
# in a buyer's browser. Refused here instead, where the deploy is still local.
# The same holds for an unknown brand, and for a travel build whose offer is
# missing or unreadable, which would render a brand with no offer. The offer's
# fields are validated by the client at startup (web/src/config.ts); this
# check applies only the client's decoding rules, which are stricter than
# Node's own: standard padded base64 (line breaks ignored), bytes that are
# valid UTF-8, and JSON that is an object. Every refusal exits, so the RUN
# fails before the build starts.
RUN for name in VITE_NETWORK VITE_ASSET_ID VITE_APP_ID VITE_FACILITATOR_URL \
        VITE_RESOURCE_HOST VITE_MAX_FILE_BYTES VITE_ALGOD_URL VITE_INDEXER_URL \
        VITE_IPFS_GATEWAY; do \
      if [ -z "$(printenv "$name")" ]; then \
        echo "build argument $name is required" >&2; exit 1; \
      fi; \
    done \
 && case "$VITE_BRAND" in \
      index|travel) ;; \
      *) echo "build argument VITE_BRAND must be index or travel, got '$VITE_BRAND'" >&2; exit 1 ;; \
    esac \
 && if [ "$VITE_BRAND" = travel ]; then \
      if [ -z "$VITE_OFFER_B64" ]; then \
        echo "build argument VITE_OFFER_B64 is required when VITE_BRAND is travel" >&2; exit 1; \
      fi; \
      node -e "const s = process.env.VITE_OFFER_B64.replace(/[\r\n]+/g, ''); \
        if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(s)) process.exit(1); \
        const text = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(s, 'base64')); \
        const offer = JSON.parse(text); \
        if (typeof offer !== 'object' || offer === null || Array.isArray(offer)) process.exit(1);" \
        || { echo "build argument VITE_OFFER_B64 must be standard padded base64 of a UTF-8 JSON object" >&2; exit 1; }; \
    fi \
 && npm run build

# --- stage 2: each edition's free tier ----------------------------------
# The published files, named one by one rather than the whole directory:
# editions/ is also where a seat holder is told to put the dataset they
# bought, and a deploy sends the working tree. .dockerignore filters the same
# names, and each filter is tested without the other.
FROM scratch AS editions
COPY editions/edition-*-sample.html \
     editions/edition-*-sample.json \
     editions/edition-*-manifest.json \
     /

# --- stage 3: the resource server ---------------------------------------
# Python 3.12.10, the version CI runs: requirements.txt records that 3.14 fails
# to build one of algosdk's transitive dependencies from source.
FROM python:3.12.10-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY --from=web /web/dist ./web/dist
# Beside the client, so /app/editions/<file> is answered like any file of the
# bundle: byte for byte, and before the payment middleware.
COPY --from=editions / ./web/dist/editions/

# Absolute, so the client is found whatever the working directory.
ENV WEB_DIST_DIR=/app/web/dist

EXPOSE 8000

# --proxy-headers plus --forwarded-allow-ips is what makes the 402 advertise
# https://<host>/... rather than http://. The platform terminates TLS, so
# without these the resource URL in the payment requirements is wrong, and the
# resource URL is the route's catalogue identity.
CMD ["uvicorn", "api.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
