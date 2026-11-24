#!/usr/bin/env bash
# 独立发包脚本：只构建本仓库的 M1/M2 镜像，与 Suunto-Agent 的 Jenkins/release.py 无关。
#
# 用法：
#   ./deploy/build.sh                              # 冒烟 + 构建两个本地镜像
#   REGISTRY=acr.example.com/ns ./deploy/build.sh  # 构建并推送（先 docker login）
#   VERSION=0.2.0 ./deploy/build.sh                # 覆盖 VERSION 文件里的版本
#   SKIP_SMOKE=1 ./deploy/build.sh                 # 跳过冒烟（不建议）
#
# 产物：suunto-langfuse-bridge-m1:<version> 与 suunto-langfuse-bridge-m2:<version>。
# 部署侧用 deploy/docker-compose.yml（BRIDGE_VERSION 对齐同一版本）。
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-$(cat VERSION)}"
[[ "$VERSION" =~ ^[0-9A-Za-z._-]+$ ]] || { echo "invalid VERSION: $VERSION" >&2; exit 1; }

if [[ "${SKIP_SMOKE:-0}" != "1" ]]; then
  bash probes/local-smoke.sh
fi

for spec in "m1:deploy/Dockerfile.m1" "m2:deploy/Dockerfile.collector"; do
  name="${spec%%:*}"
  dockerfile="${spec#*:}"
  image="suunto-langfuse-bridge-${name}:${VERSION}"
  echo "→ build ${image}"
  docker build -f "$dockerfile" -t "$image" .
  if [[ -n "${REGISTRY:-}" ]]; then
    docker tag "$image" "${REGISTRY}/${image}"
    docker push "${REGISTRY}/${image}"
    echo "→ pushed ${REGISTRY}/${image}"
  fi
done

echo "done: version ${VERSION}"