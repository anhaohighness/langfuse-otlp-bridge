#!/usr/bin/env python3
"""M1 透传转发服务（Langfuse 桥接层的临时形态，决策 D9）。

依据《Langfuse 接入 · 最终交付方案》(2026-11-24) 第 02 节 callout 与第 09 节：
M1 用普通 HTTP 转发服务原样透传 OTLP，只注入 Basic 鉴权与 ingestion 头；
M2 起换 OpenTelemetry Collector（config/collector-langfuse.yaml）做 R1/R4 等
字段改写——Android 发的是 protobuf，普通转发服务改不了这种 body。

本服务不解析、不改写载荷，不持业务状态，不落盘；日志只含计数与错误码，
不含用户内容（方案表 4-5）。仅用 Python 标准库，无第三方依赖。

用法：
    export LANGFUSE_TRACES_ENDPOINT=https://<langfuse-host>/api/public/otel/v1/traces
    export LANGFUSE_AUTHORIZATION="Basic $(printf '%s' "$LF_PUBLIC_KEY:$LF_SECRET_KEY" | base64 -w0)"
    python3 m1/forwarder.py
    BRIDGE_TRACES_URL=http://localhost:4318/v1/traces bash probes/otlp-probe.sh --repeat

环境变量（见 m1/bridge.env.example）：
    LANGFUSE_TRACES_ENDPOINT   必填，完整 traces 端点，缺配置即失败、不回退。
    LANGFUSE_AUTHORIZATION     鉴权二选一：Basic base64(publicKey:secretKey)。
    LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY  鉴权二选一：密钥对，自动拼 Basic。
    BRIDGE_LISTEN_HOST/PORT    默认 0.0.0.0:4318，与 Collector 一致，探针命令不变。
    BRIDGE_INBOUND_TOKEN       可选；设置后入站必须带 Authorization: Bearer <token>。
    BRIDGE_MAX_RETRIES         默认 3，仅对 429/5xx/超时重试（指数退避 + 抖动）。
    BRIDGE_TIMEOUT_SECONDS     默认 30。

失败处理（方案表 4-5）：
    - 429/5xx/超时：有界重试，耗尽后向调用方返回 502 并计数。
    - 400/401/403/413：不重试，记录状态与字节数并原样回传状态码。
    - 入站鉴权失败 401；路径不符 404；空体 400；超 20 MiB 413。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

LOG = logging.getLogger("bridge-m1")

MAX_BODY_BYTES = 20 * 1024 * 1024
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class Config:
    """启动时一次性读取环境；缺关键配置即退出，不做跨区回退。"""

    def __init__(self) -> None:
        self.endpoint = os.environ.get("LANGFUSE_TRACES_ENDPOINT", "").strip()
        auth = os.environ.get("LANGFUSE_AUTHORIZATION", "").strip()
        if not auth:
            pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
            sk = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
            if pk and sk:
                auth = "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self.authorization = auth
        self.listen_host = os.environ.get("BRIDGE_LISTEN_HOST", "0.0.0.0")
        self.listen_port = int(os.environ.get("BRIDGE_LISTEN_PORT", "4318"))
        self.inbound_token = os.environ.get("BRIDGE_INBOUND_TOKEN", "").strip()
        self.max_retries = max(0, int(os.environ.get("BRIDGE_MAX_RETRIES", "3")))
        self.timeout = float(os.environ.get("BRIDGE_TIMEOUT_SECONDS", "30"))
        if not self.endpoint or not self.authorization:
            raise SystemExit(
                "缺少 LANGFUSE_TRACES_ENDPOINT 或 LANGFUSE_AUTHORIZATION"
                "（或 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY 密钥对），拒绝启动。"
            )


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"received": 0, "forwarded": 0, "failed": 0, "rejected": 0}

    def bump(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def forward(cfg: Config, body: bytes, content_type: str, content_encoding: str | None) -> tuple[int, str]:
    """原样转发载荷，注入 Langfuse 鉴权与 ingestion 头；返回 (状态码, 说明)。

    入站的 Authorization（设备/APIM 凭据）一律不透传，只注入服务端密钥。
    """
    headers = {
        "Content-Type": content_type,
        "Authorization": cfg.authorization,
        "x-langfuse-ingestion-version": "4",
    }
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    delay = 1.0
    detail = "unknown"
    for attempt in range(cfg.max_retries + 1):
        req = request.Request(cfg.endpoint, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=cfg.timeout) as resp:
                resp.read()
                return resp.status, ""
        except error.HTTPError as exc:
            detail = f"upstream HTTP {exc.code}"
            if exc.code not in RETRYABLE_STATUS:
                return exc.code, detail + "（不重试）"
        except (error.URLError, TimeoutError, OSError) as exc:
            detail = f"upstream unreachable: {exc.__class__.__name__}"
        if attempt < cfg.max_retries:
            time.sleep(min(delay, 30.0) + random.uniform(0, 0.5))
            delay *= 2
    return 502, detail + "（重试耗尽）"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SuuntoLangfuseBridgeM1/0.1"
    cfg: Config
    stats: Stats

    def do_GET(self) -> None:  # noqa: N802（http.server 约定）
        if self.path == "/healthz":
            payload = json.dumps({"status": "ok", **self.stats.snapshot()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/traces":
            self.send_error(404)
            return
        self.stats.bump("received")
        if self.cfg.inbound_token:
            supplied = self.headers.get("Authorization", "")
            if supplied != f"Bearer {self.cfg.inbound_token}":
                self.stats.bump("rejected")
                LOG.warning("inbound auth rejected bytes=%s", self.headers.get("Content-Length", "?"))
                self.send_error(401)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0:
            self.stats.bump("rejected")
            self.send_error(400)
            return
        if length > MAX_BODY_BYTES:
            self.stats.bump("rejected")
            self.send_error(413)
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "application/x-protobuf")
        status, detail = forward(self.cfg, body, content_type, self.headers.get("Content-Encoding"))
        if 200 <= status < 300:
            self.stats.bump("forwarded")
            # OTLP 成功响应：protobuf 为空消息，JSON 为 {}。
            resp_body = b"{}" if "json" in content_type else b""
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        else:
            self.stats.bump("failed")
            LOG.warning("forward failed status=%s bytes=%d detail=%s", status, length, detail)
            self.send_error(status if 400 <= status < 600 else 502)

    def log_message(self, fmt: str, *args: object) -> None:
        # 只记录请求行与状态码，不打印载荷。
        LOG.info("%s", fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config()
    stats = Stats()
    Handler.cfg = cfg
    Handler.stats = stats
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), Handler)
    server.daemon_threads = True

    def shutdown(signum: int, frame: object) -> None:
        LOG.info("shutting down (signal %s)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("M1 passthrough listening on %s:%d -> %s", cfg.listen_host, cfg.listen_port, cfg.endpoint)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        LOG.info("final stats: %s", stats.snapshot())


if __name__ == "__main__":
    main()