#!/usr/bin/env bash
# docker-build-push.sh — build all ASOE images and push to Docker Hub
#
# Usage:
#   bash scripts/docker-build-push.sh                          # tag: latest
#   bash scripts/docker-build-push.sh v0.3.2                   # tag: v0.3.2
#   HTTP_PROXY=http://proxy:8080 bash scripts/docker-build-push.sh  # behind proxy
#
# Images pushed:
#   kumarabhijit/asoe:core-<tag>
#   kumarabhijit/asoe:sandbox-ui-<tag>
#   kumarabhijit/asoe:inference-<tag>
set -euo pipefail

TAG="${1:-latest}"
REPO="kumarabhijit/asoe"

# ── Proxy build args (empty if not set) ──────────────────────────────────────
PROXY_ARGS=""
if [ -n "${HTTP_PROXY:-}" ]; then
    PROXY_ARGS="$PROXY_ARGS --build-arg HTTP_PROXY=$HTTP_PROXY"
fi
if [ -n "${HTTPS_PROXY:-}" ]; then
    PROXY_ARGS="$PROXY_ARGS --build-arg HTTPS_PROXY=$HTTPS_PROXY"
fi
if [ -n "${NO_PROXY:-}" ]; then
    PROXY_ARGS="$PROXY_ARGS --build-arg NO_PROXY=$NO_PROXY"
fi

echo "==> Building ASOE images (tag: $TAG)"

echo "--- Building core ---"
docker build $PROXY_ARGS -f Dockerfile.core -t "$REPO:core-$TAG" .

echo "--- Building sandbox-ui ---"
docker build $PROXY_ARGS -f Dockerfile.ui -t "$REPO:sandbox-ui-$TAG" .

echo "--- Building inference ---"
docker build $PROXY_ARGS -f Dockerfile.inference -t "$REPO:inference-$TAG" .

echo "==> Pushing to Docker Hub ($REPO)"

docker push "$REPO:core-$TAG"
docker push "$REPO:sandbox-ui-$TAG"
docker push "$REPO:inference-$TAG"

echo "==> Done. Images pushed:"
echo "    $REPO:core-$TAG"
echo "    $REPO:sandbox-ui-$TAG"
echo "    $REPO:inference-$TAG"
