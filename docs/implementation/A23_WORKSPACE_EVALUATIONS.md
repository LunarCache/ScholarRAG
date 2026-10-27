# A23：私有工作空间评测、逐指标恢复与独立 Worker

日期：2026-10-27。状态：本地实现、自动回归和离线浏览器验收完成；未提交、未推送、未部署。没有真实 Judge 调用或费用验收，整体生产化尚未完成。

## 范围与用户行为

工作空间中，用户可在自己已完成问答下点击“开始评测”，填写可选参考答案，查看运行状态、指标、评测轨迹和保存的参考答案，停止评测，并在刷新/重新登录后恢复该问答的最新评测。评测不会改写原问答的答案、状态或事件流。

创建评测是显式付费意图；界面说明 Judge 可能收费，读取、轮询和恢复历史只发 GET。用户提供的参考答案标记为 candidate，不能设置 reviewed/source IDs 或据此声称人工 Gold。恢复执行不会自动重放结果不确定的 Judge 指标。

工作空间评测 API 只入队，不在 API 进程执行。默认 local 开发模式的原个人 API/CLI 仍可执行本地评测，session 模式继续关闭这些全局个人入口。工作空间数据集评测、人工标注/解析审核、全部评测历史列表及评测报告下载不是本轮范围；浏览器恢复的是该问答的最新一项评测。

## 身份、范围与 API

- 只能提交本人、当前空间、已完成且有 Answer 的 Query Run。Admin/Owner 角色不授予他人私有问答或评测访问权。
- 问题、论文范围、回答和检索片段均从服务端保存的问答读取。请求只能包含 `query_run_id` 和可选 `reference`，未知字段返回 422。
- 每次读取、取消、SSE 事件输出、执行与恢复均重新验证当前会话、空间成员和整个原文档范围。
- 评测读取 chunk 时，再检查每个 chunk 和引用属于允许的文档；不把全库 `chunks_by_ids` 的结果直接发给 Judge。
- 最终发布结果时，在同一 SQLite 写事务再次校验发起会话、原问答归属/状态和文档授权，再提交结果与终态事件。提交前撤权会阻止结果成为 completed。

基础前缀：`/api/workspaces/{workspace_id}`，需 Bearer 会话。

| 方法与路径 | 行为 |
| --- | --- |
| POST `/evaluations` | 私有评测入队；必填 `Idempotency-Key`，JSON 为 query_run_id/reference |
| GET `/evaluations/{job_id}` | 本人授权快照与结果 |
| GET `/query-runs/{run_id}/evaluations/latest` | 本人该问答最新评测；尚无评测时返回 404 |
| GET `/evaluations/{job_id}/event-log` | 有序评测事件日志 |
| GET `/evaluations/{job_id}/events` | 认证 SSE，支持非负 Last-Event-ID 和撤权事件 |
| POST `/evaluations/{job_id}/cancel` | 排队立即取消；运行中持久化取消意图，由 Worker 停止 |

幂等键按空间和用户隔离，只保存哈希。同一键、同一内容即使已完成也返回原任务；同一键改变参考答案或执行配置返回 409。“重新评测”产生新键，表达新的评测意图。首次 POST 响应丢失时，前端重试保留原键，不因旧任务已完成而重复创建。

公开快照不输出内部会话 ID、fingerprint、执行配置或 artifact 路径；Judge 指标异常被脱敏为固定错误原因，不暴露原始 provider 错误或签名地址。SSE 撤权事件不带下一条私有数据。

## 存储与执行

`evaluation/jobs.py` 改为 SQLite `evaluation_job_snapshots` / `evaluation_job_events`：记录空间、所有者、发起会话、父 Query Run、幂等哈希、执行配置、状态、租约、取消标记、完整结果检查点和逐指标检查点。namespace 区分 local 与空间。入队授权/容量/首事件/创建审计同事务提交；终态结果与终态事件也同事务提交。

开发阶段不导入旧 `job.json` / `events.jsonl` 队列历史，不双写旧格式，也不自动删除旧文件。本轮新增独立评测表，不要求再次更改 A22 的 v5 摄取结构；更早开发库仍受 A22 的“新数据目录”要求约束。

- `evaluation/workspaces.py`：严格请求、私有归属、幂等准入、读取/SSE 和事务发布授权。
- `evaluation/worker.py`：领取、重建当前授权、配置核对、恢复或执行评测、结果发布。
- `workers.py`：问答与评测共用的租约心跳、取消、过期接管及关闭监督；摄取仍使用其阶段执行器。
- `evaluation/service.py`：范围检查、workspace 视觉事件读取、指标检查点、workspace 结果文件。
- `evaluation/transport.py`：Judge HTTP 请求的授权、租约与进程内共享 Provider 容量边界。
- `WorkspaceEvaluation.tsx` / `workspace-api.ts`：显式创建/停止、GET 轮询、幂等重试、最新结果恢复和作用域清理。

领取通过 `BEGIN IMMEDIATE` 串行化，生成 token 与到期时间，过期执行者不能写入指标、检查点、事件或终态。工作空间结果文件位于 `data/workspaces/<workspace_id>/evaluations/`，不进入个人全局评测扫描目录；历史对外读取走授权任务快照。

Judge 的 OpenAI HTTP 客户端使用受保护 transport：调用前检查权限；拿到共享 Provider 并发槽后再次检查；响应体读取期间保留该槽，返回前再检查。OpenAI SDK 的自动重试设为 0。这里不声称 Ragas 的一个指标只产生一个 HTTP 请求：如 answer relevancy 本身可能包含多个模型/embedding 操作；跨进程限流和费用预算仍未实现。

## 恢复边界

| 中断点 | 行为 |
| --- | --- |
| queued，尚未执行 | 租约不存在或过期后正常领取 |
| 某指标已完成并保存 | 复用该指标，不再次调用其评分操作；记录恢复事件 |
| 两个指标之间，已进入受控指标循环且没有未完成指标 | 复用已完成项，继续后续指标 |
| 某指标处于 running、无结果检查点 | 失败并说明 Judge 结果不确定，不自动重放 |
| evaluator 已启动，但没有受控指标初始化记录 | 保守判定不确定，不盲目重跑 |
| 完整 EvaluationRun 已保存，终态未提交 | 验证当前授权和配置后直接发布结果 |
| Worker 关闭/租约失效 | 暂停旧写入，保留 running/检查点；到期后按上表处理 |
| 已提交 completed/failed/cancelled | 不再领取 |

Judge 模型、thinking、temperature、上下文窗口、embedding 模型或 Provider 地址变化时拒绝复用旧执行配置。已知评分失败可保存 null 指标并继续其他指标；恢复不会自动重试该 null 项。原问答完成不等于每个 Judge 指标成功，界面显示“无结果”和脱敏原因。

外部调用可能已产生费用，取消不能保证撤回供应商在途工作。文件系统与 SQLite 不构成跨存储事务，失败/撤权可能留下不可经授权任务读取的私有结果文件；后续仍需清理和对账。本实现不承诺 exactly-once 计费、任意调用中点续跑或真实生产容量。

## 启动与配置

```bash
uv run scholarrag worker --kind evaluation
```

要求 session 模式，与 API/其他 Worker 共享 DATA_DIR 与远程 Qdrant 配置。评测依赖安装方式为 `uv sync --extra eval --extra dev --extra observability`。Compose 新增 `evaluation-worker`，镜像已有 eval extra。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| EVALUATION_WORKER_CONCURRENCY | 1 | 每个评测进程并行任务数 |
| EVALUATION_WORKER_LEASE_SECONDS | 30 | 执行租约秒数 |
| EVALUATION_WORKER_POLL_SECONDS | 0.5 | 领取轮询间隔 |
| WORKSPACE_EVALUATION_MAX_ACTIVE | 16 | 当前空间 queued + running 上限 |
| WORKSPACE_EVALUATION_MAX_PER_USER | 4 | 当前用户在该空间的 queued + running 上限 |

以上变量均使用 `SCHOLARRAG_` 前缀。准入容量和幂等基于数据库，多个 API 进程不能绕过；Provider 并发仍按进程生效，没有总费用预留/结算。默认个人 local 模式不运行此 Worker。

## 验证与可复核附件

后端新增 **27 项**，全量 **289 项通过**；前端新增 **2 项**，客户端/SSE 共 **33 项通过**。覆盖跨用户/Admin/空间隔离、范围与 Gold 伪造拒绝、父问答状态、持久幂等与并发准入、限额、取消/撤权、事务发布前撤权、逐指标/完整结果恢复、未知调用停止、旧执行者隔离、入队与终态回滚、transport 排队后重新授权及错误脱敏。CLI 启动/退出覆盖 query、ingestion、evaluation 三类 Worker。

双真实子进程共享隔离数据库，禁用 socket，以模拟评分操作验证同一任务只评分一次并写入私有结果。HTTP transport 使用 MockTransport 验证授权/容量边界。没有执行真实 Ragas/Judge 云端联调或费用对账。

浏览器使用 Playwright 技能与显式启动的 `tests/evaluation_browser_fixture.py`。Fixture 只生成临时身份、单页合成 PDF、合成已完成问答和模拟分数；没有读取私有 .env，所有供应商凭据为假值，API 在 8831、Web 在 3131：

```bash
uv run python tests/evaluation_browser_fixture.py
# 另开终端
cd web
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8831 npm run dev -- --hostname 127.0.0.1 --port 3131
```

已验证：创建/完成、运行中停止、参考答案与轨迹、390px 窄屏无横向溢出、空间切换清空旧问答/评测、刷新重新登录后恢复最新结果。网络故障注入先让服务端创建成功、再丢弃响应，重试两次 POST 使用同一键；Fixture 最终 4 个显式任务（3 completed、1 cancelled），刷新前后仍为 4，原问答保持 completed 且事件数仍为 2。模拟值 0.750 不是实测 RAG 质量。

- [离线验收结果 JSON](acceptance/A23-browser-results.json)
- [桌面截图](screenshots/A23-evaluation-desktop.png)
- [390px 窄屏截图](screenshots/A23-evaluation-mobile.png)

浏览器控制台中的预期网络错误来自首次查询尚不存在的 latest（404）以及故障注入的丢失响应；未观察到 React 渲染异常。测试服务器和独立浏览器会话已停止。

验证命令：

```bash
uv run --extra dev --extra observability ruff check src tests
uv run --extra dev --extra observability ruff format --check src tests
uv run --extra dev --extra observability pytest -q
cd web
npm run lint
npm test
npm run build
cd ..
docker compose config -q
uv build
```

本轮仍为本地修改，未提交、推送、触发新版本 CI 或部署。下一步优先跨进程预算配额、费用预留/结算与公平调度，再处理 OIDC、解析审核迁移、索引批次恢复和真实服务故障演练。
