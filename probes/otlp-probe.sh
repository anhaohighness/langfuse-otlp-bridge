#!/usr/bin/env bash
# OTLP 探针：向桥接层发两条最小 trace，验证接收、R1/R3/R4/R6 改写与幂等。
# 不调模型、不读数据库，只发送固定占位内容。
#
# 用法：
#   ./otlp-probe.sh              # 新 trace ID，各发一次
#   ./otlp-probe.sh --repeat     # 同一批 ID 再发一次，验证 Langfuse 不重复成树
#
# 环境变量：
#   BRIDGE_TRACES_URL   桥接层 traces 端点（默认 http://localhost:4318/v1/traces）
#   BRIDGE_AUTH_HEADER  网关或桥接层要求的鉴权头名（可选）
#   BRIDGE_AUTH_VALUE   对应的值（可选）
#
# 发送内容：
#   trace A：chat_turn（根，turn_total 用量 + 标签）
#            ├ coach.run（invoke_workflow + suunto.agent.loop.*）
#            │  ├ chat probe-model（请求级用量 + suunto.request.phase）
#            │  └ execute_tool tool_probe
#   trace B：memory.extract（独立根，LLM span，自带用量，scope=memory_extraction）
#
# 期望（在 Langfuse 里人工核对，按 service "otlp-probe" 查）：
#   1. 两条 trace；--repeat 之后仍是两条。
#   2. chat_turn = chain，coach.run = agent，chat probe-model = generation，
#      execute_tool = tool（R1 与自动推断）。
#   3. chat_turn 上没有 gen_ai.usage.*，metadata 里有 turn_total_input_tokens=30
#      等三项（R4）；trace 总 token = 30 / 6，不是 60 / 12。
#   4. memory.extract 的用量保留（12 / 3）：R4 不能误删非 turn_total 的根。
#   5. trace 标签含 otlp-sample 与 jvm（R3）；coach.run 的 metadata 有 loop_exit，
#      generation 的 metadata 有 request_phase（R6）。
#   6. 两条 trace 的 session 相同：probe-session。

set -euo pipefail

URL="${BRIDGE_TRACES_URL:-http://localhost:4318/v1/traces}"

hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

TRACE_A="$(hex 16)"; TRACE_B="$(hex 16)"
ROOT="$(hex 8)"; RUN="$(hex 8)"; REQ="$(hex 8)"; TOOL="$(hex 8)"; MEM="$(hex 8)"

T0="$(date +%s%N)"
t() { echo $((T0 + $1 * 1000000)); }   # 毫秒偏移 → 纳秒时间戳

s() { printf '{"key":"%s","value":{"stringValue":"%s"}}' "$1" "$2"; }
i() { printf '{"key":"%s","value":{"intValue":"%s"}}' "$1" "$2"; }

COMMON="$(s session.id probe-session),$(s user.id probe-user),$(s gen_ai.conversation.id probe-session),\
{\"key\":\"suunto.trace.tags\",\"value\":{\"arrayValue\":{\"values\":[{\"stringValue\":\"otlp-sample\"},{\"stringValue\":\"jvm\"}]}}}"

span() { # id parent name start_ms end_ms attrs
  local parent=""
  [[ -n "$2" ]] && parent="\"parentSpanId\":\"$2\","
  printf '{"traceId":"%s","spanId":"%s",%s"name":"%s","kind":1,"startTimeUnixNano":"%s","endTimeUnixNano":"%s","attributes":[%s]}' \
    "$TRACE" "$1" "$parent" "$3" "$(t "$4")" "$(t "$5")" "$6"
}

payload() {
  local spans="$1"
  cat <<JSON
{"resourceSpans":[{"resource":{"attributes":[$(s service.name otlp-probe),$(s service.version 0.0.0-probe),$(s deployment.environment.name probe)]},
"scopeSpans":[{"scope":{"name":"dev.otlp.bridge.probe"},"spans":[${spans}]}]}]}
JSON
}

TRACE="$TRACE_A"
SPANS_A="$(span "$ROOT" "" chat_turn 0 900 "$COMMON,$(s gen_ai.operation.name invoke_workflow),$(s gen_ai.workflow.name chat_turn),$(s suunto.usage.scope turn_total),$(i gen_ai.usage.input_tokens 30),$(i gen_ai.usage.output_tokens 6),$(i gen_ai.usage.cache_read.input_tokens 10)"),\
$(span "$RUN" "$ROOT" coach.run 10 880 "$COMMON,$(s gen_ai.operation.name invoke_workflow),$(s gen_ai.agent.id probe-agent),$(s suunto.agent.loop.strategy native),$(s suunto.agent.loop.exit no_tool_calls),$(s suunto.agent.loop.tool_rounds 1),$(s suunto.agent.loop.reused_tool_results 0)"),\
$(span "$REQ" "$RUN" "chat probe-model" 20 400 "$COMMON,$(s gen_ai.operation.name chat),$(s gen_ai.request.model probe-model),$(s suunto.usage.scope provider_request),$(s suunto.request.phase native),$(i gen_ai.usage.input_tokens 30),$(i gen_ai.usage.output_tokens 6),$(i gen_ai.usage.cache_read.input_tokens 10)"),\
$(span "$TOOL" "$RUN" "execute_tool tool_probe" 420 500 "$COMMON,$(s gen_ai.operation.name execute_tool),$(s gen_ai.tool.name tool_probe),$(s gen_ai.tool.call.arguments '{}'),$(s gen_ai.tool.call.result '{}')")"

TRACE="$TRACE_B"
SPANS_B="$(span "$MEM" "" memory.extract 1000 1300 "$COMMON,$(s gen_ai.operation.name chat),$(s gen_ai.request.model probe-model),$(s suunto.usage.scope memory_extraction),$(i gen_ai.usage.input_tokens 12),$(i gen_ai.usage.output_tokens 3)")"

send() {
  local extra=()
  if [[ -n "${BRIDGE_AUTH_HEADER:-}" ]]; then extra=(-H "${BRIDGE_AUTH_HEADER}: ${BRIDGE_AUTH_VALUE:-}"); fi
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$URL" \
    -H "Content-Type: application/json" "${extra[@]}" --data-binary @<(payload "$1")
}

echo "→ POST ${URL}" >&2
echo "  trace A ${TRACE_A}" >&2; send "$SPANS_A"
echo "  trace B ${TRACE_B}" >&2; send "$SPANS_B"
if [[ "${1:-}" == "--repeat" ]]; then
  echo "→ 用同一批 ID 重发（幂等检查）" >&2
  send "$SPANS_A"; send "$SPANS_B"
fi
echo "返回 2xx 只代表桥接层收下了；按脚本头部的 6 条期望到 Langfuse 里逐项核对。" >&2