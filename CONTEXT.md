# 上下文快照（2026-11-24，自 Suunto-Agent 工作区会话迁移）

本文件是搭建本仓库的会话结论存档，供接手者与 IDE 助手快速恢复上下文。
开发约束见 AGENTS.md；权威方案见其末尾的本机路径。

## 背景与已拍板决策

- 链路：设备（Koog SDK）导出中立 OTLP → 已有 APIM（新增 Langfuse 通道）→ 本桥接层 → Langfuse `/api/public/otel/v1/traces`。SDK 不引入厂商依赖，密钥只在服务端。
- **独立仓库、单独发包**（用户 2026-11-24 拍板）：桥接层与 agent/memory/mcp/knowledge 零代码共享；Suunto-Agent 的 release.py 每次 BUILD 构建全部镜像并整栈部署，且 agent 镜像构建上下文为整仓（根 .dockerignore 未排除其他服务目录），集成会互相牵连。独立仓库天然隔离发布。
- 形态按决策 D9：M1 普通 HTTP 透传 → M2 起换 OpenTelemetry Collector（Android 发 protobuf，网关策略改不了 body）。
- 两个前置拍板项：D1 宿主是否实现 `ProviderRequestTraceBridge`（决定 generation/成本，R4 的前提）；D4 中国区自建 Langfuse 4.x 是否就绪（未就绪 +8–15 人日）。

## 已建成（本仓库）

- `config/collector-langfuse.yaml`：方案物料原样纳入（R1/R3–R6 启用，R7/R8 注释备用）。
- `probes/otlp-probe.sh`：权威探针原样纳入（两条 trace + `--repeat` 幂等检查）。
- `m1/forwarder.py`：Python 标准库透传服务——注入 Basic 与 ingestion 头、不透传入站凭据、429/5xx/超时有界重试（指数退避+抖动）、4xx 不重试、缺配置拒启、`/healthz` 计数；默认端口 4318 与 Collector 一致。
- `deploy/`：`Dockerfile.m1`、`Dockerfile.collector`、`build.sh`（独立发包）、`docker-compose.yml`（profile m1/m2 互斥，`BRIDGE_VERSION` 对齐版本）。
- `probes/local-smoke.sh` + `probes/smoke_sink.py`：发布前冒烟闸门（假接收端 + 转发 + 探针 + 断言）。
- `VERSION`：0.1.0；`m1/bridge.env.example`：环境变量模板。

## 验证记录（2026-11-24，本机真实执行）

- 语法：py_compile / bash -n / YAML 解析全部通过。
- 冒烟：4/4 转发，Basic 与 ingestion 头正确，载荷字节级原样；失败路径 404/400/401/502/缺配置拒启均验证。
- 镜像：`suunto-langfuse-bridge-m1:0.1.0`（176MB）、`suunto-langfuse-bridge-m2:0.1.0`（525MB）构建成功。
- Collector 配置：用 M2 镜像内真实 contrib 二进制 `validate` 通过——OTTL 语句从方案的"参考配置、未执行"升级为"已在真实发行版校验"。
- 容器化冒烟：M1 镜像 + sink 容器组网，探针 4×HTTP 200。
- **未验证**：真实 Langfuse 实例（M0）、R1/R4 渲染效果、目标实例 ingestion v4 支持、设备端真实回合。

## 待办（按优先级）

1. 首次 git commit + 创建远端仓库（当前全部文件未提交，git init 已做，分支 main）。
2. `cp m1/bridge.env.example .env` 填 dev 区域端点与密钥 → M0：探针写入真实实例一条 trace，记录实例版本与 ingestion v4 支持。
3. 基础镜像钉版本（当前 `python:3.13-slim` / `otelcol-contrib:latest` 为浮动标签）。
4. APIM 新增 Langfuse 通道（不在本仓库）；跟进 D1/D4 拍板。
5. M2 启用 R4 前确认宿主已实现 `ProviderRequestTraceBridge`；按方案第 07 节验收矩阵逐项核对。

## 本机环境注意（会话中踩过的坑）

- Docker Desktop + WSL：`--network host` 落在 VM 命名空间，WSL 侧访问不到，用 `-p` 发布端口；跨容器测试时接收端须绑 `0.0.0.0`（smoke_sink.py 第 3 参）。
- Qoder 文件工具不能写 Suunto-Agent 工作区之外：经 `.scratch` 暂存 + `cp` 迁移（暂存已清理）。
- WSL 下 shell grep 偶发输出不稳，用 python 检索文件内容替代。

## 关键路径

- 方案与物料：`D:\8004\Desktop\AIGC\Koog改造LF\00_2026-11-24_最终交付方案\`（index.html + materials/，含 SDK 补丁与覆盖矩阵）。
- 设备侧 SDK：`/home/vladimir/suunto-ai-sdk`（分支 feature/langfuse-trace-depth，23 个文件未提交，基线 c3a0a977f）。
- 云端业务：`/home/vladimir/Suunto-Agent`（本仓库不依赖、不改动它；其发布机制见 deploy/swarm/release.py）。