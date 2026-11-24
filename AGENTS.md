# 仓库开发约束

本仓库是 Langfuse OTLP 桥接层（《Langfuse 接入 · 最终交付方案》2026-11-24 的"范围 B"），
独立仓库、单独发包，与 Suunto-Agent（云端业务服务）和 suunto-ai-sdk（设备侧 SDK）零代码共享。

AGENTS.md 与 CLAUDE.md 的全部内容必须完全一致。背景、验证记录与待办见 CONTEXT.md。

- **职责边界**

  - 只做中立 OTLP 的接收、改写（R1–R8）与送达；不解析业务语义、不持业务状态、不落盘载荷。
  - 本仓库是唯一持有 Langfuse 密钥、唯一写 `langfuse.*` 字段的地方；密钥来自受控配置，从不下发设备。
  - 不改写 trace/span ID（反馈链路用真实 ID 定位）；不重建客户端省略的内容。
  - 区域绑定：缺配置即失败，不跨区回退；中国区与自建 Langfuse 4.x 同区部署。

- **契约与规则**

  - 入站 OTLP/HTTP，protobuf 与 JSON 同一端点 `/v1/traces`；不开 gRPC。
  - 出站带 `Authorization: Basic` 与 `x-langfuse-ingestion-version: 4`；压缩默认关闭。
  - R4 只对 `suunto.usage.scope = turn_total` 去重；宿主未实现 `ProviderRequestTraceBridge`（决策 D1）前不得启用，否则整轮 token 消失。
  - 日志只含计数与错误码，不含用户内容。

- **形态与发布（决策 D9）**

  - M1：`m1/forwarder.py` 透传（临时形态）；M2：OpenTelemetry Collector（`config/collector-langfuse.yaml`，目标形态）。
  - 独立发包：`./deploy/build.sh`（冒烟闸门 + M1/M2 镜像）；`REGISTRY=...` 时推送；版本见 `VERSION`。
  - 不接入 Suunto-Agent 的 Jenkins / release.py 整栈发布；回滚在 APIM 关闭 Langfuse 通道，设备无需发版。

- **检查与交付**

  - 改动后运行 `bash probes/local-smoke.sh`；涉及 Collector 配置时用 M2 镜像执行 `validate --config=/etc/otelcol-contrib/config.yaml`。
  - 真实实例验收按方案 M0 与第 07 节执行；本地通过不等于生产验收，如实报告未验证项。
  - 权威方案（本机路径）：`D:\8004\Desktop\AIGC\Koog改造LF\00_2026-11-24_最终交付方案\index.html`，物料在其 `materials/`。