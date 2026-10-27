# A22：独立摄取 Worker、供应商检查点与事务发布

2026-10-27 追加：评测已由 [A23](A23_WORKSPACE_EVALUATIONS.md) 接入工作空间并拆出独立 Worker。本文保留 A22 当时的实现记录，当前部署包括 query、ingestion 和 evaluation 三类 Worker。

日期：2026-10-27。状态：本地实现与回归完成；未提交、未推送、未部署。继 A21 后拆出摄取执行，评测工作空间迁移和跨实例预算仍未完成。

## 用户可见变化与开发 Schema

API 只负责上传校验、入队、私有任务读取和取消，启动时不再启动摄取协程或把所有 parsing 任务判失败。摄取由 `scholarrag worker --kind ingestion` 执行；没有摄取 Worker 时，上传保持 queued。

按当前开发约束直接使用新的 `ingestion_jobs` 结构：新增租约 token、到期时间、Worker ID 和私有 checkpoint JSON，身份 Schema 记为 v5。不提供旧任务迁移或自动删除旧任务的兼容逻辑。检测到缺少新字段的开发库时，启动会给出 `Development ingestion schema changed; use a new data directory`，旧库不会被重写或清空。

本地可设置新的 `SCHOLARRAG_DATA_DIR=./data-dev-a22`，再通过 `scholarrag auth` 创建测试用户与空间。Compose 使用命名卷；旧卷不兼容时可选择新的 Compose 项目名创建新开发卷，例如 `docker compose -p scholarrag-dev-a22 up --build`，不要把修改宿主机 DATA_DIR 误认为已切换容器内 `/data` 卷。本轮没有重置或操作用户现有开发库、论文与索引。

## 实现与执行边界

| 位置 | 职责 |
| --- | --- |
| `ingestion/worker.py` | 独立进程循环、并发上限、原子领取、关闭与旧任务接管 |
| `ingestion/workspaces.py` | 私有队列、租约/心跳、检查点、授权、取消和事务发布 |
| `ingestion/checkpoints.py` | 绑定任务租约的检查点读写；不确定外部结果异常 |
| `ingestion/service.py` | 复用供应商任务、候选产物与索引检查点，固定恢复配置 |
| `providers/mineru.py` | 分配 batch ID 后、上传 PDF 前回调持久化检查点 |
| `providers/paddleocr.py` | 线程开始与资源下载前检查当前授权/租约 |
| `storage/catalog.py` | 支持使用外部 SQLite 事务激活文献及写入元数据 |
| `storage/artifacts.py` | JSON 先写独立临时文件，检查授权后原子替换 |
| `api/main.py` / `cli.py` | API 启动不执行摄取；CLI 按 query/ingestion 类型启动 Worker |

领取使用 SQLite `BEGIN IMMEDIATE`，原子选择 queued 或租约过期的 parsing 任务，生成新 token。未到期任务不能被第二个 Worker 领取。执行中按 `min(AUTHORIZATION_POLL_SECONDS, INGESTION_WORKER_LEASE_SECONDS / 3)` 检查权限并续租。

检查点、失败状态和发布写入都在事务内校验 token、到期时间与 parsing 状态。取消立即提交 canceled 并清除租约，旧执行者不能把它写成 ready。Worker 正常关闭先暂停写入、取消执行协程，保留 parsing 与检查点，由新执行者在租约过期后接管，不冒充用户取消。

归一化、切块/质量评估和图像回填在线程执行，避免长时间占用事件循环导致心跳停顿。恢复未完成解析时会分配新的 parse ID 和产物目录；已完成候选保持原 parse ID。旧线程可能继续执行尚未结束的本地或 SDK 操作，但不能保存新检查点或激活文献。

## 检查点与恢复策略

检查点保存在私有任务行，不在 HTTP 任务快照和事件中暴露。保存供应商任务 ID，不保存 MinerU 的签名上传 URL；原始 provider response 继续留在本地私有 artifacts。

| 阶段 | 检查点 | 恢复行为 |
| --- | --- | --- |
| 初始排队 | 无外部调用记录 | 首次执行 |
| MinerU POST 开始、尚无 batch ID | `submitting` | 标记 `external_outcome_unknown`；不自动重新 POST |
| 已分配 MinerU batch ID，上传尚未确认 | `allocated` + batch ID | GET 原批次；若已 pending/running/done，继续原任务；若仍 waiting-file/无记录，停止，不重新上传 |
| MinerU 上传返回 | `submitted` + batch ID | GET/等待原批次，再下载结果；不重新提交 PDF |
| PaddleOCR 提交开始、尚无 job ID | `submitting` | 标记不确定，不自动重新提交 |
| PaddleOCR 已返回 job ID | `submitted` + job ID/options | 等待原任务并下载资源；不重新解析 |
| 候选已完成 | `ready` + 产物路径、校验和、供应商 ID | 校验 canonical/chunks/quality 三个 JSON 和源 PDF，复用候选 |
| 路由已选择 | `selected` + 候选、路由决策/路径 | 复用已选择 parse，继续激活流程 |
| 文本或视觉索引即将执行 | `running` + parse ID | 中断后停止，不盲目重做可能已计费的 embedding |
| 文本或视觉索引完成 | `done` + parse ID/count | 跳过该索引阶段，继续后续步骤 |
| 发布事务完成 | ready + parse ID | 不再领取；Idempotency-Key 重放返回同一任务 |

供应商提交响应丢失、上传结果不确定和恢复中发现未完成索引，都会保留检查点并返回脱敏的 `external_outcome_unknown`。其他已知执行错误标为 failed，也不自动重试。新的尝试应使用新 Idempotency-Key，不会悄悄重放旧请求。

恢复会再次检查发起会话、成员角色、源文件哈希，以及影响解析/质量/embedding 的配置。配置已变化时拒绝复用旧阶段，避免拿旧模型或旧 Qdrant 的索引检查点跳过新配置下的工作。候选文件校验失败也会停止，不凭路径存在就当作有效结果。

## 激活与权限发布

工作空间摄取服务只准备产物并建立索引，不提前注册可读取的 Catalog 文献。Worker 最终在同一授权写事务中完成：

1. 校验当前用户/空间/上传权限、发起会话、任务状态和执行租约。
2. 注册文献、激活 parse、写入 chunks/FTS、元数据与知识关系。
3. 核对文献与原上传内容哈希，授予文献私有访问权。
4. 提交任务 ready、parse ID、终态事件和发布审计，清除租约。

任何一步失败，以上 SQLite 内容一起回滚。旧 Worker、取消后的任务和撤权会话不能发布晚到结果。任务发布成功后才删除临时上传文件；保留的源 PDF 位于 artifacts 中。

Qdrant 和文件系统不属于 SQLite 事务：索引或下载已产生但尚未发布时，可能留下无授权、非 active parse 的向量或文件，后续仍需对账/回收。此实现不声称跨存储 exactly-once，也没有批次级 embedding 续跑。

## 启动与配置

使用与 API 相同的 `.env`、数据目录及远程 Qdrant：

```dotenv
SCHOLARRAG_AUTH_MODE=session
SCHOLARRAG_QDRANT_URL=http://localhost:6333
SCHOLARRAG_INGESTION_WORKER_CONCURRENCY=1
SCHOLARRAG_INGESTION_WORKER_POLL_SECONDS=0.5
SCHOLARRAG_INGESTION_WORKER_LEASE_SECONDS=30
```

```bash
uv run scholarrag worker                  # 问答
uv run scholarrag worker --kind ingestion # 摄取，另开终端
```

Compose 新增 `ingestion-worker`，与 API、query-worker 共用 `/data`、evals 卷和 Qdrant 服务。两个 Worker 的并行数分别配置；Provider 并发上限仍按进程生效，不是全服务共享额度。

普通独立进程部署要求远程 Qdrant，不能共用嵌入式 Qdrant 目录。`tests/live_browser_fixture.py` 是显式启动的真实供应商测试工具，为避免污染外部向量库，它使用隔离临时目录和嵌入式 Qdrant，并在 Fixture 内同进程启动两个 Worker 类；这不用于证明多进程或生产容量。普通 API 没有这一例外。

PaddleOCR 路径需要可选 Paddle SDK。当前默认 Dockerfile 没有安装 `paddle` extra；需要该路径时应使用已安装此 extra 的开发环境或定制镜像，本轮未验证真实 SDK/容器联调。同步 SDK 的在途提交/轮询线程可能持续至完成或超时，协程取消不能保证立即停止它；租约阻止其结果被晚到发布，服务管理器仍需提供进程终止上限。

## 验证

新增摄取恢复测试覆盖供应商 ID 复用、不确定提交/上传、候选与配置校验、已完成索引跳过、索引中断停止、关闭恢复、过期写入拦截、撤权/取消、发布事务回滚和慢归一化期间续租；MinerU 契约测试确认检查点回调发生在 PUT 前，检查点失败时不会上传。

双真实子进程测试共享隔离 SQLite，禁用 socket，复用已保存供应商 ID，使用模拟解析与 embedding：同一任务只执行一次 embedding、只记录一次发布审计。CLI SIGTERM 测试覆盖 query 与 ingestion 两种启动方式。原有 API 测试已改为确认 API 不执行上传，独立 Worker 启动后才处理。

本轮未调用真实 MinerU/PaddleOCR/SiliconFlow、未上传真实论文、未运行真实 Qdrant 或 Docker 容器。验证边界是代码、独立解释器、模拟供应商和配置检查；不代表费用对账、质量 Gold、容器就绪或生产恢复验收。

```bash
uv run --extra dev --extra observability ruff check src tests
uv run --extra dev --extra observability ruff format --check src tests
uv run --extra dev --extra observability pytest -q
docker compose config -q
uv build
```

本轮新增 **21 项**测试，全量后端 **262 项通过**，Ruff/全量格式、Compose 配置和 Python 打包通过，文档链接检查通过。最终本地结果同步记录在实施索引；未提交/推送，未触发新版本远端 CI。Web 源码未变，本轮未重复 Web 构建。

## 下一步

评测任务的工作空间归属、授权与独立执行；跨进程预算/速率与公平调度；embedding 批次检查点与向量/文件回收；真实 Qdrant、SDK、容器重启与故障演练。整体阶段 C 和生产上线验收仍未完成。
