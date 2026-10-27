# A14：统一摄取队列与旧任务移除

2026-10-27 追加：摄取已由 [A22](A22_DURABLE_INGESTION_WORKER.md) 拆出独立 Worker，并新增租约与供应商/产物/索引检查点。本文中的旧摄取执行和历史迁移说明保留为当时记录，当前开发部署以 A22 为准。

实施日期：2026-10-26。状态：API、CLI、存储迁移和 Worker 已实现并通过本地验证；浏览器界面随后由 [A16](A16_BROWSER_SESSION.md)–[A17](A17_BROWSER_INGESTION.md) 接入。

## 范围与结果

按照本次追加要求，不保留历史单用户摄取任务，后续重新创建。现在所有业务上传都提交到同一套工作空间摄取队列，只有 `ingestion_jobs` / `ingestion_job_events` 一组表和一个摄取 Worker。没有为个人模式维护第二套队列。问答 Run 与评测任务暂时仍使用各自的执行模型，本次统一的范围是摄取任务。

删除了 `storage/jobs.py`、旧 `_ingestion_worker` / `_run_ingestion`、`Runtime.jobs` 和对应旧上传/任务路由。CLI `scholarrag ingest` 改成调用工作空间上传 HTTP API，返回排队任务，不直接调用 MinerU。底层 `IngestionService` 仍供统一 Worker、维护工具和离线测试使用。

旧 Web 中调用这些接口的上传按钮、上传函数和轮询已移除。现有页面尚未完成工作空间身份接入，因此本次可使用 CLI 或 API 上传，不能把旧页面当作多用户客户端。

## 数据库迁移

身份 Schema 升至 v4。`IdentityStore` 初始化时检查旧 `ingestion_jobs` 是否缺少 `workspace_id`：若是，先删除旧事件表，再删除旧任务表，然后在同一事务内建立新队列和版本标记。

- 旧 queued / parsing / ready / failed 任务及其事件全部移除，不自动重放、不自动认领。
- PDF、解析产物、Catalog、已建立的用户、成员关系、授权、私人笔记和集合保留。
- 旧上传暂存文件不随任务表删除；它们没有自动归属，后续需要单独的保留与清理策略。
- 重复初始化不会删除新工作空间任务。未知未来 Schema 会拒绝升级，事务回滚保证旧表不会因失败迁移而丢失。
- 这是明确的数据兼容性变更。旧任务 ID 和旧接口调用不能继续使用；原有论文需要时仍可通过既有显式文档归属流程迁移。

验证环境说明已由 A16 更正：后续检查发现此前测试收集会初始化默认 `data` 目录，默认库已确认采用 v4 结构，不能再称实际目录尚未迁移。旧任务删除符合用户明确要求；迁移 SQL 不删除 PDF 和解析文件。现在测试收集阶段已改用临时目录，详见 [A16 的更正记录](A16_BROWSER_SESSION.md)。

## 任务归属与幂等

每个新任务必须包含服务端确认的 `workspace_id`、`owner_id`、`actor_session_id`。任务详情、列表、状态事件和取消均只允许当前工作空间中的任务所有者访问；同工作空间 Admin / Owner 也不能读取其他人的上传任务。

上传要求 Member / Admin / Owner。Viewer 不可提交，但被降为 Viewer 后仍可取消自己先前的任务。会话失效或退出工作空间后不能继续操作任务。

`Idempotency-Key` 必填、非空、最多 128 字符，数据库只保存其 SHA-256。唯一范围为工作空间 + 用户 + key：

- 同范围、同 key、相同 PDF 字节返回原任务；新保存的重复暂存文件清除。
- 同 key 对应不同字节返回 409，不覆盖原任务；PDF 即使视觉相同，重新导出导致字节变化也属于不同请求。
- 不同用户或工作空间可使用同一个 key，互不影响。
- 每次新任务分配随机文档 ID，文件哈希仅做完整性检查。不同用户上传同一字节的 PDF 不合并身份，不暴露全局内容去重命中。

`BEGIN IMMEDIATE` 将幂等检查、容量检查、创建任务、初始事件与审计放在同一事务，防止并发绕过。容量不足返回 409，完成或取消后释放活跃名额。

## 状态与恢复

状态路径为 `queued → parsing → ready / failed / canceled`，事件序号在事务内递增。取消终态任务保持原终态，不重复追加状态事件。

启动时，将上次处于 parsing 的任务标记为 failed / `execution_interrupted`；queued 任务可以领取，但实际执行前重新验证原会话与当前上传权限。解析失败、撤权或进程中断均不自动重试远端解析，以免重复计费。相同 key 查询仍返回失败任务，明确重新提交需使用新 key。

当前只有单进程 API 中的一个串行摄取 Worker。SQLite 原子领取可防止同一时刻重复领取，但尚无任务租约、跨实例恢复协调或独立 Worker 部署协议，不能据此宣称支持多个 Uvicorn Worker / API 副本。新任务列表默认 50 条、最多 200 条；尚无游标分页和全局磁盘配额。

## 接口与配置

启用 `SCHOLARRAG_AUTH_MODE=session`：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/workspaces/{ws}/uploads` | multipart `file` + Bearer + `Idempotency-Key`，202 |
| GET | `/api/workspaces/{ws}/ingestion-jobs` | 所有者私有列表，`limit=1..200` |
| GET | `/api/workspaces/{ws}/ingestion-jobs/{job}` | 私有状态 |
| GET | `/api/workspaces/{ws}/ingestion-jobs/{job}/event-log` | 私有持久化状态事件 |
| POST | `/api/workspaces/{ws}/ingestion-jobs/{job}/cancel` | 取消自己的活跃任务 |

业务响应不返回服务器路径、源文件哈希、幂等哈希或认证会话 ID。成功后返回文档和 parse ID；后续文档授权被撤销时，这两个字段不再展示。响应沿用 session API 的 `private, no-store`。

默认配置：

```dotenv
SCHOLARRAG_AUTH_MODE=session
SCHOLARRAG_WORKSPACE_INGESTION_MAX_ACTIVE=64
SCHOLARRAG_WORKSPACE_INGESTION_MAX_PER_USER=8
SCHOLARRAG_UPLOAD_MAX_BYTES=52428800
SCHOLARRAG_UPLOAD_MAX_PAGES=500
SCHOLARRAG_AUTHORIZATION_POLL_SECONDS=1
```

前两个限额分别针对每个工作空间、以及该工作空间内的每个用户，统计 queued + parsing。原有 PDF 字节、页数、有效性校验继续在入队前执行；代理请求体限制仍需单独配置。

## 使用与兼容变更

本地可信管理员创建账户与工作空间，命令输出 ID 后替换后续占位值：

```bash
uv run scholarrag auth create-user learner --subject learner
uv run scholarrag auth create-workspace usr_REPLACE "My papers"
uv run scholarrag auth issue-session usr_REPLACE
```

将一次性显示的 token 放入调用终端的环境变量，避免作为 CLI 参数传入：

```bash
read -rs SCHOLARRAG_SESSION_TOKEN
export SCHOLARRAG_SESSION_TOKEN
uv run scholarrag ingest paper/example.pdf --workspace ws_REPLACE --idempotency-key example-upload-1
```

API 必须先以 session 模式启动。CLI 默认连接 `http://127.0.0.1:8000`，可用 `--api-url` 指定地址；网络异常时使用同 key 重试上传。命令在 stderr 打印本次 key，在 stdout 输出排队结果，不等待整个解析完成。旧 `ingest --batch-id` 选项已移除，不能将无归属的历史供应商任务直接接入多人任务。

通过 GET 任务详情接口查询进度，通过工作空间文档列表读取成功结果。浏览器 OIDC / 登录、工作空间选择、上传进度与取消 UI 是下一实施项。默认 local 模式保留现有本地读取与问答功能，但不再提供无用户归属的上传队列。

## 验收与未完成项

`tests/test_workspace_ingestion.py` 21 项覆盖：私有任务隔离、同字节跨用户独立 ID、幂等并发、原子领取、容量、撤权、取消后继续处理下一项、故障恢复、API 生命周期执行、CLI 统一提交、v3→v4 旧任务删除和迁移失败回滚。原有上传校验测试已改为工作空间入口，旧队列测试由新测试替代。

本批当时全量后端 **217 项通过**，Ruff 与格式检查通过；Web TypeScript 检查和 Next.js 生产构建通过。未上传真实 PDF、未调用真实供应商。关于默认数据目录初始化的判断已由 A16 更正，当前验证结果见实施索引。

下一步：浏览器身份和上传流程；之后实施独立 Worker、租约/心跳、检查点、对象存储、孤儿产物与暂存文件清理、容量与费用预算及恢复演练。执行阶段的授权与发布边界见 [A15](A15_INGESTION_AUTHORIZATION.md)。
