# A21：独立问答 Worker、事务事件、租约与答案检查点

2026-10-27 追加：摄取已由 [A22](A22_DURABLE_INGESTION_WORKER.md) 拆出独立 Worker，并新增租约与供应商/产物/索引检查点。本文中的旧摄取执行和历史迁移说明保留为当时记录，当前开发部署以 A22 为准。

日期：2026-10-27。状态：本地实现与自动回归完成；未提交、未推送、未部署。本轮仅拆出工作空间问答，阶段 C 尚未全部完成。

## 范围与用户可见变化

工作空间问答现在由独立进程执行。API 接收任务、检查权限和排队容量、读取快照/事件及提交取消，不直接运行问答。没有 Worker 时任务保持 queued；API 重启不再把工作空间问答改为失败。默认 local 阅读/问答和现有评测仍由 API 执行；摄取仍由 API 内单个 Worker 执行。

按开发阶段直接采用新存储，不实现旧 `run.json` / `events.jsonl` 问答历史导入，也不增加历史格式双写。现有 JSON 文件不会被新历史接口读取，不自动删除本地旧文件。论文、解析与索引的现有结构不变。本轮没有操作实际运行数据。

## 实现位置与存储

- `runtime.py`：共享服务组装。独立 Worker 不通过导入 `api.main` 创建额外默认应用或数据库。
- `rag/execution.py`：从 API 拆出的问答执行、授权监视和追问上下文。
- `rag/runs.py`：SQLite 快照、事件、租约、取消标记和答案检查点。
- `rag/workspaces.py` / `storage/identity.py`：工作空间准入、归属和持久化容量检查。
- `worker.py`：原子领取、心跳、执行与检查点恢复。
- `cli.py`：`scholarrag worker`；接收 SIGTERM 并停止执行，保留恢复状态。

`scholarrag.db` 新增 `query_run_snapshots`、`query_run_events`。快照以 namespace 区分 local 和工作空间。任务归属记录、容量检查、快照、首条事件与创建审计在同一 SQLite 写事务提交；终态快照和终态事件也在同一事务提交。终态序号不会先发布、后补写快照。

SSE 通过数据库按 seq 轮询，最长每 100ms 重新检查事件，不依赖另一个进程无法通知的 asyncio Condition。现有 Bearer 授权、Last-Event-ID、断线续传与终态对账保持可用。

## 领取、租约和取消

`BEGIN IMMEDIATE` 串行化领取事务。只有已登记归属的工作空间 queued 任务和租约过期的 running 任务可领取。每次领取生成新随机 token，并记录到期时间和 Worker ID。未到期的任务不能被第二个 Worker 领取。

运行时定期续租。事件、快照、检查点和终态的写入均校验 token 与租约有效期；授权 Guard 同时检查租约，覆盖供应商调用前及排队容量释放后的授权边界。旧 Worker 即使恢复执行也不能覆盖新执行者的结果。已经发出的远程请求无法保证撤回。

API 取消 queued 任务会立即提交 cancelled 终态并清除租约；即使 Worker 刚领取也不能再启动它。取消 running 任务先持久化标记，Worker 在心跳、授权或写入检查处停止；HTTP 响应可能暂时仍为 running，随后通过快照/SSE 得到 cancelled。取消与完成由事务先后决定；已提交的终态不会被用户取消改写。

每个工作空间的 queued + running 上限为 `WORKSPACE_QUERY_MAX_ACTIVE`，同一用户在该空间的上限为 `WORKSPACE_QUERY_MAX_PER_USER`。容量取自数据库，多个 API 对同一队列提交时不能绕过限制。单个 Worker 并发数与队列准入容量是两个配置。

## 恢复边界

| 中断点 | 恢复行为 | 是否重新调用模型 |
| --- | --- | --- |
| 入队后尚未开始 | 租约不存在或过期后继续领取 | 首次执行 |
| 已生成答案且检查点已提交，终态未提交 | 重新验证当前会话、空间与文档权限，再将检查点提交为完成 | 否 |
| 已进入 running，但无答案检查点 | 租约过期后标记 failed，说明供应商执行结果可能未知，保留已有事件 | 否 |
| 已完成、失败或取消 | 保留终态，不再领取 | 否 |
| Worker 正常关闭或 SIGTERM | 停止心跳和执行，不冒充用户取消；租约过期后按上述规则恢复 | 不盲目重放 |

检查点仅保存已生成的 Answer，当前没有检索阶段、Agent 中间轮次、供应商请求 ID 或逐 token 检查点。模型已返回但检查点尚未落盘时，仍按结果不确定处理。此实现不能声称付费调用 exactly-once，也不能声称任意阶段无损续跑。

检查点恢复不绕过权限：发起会话过期/撤销、成员移除或文档授权变化都会阻止发布。取消标记同样阻止检查点恢复为成功。

## 启动与配置

本地工作空间开发先设置：

```dotenv
SCHOLARRAG_AUTH_MODE=session
SCHOLARRAG_QDRANT_URL=http://localhost:6333
SCHOLARRAG_QUERY_WORKER_CONCURRENCY=2
SCHOLARRAG_QUERY_WORKER_POLL_SECONDS=0.5
SCHOLARRAG_QUERY_WORKER_LEASE_SECONDS=30
```

```bash
docker compose up -d qdrant
uv run uvicorn scholarrag.api.main:app --host 127.0.0.1 --port 8000
```

另开终端，在同一仓库和数据目录运行：

```bash
uv run scholarrag worker
```

Worker 要求 session 模式和远程 Qdrant URL。API 摄取与问答 Worker 在不同进程，不能共用同一个嵌入式 Qdrant 目录。两者必须共享 SQLite、本地 artifacts 和 Qdrant 服务；这还不是跨主机方案。

Compose 新增 `query-worker` 服务，API 与 Worker 都明确使用 session 模式和相同 `/data` 卷。`docker compose up --build` 会启动两者和 Web/Qdrant。当前摄取启动恢复仍假定单个 API，不能因此扩大到多个 API 副本。

心跳间隔为 `min(AUTHORIZATION_POLL_SECONDS, QUERY_WORKER_LEASE_SECONDS / 3)`。默认停止响应通常不超过一次授权轮询；进程被强杀后由租约时长决定最早恢复时间。

## 验证与证据

新增 `tests/test_query_worker.py` 共 18 项，另更新既有工作空间测试，使 API 和 Worker 使用独立 Runtime。全量后端 **241 项通过**；Ruff、全量格式、Compose 配置校验与 Python 源码包/轮子构建通过。

覆盖原子并发领取、双 API 容量竞争、入队事务故障回滚、心跳续租与过期写入拦截、排队取消竞争、运行中跨实例取消、检查点恢复/撤权/取消、无检查点停止、终态事务故障回滚、Worker 关闭，以及 CLI 配置防误用。

真实子进程测试同时启动两个独立解释器，共享隔离 SQLite，使用模拟模型并在子进程禁用 socket 连接：同一 Run 只记录一次模型调用，父进程的事件等待者读到子进程写入的有序事件；另一个子进程通过真正 CLI 启动空闲 Worker，SIGTERM 后正常退出。它们证明进程边界和故障策略，不证明真实供应商吞吐、远程 Qdrant 联调或生产稳定性。

验证命令：

```bash
uv run --extra dev --extra observability ruff check src tests
uv run --extra dev --extra observability ruff format --check src tests
uv run --extra dev --extra observability pytest -q
docker compose config -q
uv build
```

本轮没有 Web 源码变更，不重复构建 Web。没有真实供应商调用、用户论文上传或生产数据变更。新提交的远端 CI 尚未触发，原 `291df90` 的 CI 成功不作为本轮改动通过的证据。

## 未完成与下一步

- 摄取独立 Worker、供应商任务 ID 检查点与重复发布防护；评测的工作空间权限和独立执行。
- 跨进程/跨实例供应商速率、预算配额、公平调度与费用对账；当前并发控制仍按进程生效。
- PostgreSQL、对象存储、共享卷故障及真实 Qdrant/容器恢复演练。
- 检索/Agent 阶段恢复、供应商不确定调用的人工或协议级对账。
- Worker 存活监控、队列延迟告警和已声明容量下的压力验收。
