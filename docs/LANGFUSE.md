# Langfuse 接入与学习指南

更新日期：2026-10-25。

ScholarRAG 通过可选的 Langfuse Python SDK 导出问答追踪。现有 Query Run、`events.jsonl`、SSE 和本地历史仍然是业务状态与恢复的依据；Langfuse 用于分析调用树、耗时和模型用量。

本次实现使用 `langfuse>=4.7,<5`，锁文件解析为 4.18.0，采用 OpenTelemetry 上下文与 `start_as_current_observation()`，没有使用旧版 `trace()/generation()` API。选择 SDK v4 是为了兼容 Cloud 以及部分仍运行服务端 v3 的自部署环境；官方最新文档还包含 SDK v5，后续升级应对照迁移指南和服务端兼容矩阵，不能混用不同版本的示例。

## 1. 先理解四个概念

| 概念 | 在 ScholarRAG 中的映射 | 用途 |
| --- | --- | --- |
| Trace | 一次异步 Query Run、同步问答或 CLI ask | 查看一次请求的整体执行过程 |
| Observation / Span | 检索、融合、重排、上下文和验证阶段 | 定位耗时或失败阶段 |
| Generation | 一次实际生成模型调用，包括流式调用 | 查看模型、Token 用量、首个有效输出时间和结果 |
| Session | `conversation_id` | 将同一会话中的多次问答归组 |

一个 Trace 可以有多个 Generation；工具 Agent 的一次回答也可能调用模型多次。每次供应商流式请求只生成一条 Generation，SDK 上下文在 `asyncio` 并行检索时保留父子关系。

`trace_id` 与已有问答返回结果中的 `trace_id` 相同。异步 Run 另外在根 Observation metadata 中记录 `run_id`。可以复制 trace_id 到 Langfuse 检索，关联本地事件与远端追踪。

阶段的 `parent_stage_id` 会作为 metadata 保存；Langfuse 树的实际父子关系由运行时上下文决定，不是简单按点分隔的阶段名称重建。工具调用拥有独立 `tool` Observation。

个人模式不把客户端自报 ID 当作 user_id。已接通的工作空间问答使用服务端认证归属传播 user_id 与 workspace_id；这类遥测字段不构成业务授权或 Langfuse 项目内的数据隔离。实现边界见 [执行授权记录](implementation/A10_EXECUTION_AND_STREAM_AUTH.md)。

## 2. Cloud 费用与准备

截至更新日期，[官方价格页](https://langfuse.com/pricing) 列出免费 Hobby 计划：无需信用卡，每月 50,000 units，30 天历史数据访问，2 名平台成员。Units 不是模型 Token，也不能直接等同于问答次数；一轮 Agent 可能产生多个 Observation。以官方当时的计量说明和控制台用量为准。

硅基流动生成、Embedding、重排、OCR 和 LLM Judge 的费用独立于 Langfuse。免费平台额度不包含这些模型调用费用。Langfuse 的成员数指可登录其控制台的成员，不是 ScholarRAG 最终用户数。

在 Langfuse Cloud 创建项目，在项目设置中取得 Public Key、Secret Key 和对应区域的 Base URL。选择区域后复制控制台地址，不要假设不同区域的项目 Key 可配合任意地址使用。

不需要将真实 Key 发送到聊天或提交到仓库，填写本地 `.env` 即可。

## 3. 安装与配置

安装可选依赖：

```bash
uv sync --extra dev --extra observability
```

如果同时需要评测或 Paddle，保留相应 `--extra eval`、`--extra paddle`。Docker 镜像已包含 observability extra，但仍需显式启用导出。

在 `.env` 中填写：

```dotenv
SCHOLARRAG_LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-替换为项目PublicKey
LANGFUSE_SECRET_KEY=sk-lf-替换为项目SecretKey
SCHOLARRAG_LANGFUSE_ENVIRONMENT=development
SCHOLARRAG_LANGFUSE_CAPTURE_CONTENT=false
```

Base URL 使用项目控制台提供的实际值；这里仅展示默认值。自部署也使用相同配置入口，但需要根据官方兼容说明确认服务端版本。本次未搭建自部署 Langfuse。

检查配置，不打印 Key：

```bash
uv run --extra observability scholarrag check-config
```

`langfuse_configured` 只表示两项凭据存在，不代表网络和认证已通过。显式开启但缺少凭据或 SDK 时会提示配置错误；默认关闭时无需安装 SDK，也不会创建 Langfuse 客户端。

## 4. 第一次实验：无需论文和模型调用

```bash
uv run --extra observability scholarrag langfuse-smoke
```

命令验证 Langfuse 认证，然后导出名为 `scholarrag.smoke` 的合成 Trace，包含 `example.retrieval` 和 `example.generation` 两个子 Observation。命令不访问论文，不调用硅基流动，也不模拟真实模型 Token 或费用。

成功输出包括 `authenticated`、`trace_id` 和 `synthetic=true`。在 Langfuse 控制台按 trace_id 查找，观察根节点与子节点、模型类型和 Session。合成模型名为 `synthetic`，不要把该 Trace 当作真实性能基准。

数据后台批量发送；flush 结束与控制台可检索之间可能有延迟。命令认证成功也不等于每条追踪已在界面展示，仍需检查 Trace。SDK 导出错误会记录日志，且不会触发论文或模型请求重试。

## 5. 第二次实验：真实问答

启动 API：

```bash
uv run --extra observability uvicorn scholarrag.api.main:app --reload --host 127.0.0.1 --port 8000
```

按 README 启动 Web、上传自己的论文并提问。也可以使用 CLI：

```bash
uv run --extra observability scholarrag ask "这篇论文的方法与局限是什么？" -d <document-id>
```

建议依次学习：

1. 单轮检索：识别检索、重排、生成和引用阶段，查看真正耗时的环节。
2. 工具 Agent：查看多个 `tool.*` 和 Generation，理解一次回答与一次模型调用的区别。
3. 追问：在 Web 同一会话中继续提问，观察 Session 下出现多条 Trace。
4. 重复问题：比较缓存前后的 Embedding 请求；全部命中缓存时没有实际 Embedding Observation，检索阶段仍存在。
5. 失败与取消：结合本地事件确认状态；Generation 记录异常类型，异步根节点显示最终失败或取消。

Token 用量来自供应商响应，规范化为 input/output；流式响应结束时才可能出现最终用量。取消或供应商未返回 usage 时不能解释为零消耗。首个有效输出时间包含 reasoning/tool-call delta 的首次出现，不等同于用户可见回答的首字时间。

Langfuse 是否能自动估算特定 Qwen 模型费用取决于其模型价格配置。项目现有 CNY 配置没有直接写入 Langfuse cost_details，避免币种误用；没有配置匹配价格时不要把空费用当作免费。

## 6. 正文采集与隐私边界

默认 `SCHOLARRAG_LANGFUSE_CAPTURE_CONTENT=false`：不导出 Observation input/output，也不导出原始错误正文。仍导出阶段名、模型、耗时、状态、Token 用量以及 run_id、conversation_id 等关联信息。

如果希望学习 Prompt 与回答调试，可对自己允许上传的测试论文启用：

```dotenv
SCHOLARRAG_LANGFUSE_CAPTURE_CONTENT=true
```

重启 API 后，问题、回答、模型消息、工具结果和阶段上下文可能进入 Langfuse。此时可能含论文原文，须理解所选 Cloud 区域和数据保留政策。

本实现不导出 PDF、图片文件、Base64 图片、模型原始思考字段或 Authorization Header；对正文进行长度和深度限制，并过滤媒体、路径及常见凭据字段。该过滤是有限的工程防护，不是通用 DLP，无法识别正文中任意形式的秘密或个人信息。

关闭后仅影响新的导出，不删除已上传数据。不要通过开启 SDK debug 日志来处理真实敏感论文。

## 7. 实现入口与生命周期

- `src/scholarrag/observability.py`：可选 SDK 初始化、上下文隔离、内容开关、错误隔离、用量规范化。
- `src/scholarrag/rag/runs.py`：现有 PipelineTracer 阶段导出 Span，本地事件逻辑保留。
- `src/scholarrag/providers/siliconflow.py`：生成、流式生成、实际 Embedding 和重排调用。HTTP 重试包含在同一请求 Observation 中；首版未拆成逐次重试 Span。
- `src/scholarrag/rag/tool_agent.py`：工具执行 Observation。
- `src/scholarrag/api/main.py`：异步 Query Run 与同步 `/api/query` 根 Trace；服务退出时在线程中 shutdown。
- `src/scholarrag/cli.py`：`ask` 根 Trace 和 `langfuse-smoke`；命令退出前 shutdown，避免短进程丢失缓冲数据。

首版覆盖上述问答入口；摄取、独立评测与其他 CLI 命令尚未建立根 Trace，未处于追踪上下文的 Provider 调用不会自动导出。没有接入 Langfuse Prompt Management、Dataset、Score 或自动 Judge，也不会把现有评测集批量上传。

业务异常与取消继续传播或按原逻辑持久化；运行期遥测初始化、update、导出或关闭失败不应使问答重复执行。进程被强制终止时仍可能丢失尚未导出的遥测，本地持久化事件用于补充审计。

## 8. 验证与排查

测试使用真实 SDK 与内存 OTel exporter，验证父子关系、并行请求隔离、流式用量、默认内容关闭、媒体过滤、异常和取消；不需要 Langfuse Key，也不向 Cloud 上传。

```bash
uv run --extra dev --extra observability pytest tests/test_observability.py -q
```

找不到 Trace 时依次检查：是否启用、是否安装 extra、Key 是否属于当前项目、Base URL 区域、网络出口、后台批量发送和控制台时间范围。短 CLI 必须等待正常退出，服务正常关闭时等待 shutdown。

首次真实接通仍需在用户自己的项目执行 smoke 并查看控制台；本地测试通过不构成 Cloud 端到端验证。

## 9. 后续学习方向

先掌握 Trace/Span/Generation/Session 和耗时分析，再将人工审核或 Ragas 的结果以 Score 关联原始 Trace；保持指标版本、样本来源与人工复核状态。随后学习 Prompt 版本管理和 Dataset 实验，对模型、Prompt、Agent 和视觉输入做受控对照。

多用户阶段从已认证主体传播身份和工作空间，增加采样、环境拆分、保留与脱敏策略。不要让应用普通用户通过 Langfuse 项目直接访问其他用户 Trace。

参考：[SDK 概览与兼容性](https://langfuse.com/docs/observability/sdk/overview)、[手动埋点](https://langfuse.com/docs/observability/sdk/instrumentation)、[Python v3→v4 迁移](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4)、[官方价格](https://langfuse.com/pricing)。
