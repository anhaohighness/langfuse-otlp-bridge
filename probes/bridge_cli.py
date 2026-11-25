#!/usr/bin/env python3
"""桥接层调试 CLI：样例 OTLP 上报（JSON / protobuf 双编码）+ 健康检查 + 样例导出。

与权威探针 otlp-probe.sh 互补：探针用于验收核对，本 CLI 用于日常调试——
可选场景（正常回合 / 取消 / 失败 / 记忆抽取）、可选编码（iOS 走 JSON，
Android/JVM 走 protobuf）、chunked 与查询串入站、--repeat 幂等重放。
仅 Python 标准库；protobuf 按 opentelemetry-proto trace/v1 字段号手工编码。

用法：
    python3 probes/bridge_cli.py health
    python3 probes/bridge_cli.py send                          # 正常回合，JSON
    python3 probes/bridge_cli.py send --encoding proto         # Android 同款 protobuf
    python3 probes/bridge_cli.py send --scenario all --repeat 2
    python3 probes/bridge_cli.py send --chunked --query "api-version=2026-01-01"
    python3 probes/bridge_cli.py samples --out-dir /tmp/lf-samples   # 导出样例给 curl 重放

环境变量（与 otlp-probe.sh 一致）：
    BRIDGE_TRACES_URL   默认 http://localhost:4318/v1/traces
    BRIDGE_AUTH_HEADER / BRIDGE_AUTH_VALUE   可选入站鉴权头

发送后打印 trace ID，拿它到 Langfuse 里按 service "bridge-cli" 检索。
场景与期望：
    turn       chat_turn(chain) → coach.run(agent) → chat(generation) + execute_tool(tool)
               根带 turn_total 用量（R4 应搬进 metadata 后删除）
    cancelled  同 turn 树，error.type=cancelled（R5 应降级 WARNING）
    failure    同 turn 树，error.type=provider_error（保持 ERROR）
    memory     memory.extract 独立根，scope=memory_extraction（R4 不得误删其用量）
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import struct
import sys
import time
import urllib.parse

DEFAULT_URL = os.environ.get("BRIDGE_TRACES_URL", "http://localhost:4318/v1/traces")
SERVICE = "bridge-cli"
SESSION = "cli-session"
TAGS = ["suunto-ai-sdk", "jvm"]
SCENARIOS = ("turn", "cancelled", "failure", "memory", "all")


# ── 最小 OTLP protobuf 编码（字段号见 opentelemetry/proto/trace/v1/trace.proto）──

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:  # length-delimited
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _vfield(field: int, n: int) -> bytes:  # varint 字段
    return _varint((field << 3) | 0) + _varint(n)


def _f64(field: int, n: int) -> bytes:  # fixed64 字段
    return _varint((field << 3) | 1) + struct.pack("<Q", n)


def _any_value(v: object) -> bytes:
    if isinstance(v, bool):
        return _vfield(2, int(v))
    if isinstance(v, int):
        return _vfield(3, v)
    if isinstance(v, float):
        return _varint((4 << 3) | 1) + struct.pack("<d", v)
    if isinstance(v, str):
        return _ld(1, v.encode())
    if isinstance(v, list):
        return _ld(5, b"".join(_ld(1, _any_value(x)) for x in v))
    raise TypeError(f"unsupported attribute value: {type(v)}")


def _kv(key: str, value: object) -> bytes:
    return _ld(1, key.encode()) + _ld(2, _any_value(value))


def _span_proto(sp: dict) -> bytes:
    out = _ld(1, bytes.fromhex(sp["trace_id"])) + _ld(2, bytes.fromhex(sp["span_id"]))
    if sp["parent"]:
        out += _ld(4, bytes.fromhex(sp["parent"]))
    out += _ld(5, sp["name"].encode()) + _vfield(6, 1)  # kind = INTERNAL
    out += _f64(7, sp["start_ns"]) + _f64(8, sp["end_ns"])
    out += b"".join(_ld(9, _kv(k, v)) for k, v in sp["attrs"])
    if sp["status"]:
        msg, code = sp["status"]
        out += _ld(15, _ld(2, msg.encode()) + _vfield(3, code))
    return out


def request_proto(traces: list[dict]) -> bytes:
    out = b""
    for tr in traces:
        resource = _ld(1, b"".join(_ld(1, _kv(k, v)) for k, v in tr["resource"]))
        scope = _ld(1, _ld(1, b"com.suunto.ai.bridge-cli"))
        spans = b"".join(_ld(2, _span_proto(sp)) for sp in tr["spans"])
        out += _ld(1, resource + _ld(2, scope + spans))
    return out


# ── OTLP JSON 编码（与 otlp-probe.sh 同一形状）──────────────────────────────

def _any_json(v: object) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_any_json(x) for x in v]}}
    raise TypeError(f"unsupported attribute value: {type(v)}")


def request_json(traces: list[dict]) -> bytes:
    rs = []
    for tr in traces:
        spans = []
        for sp in tr["spans"]:
            j = {
                "traceId": sp["trace_id"], "spanId": sp["span_id"], "name": sp["name"],
                "kind": 1,
                "startTimeUnixNano": str(sp["start_ns"]), "endTimeUnixNano": str(sp["end_ns"]),
                "attributes": [{"key": k, "value": _any_json(v)} for k, v in sp["attrs"]],
            }
            if sp["parent"]:
                j["parentSpanId"] = sp["parent"]
            if sp["status"]:
                j["status"] = {"message": sp["status"][0], "code": sp["status"][1]}
            spans.append(j)
        rs.append({
            "resource": {"attributes": [{"key": k, "value": _any_json(v)} for k, v in tr["resource"]]},
            "scopeSpans": [{"scope": {"name": "com.suunto.ai.bridge-cli"}, "spans": spans}],
        })
    return json.dumps({"resourceSpans": rs}, ensure_ascii=False).encode()


# ── 样例场景（合成占位内容，不调模型、不含用户数据）─────────────────────────

def _hex(n: int) -> str:
    return os.urandom(n).hex()


def build_scenarios(names: list[str]) -> list[dict]:
    if "all" in names:
        names = ["turn", "cancelled", "failure", "memory"]
    t0 = time.time_ns()
    common = [("session.id", SESSION), ("user.id", "cli-user"),
              ("gen_ai.conversation.id", SESSION), ("suunto.trace.tags", TAGS)]
    resource = [("service.name", SERVICE), ("service.version", "0.0.0-cli"),
                ("deployment.environment.name", "dev")]
    traces = []

    def span(tr, sid, parent, name, s_ms, e_ms, attrs, status=None):
        return {"trace_id": tr, "span_id": sid, "parent": parent, "name": name,
                "start_ns": t0 + s_ms * 1_000_000, "end_ns": t0 + e_ms * 1_000_000,
                "attrs": common + attrs, "status": status}

    for name in names:
        tr, root, run, req, tool = _hex(16), _hex(8), _hex(8), _hex(8), _hex(8)
        if name == "memory":
            spans = [span(tr, root, "", "memory.extract", 1000, 1300, [
                ("gen_ai.operation.name", "chat"), ("gen_ai.request.model", "probe-model"),
                ("suunto.usage.scope", "memory_extraction"),
                ("gen_ai.usage.input_tokens", 12), ("gen_ai.usage.output_tokens", 3)])]
        else:
            status = None
            if name == "cancelled":
                status = ("cancelled", 2)
            elif name == "failure":
                status = ("provider_error", 2)
            err = [("error.type", "cancelled" if name == "cancelled" else "provider_error")] if status else []
            spans = [
                span(tr, root, "", "chat_turn", 0, 900, [
                    ("gen_ai.operation.name", "invoke_workflow"), ("gen_ai.workflow.name", "chat_turn"),
                    ("suunto.usage.scope", "turn_total"),
                    ("gen_ai.usage.input_tokens", 30), ("gen_ai.usage.output_tokens", 6),
                    ("gen_ai.usage.cache_read.input_tokens", 10)] + err, status),
                span(tr, run, root, "coach.run", 10, 880, [
                    ("gen_ai.operation.name", "invoke_workflow"),
                    ("suunto.agent.loop.strategy", "native"), ("suunto.agent.loop.exit", "no_tool_calls"),
                    ("suunto.agent.loop.tool_rounds", 1), ("suunto.agent.loop.reused_tool_results", 0)] + err, status),
                span(tr, req, run, "chat probe-model", 20, 400, [
                    ("gen_ai.operation.name", "chat"), ("gen_ai.request.model", "probe-model"),
                    ("suunto.usage.scope", "provider_request"), ("suunto.request.phase", "native"),
                    ("gen_ai.usage.input_tokens", 30), ("gen_ai.usage.output_tokens", 6),
                    ("gen_ai.usage.cache_read.input_tokens", 10)]),
                span(tr, tool, run, "execute_tool tool_probe", 420, 500, [
                    ("gen_ai.operation.name", "execute_tool"), ("gen_ai.tool.name", "tool_probe"),
                    ("gen_ai.tool.call.arguments", "{}"), ("gen_ai.tool.call.result", "{}")]),
            ]
        traces.append({"scenario": name, "trace_id": tr, "resource": resource, "spans": spans})
    return traces


# ── HTTP 发送与健康检查 ─────────────────────────────────────────────────────

def _connect(u: urllib.parse.SplitResult, timeout: float, insecure: bool):
    if u.scheme == "https":
        ctx = ssl._create_unverified_context() if insecure else None
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def post(url: str, body: bytes, content_type: str, auth: tuple[str, str] | None,
         chunked: bool, timeout: float, insecure: bool) -> tuple[int, bytes, float]:
    u = urllib.parse.urlsplit(url)
    headers = {"Content-Type": content_type, "Accept": content_type}
    if auth and auth[0]:
        headers[auth[0]] = auth[1]
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    conn = _connect(u, timeout, insecure)
    start = time.monotonic()
    try:
        conn.request("POST", path, body=body, headers=headers, encode_chunked=chunked)
        resp = conn.getresponse()
        return resp.status, resp.read(), time.monotonic() - start
    finally:
        conn.close()


def cmd_send(args: argparse.Namespace) -> int:
    url = args.url
    if args.query:
        url += ("&" if urllib.parse.urlsplit(url).query else "?") + args.query
    auth = (args.auth_header or os.environ.get("BRIDGE_AUTH_HEADER", ""),
            args.auth_value or os.environ.get("BRIDGE_AUTH_VALUE", ""))
    traces = build_scenarios([args.scenario])
    ctype = "application/x-protobuf" if args.encoding == "proto" else "application/json"
    encode = request_proto if args.encoding == "proto" else request_json
    print(f"→ POST {url}  encoding={args.encoding} chunked={args.chunked}")
    ok = True
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"  第 {i + 1}/{args.repeat} 轮发送（同一批 ID，幂等重放）")
        for tr in traces:
            body = encode([tr])
            try:
                status, resp, elapsed = post(url, body, ctype, auth, args.chunked, args.timeout, args.insecure)
            except OSError as exc:
                print(f"  scenario={tr['scenario']} 连接失败: {exc.__class__.__name__}: {exc}")
                return 1
            snippet = resp[:200].decode(errors="replace")
            print(f"  scenario={tr['scenario']} trace_id={tr['trace_id']} bytes={len(body)}"
                  f" → HTTP {status} ({elapsed * 1000:.0f} ms) {snippet}")
            ok = ok and 200 <= status < 300
    print("2xx 只代表桥接层收下了；到 Langfuse 按 service bridge-cli 与上面的 trace_id 核对渲染。")
    return 0 if ok else 1


def cmd_health(args: argparse.Namespace) -> int:
    u = urllib.parse.urlsplit(args.url)
    base = f"{u.scheme}://{u.netloc}/healthz"
    conn = _connect(urllib.parse.urlsplit(base), args.timeout, args.insecure)
    try:
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        print(f"GET {base} → HTTP {resp.status} {resp.read().decode(errors='replace')}")
        return 0 if resp.status == 200 else 1
    except OSError as exc:
        print(f"连接失败: {exc}")
        return 1
    finally:
        conn.close()


def cmd_samples(args: argparse.Namespace) -> int:
    os.makedirs(args.out_dir, exist_ok=True)
    for tr in build_scenarios(["all"]):
        path = os.path.join(args.out_dir, f"{tr['scenario']}.json")
        with open(path, "wb") as f:
            f.write(request_json([tr]))
        print(f"→ {path}  trace_id={tr['trace_id']}")
    print("重放示例：curl -X POST <bridge>/v1/traces -H 'Content-Type: application/json' --data-binary @turn.json")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Langfuse 桥接层调试 CLI（样例 OTLP 上报）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="发送样例 trace")
    s.add_argument("--url", default=DEFAULT_URL)
    s.add_argument("--encoding", choices=("json", "proto"), default="json")
    s.add_argument("--scenario", choices=SCENARIOS, default="turn")
    s.add_argument("--repeat", type=int, default=1, help="同一批 ID 重放次数（幂等调试）")
    s.add_argument("--chunked", action="store_true", help="用 chunked 传输编码发送")
    s.add_argument("--query", default="", help="附加查询串，如 api-version=2026-01-01")
    s.add_argument("--auth-header", default="")
    s.add_argument("--auth-value", default="")
    s.add_argument("--timeout", type=float, default=30.0)
    s.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（自签 dev 实例）")
    s.set_defaults(fn=cmd_send)

    h = sub.add_parser("health", help="GET /healthz（M1 计数；M2 请用 13133 端口）")
    h.add_argument("--url", default=DEFAULT_URL)
    h.add_argument("--timeout", type=float, default=10.0)
    h.add_argument("--insecure", action="store_true")
    h.set_defaults(fn=cmd_health)

    d = sub.add_parser("samples", help="导出四个场景的 JSON 样例文件，供 curl 重放")
    d.add_argument("--out-dir", default="lf-samples")
    d.set_defaults(fn=cmd_samples)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()