#!/usr/bin/env bash
# M1 本地冒烟（发布前闸门）：假 Langfuse 接收端 + 透传服务 + 权威探针。
# 验证：4 条 POST 全部 2xx、Basic 鉴权与 ingestion 头注入正确、载荷原样透传。
# 不触达真实 Langfuse；真实实例验收仍按最终交付方案 M0 与第 07 节执行。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SMOKE_PORT:-4318}"
SINK_PORT="${SMOKE_SINK_PORT:-18080}"
LOG="$(mktemp)"
SINK_PID=""
FWD_PID=""

cleanup() {
  [[ -n "$FWD_PID" ]] && kill "$FWD_PID" 2>/dev/null || true
  [[ -n "$SINK_PID" ]] && kill "$SINK_PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT

python3 probes/smoke_sink.py "$SINK_PORT" "$LOG" &
SINK_PID=$!

LANGFUSE_TRACES_ENDPOINT="http://127.0.0.1:${SINK_PORT}/api/public/otel/v1/traces" \
LANGFUSE_PUBLIC_KEY=pk-smoke LANGFUSE_SECRET_KEY=sk-smoke \
BRIDGE_LISTEN_HOST=127.0.0.1 BRIDGE_LISTEN_PORT="$PORT" \
  python3 m1/forwarder.py &
FWD_PID=$!

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done

BRIDGE_TRACES_URL="http://127.0.0.1:${PORT}/v1/traces" bash probes/otlp-probe.sh --repeat

python3 - "$LOG" <<'PY'
import base64, json, sys
recs = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
assert len(recs) == 4, f"expect 4 forwards, got {len(recs)}"
want = "Basic " + base64.b64encode(b"pk-smoke:sk-smoke").decode()
for r in recs:
    assert r["authorization"] == want, f"bad auth: {r}"
    assert r["ingestion"] == "4", f"bad ingestion header: {r}"
    assert r["path"] == "/api/public/otel/v1/traces", f"bad path: {r}"
sizes = [r["bytes"] for r in recs]
assert sizes[0] == sizes[2] and sizes[1] == sizes[3], f"payload not verbatim: {sizes}"
print("local smoke OK: 4/4 forwarded, auth + ingestion headers correct, payload verbatim")
PY