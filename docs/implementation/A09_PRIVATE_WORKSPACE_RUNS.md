# A09：私有会话、工作空间 Query Run 与任务归属

日期：2026-10-25。本文保留 A09 当时的实现与验收记录。

2026-10-27 更新：浏览器登录与问答已由 A16–A20 接通；任务存储、执行、取消与容量限制已由 [A21](A21_DURABLE_QUERY_WORKER.md) 改为 SQLite 事务和独立问答 Worker。下文单进程与 JSON 文件说明属于旧实现，不作为当前部署依据。

## 1. 数据与隔离

身份 Schema 升级至 v2，新增 auth_conversations 与 auth_query_runs，记录空间、用户、会话、发起会话 ID、文档范围和创建时间。v1 的用户、成员与有效会话保留；v2 迁移可重复执行，不导入旧个人历史。

Conversation 与 Query Run 默认且目前仅支持用户私有：同空间的 Member、Admin、Owner 都不能读取、导出、取消或接入其他人的私有 Run/SSE。管理空间的权限不隐含私有会话查看权。后续共享必须独立设计。

每个工作空间的问答文件位于：

```text
data/workspaces/<workspace_id>/query_runs/<run_id>/
```

原个人 data/query_runs 不变，不会被本地模式的历史扫描读取为工作空间历史。目录名称校验为服务端生成的空间 ID，先校验数据库归属，再访问文件；数据库索引与文件中的空间、所有者、会话和文档范围必须匹配。

## 2. 创建、追问与读取

- 客户端不指定 Conversation 时由服务端生成；指定已有 ID 时必须属于本人和当前空间。
- 文档范围为当时获授权文档的显式快照。省略 document_ids 表示当前可访问集合，空数组表示空；越界 ID 返回 403。
- 父 Run 必须属于同一个私有会话、已完成且文档范围相同。更换论文范围时不复用父回答上下文，可创建无 parent_run_id 的新分支。
- 父轮在后台读取时再次授权；不会仅因客户端知道 ID 就加入上下文。
- HTTP 快照不输出内部 actor_session_id 和 artifact_dir。普通成员不能通过列表、导出或事件绕过直接读取限制。
- 历史回答读取要求其整个文档快照仍获授权；撤销某篇论文归属后，该回答无法直接读取，列表略过该项。恢复授权后可重新读取。
- 会话列表只包含本人会话 ID、时间与计数，不返回未重新授权的旧问题或回答。

文件生成后才登记数据库归属，登记失败清理尚未发布的文件。两种存储没有跨系统原子事务；崩溃可能留下不可通过 API 读取的孤立文件或空会话，需要后续对账机制处理，不能声称 exactly-once。

## 3. API

基础前缀：`/api/workspaces/{workspace_id}`。需 session 模式与 Bearer 会话。

| 方法与后缀 | 行为 |
| --- | --- |
| POST /query-runs | 创建异步问答，返回 202；请求体沿用 QueryRequest |
| GET /query-runs | 本人可访问历史，limit 1–200 |
| GET /query-runs/{run_id} | 读取授权快照 |
| GET /query-runs/{run_id}/event-log | 获取授权事件日志 |
| GET /query-runs/{run_id}/events | SSE，支持 Last-Event-ID |
| POST /query-runs/{run_id}/cancel | 取消本人任务，处理尚未开始的任务 |
| GET /query-runs/{run_id}/export | 导出已完成结果与引用为 Markdown |
| GET /conversations | 本人私有会话元数据 |
| GET /conversations/{conversation_id} | 同会话内本人可访问 Run |

旧 `/api/query-runs` 等入口仍在 session 模式下关闭，上传、评测、个人笔记与集合还未迁移。A08 关于“工作空间问答尚未开放”的状态由本记录更新，不代表其他接口已接通。

## 4. 执行与资源限制

WorkspaceQueries 管理带归属的执行任务，后台从持久化的用户、空间与发起会话重新验证权限，不从可变的全局当前用户获取身份。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| SCHOLARRAG_WORKSPACE_QUERY_MAX_ACTIVE | 16 | 当前进程工作空间问答总活跃数 |
| SCHOLARRAG_WORKSPACE_QUERY_MAX_PER_USER | 4 | 同用户跨空间活跃数 |
| SCHOLARRAG_AUTHORIZATION_POLL_SECONDS | 1 | 后台任务与空闲 SSE 的授权轮询间隔 |

提交在进程内锁下检查额度，超额返回 429，不创建额外任务或文档调用。取消尚未开始的任务也会写入终态，避免永久 queued。

重启仅扫描身份索引中已登记的工作空间任务，将中断任务标为失败并保留事件；不启动个人摄取 Worker、不改写旧个人 Run。此为中断恢复识别，不是阶段续跑或付费调用自动重试。

## 5. 验证与限制

本轮结果：13 项工作空间问答回归通过，包含真实工具 Agent 循环与模拟模型交互；完整后端 175 项通过，Ruff 和格式检查通过。没有改动 Web，本轮未重跑前端构建。

新增测试位于 tests/test_workspace_queries.py，覆盖私有历史、跨用户/空间访问、父轮约束、文档越界、SSE 重放与撤权、任务额度、排队取消、命名空间恢复、迁移与并发上下文。结果见实施索引。

本阶段仅支持单 API 进程；任务字典、文件锁、事件条件与额度不是跨进程协调。部署多 Worker/多副本前必须迁移共享事件存储、原子任务领取和租约。会话续期、服务账户、长任务是否应独立于发起登录会话存活，仍需明确策略；当前会话失效会停止正在执行的任务。

下一步：迁移个人状态和摄取任务归属，完成浏览器身份入口，然后交付持久化队列及独立 Worker。
