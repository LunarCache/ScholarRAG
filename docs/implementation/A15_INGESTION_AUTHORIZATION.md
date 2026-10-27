# A15：摄取执行授权、独立文档身份与发布边界

2026-10-27 追加：摄取已由 [A22](A22_DURABLE_INGESTION_WORKER.md) 拆出独立 Worker，并新增租约与供应商/产物/索引检查点。本文中的旧摄取执行和历史迁移说明保留为当时记录，当前开发部署以 A22 为准。

实施日期：2026-10-26。状态：统一队列执行链已实现并通过离线回归；真实供应商实测待完成；浏览器合成验收随后由 [A17](A17_BROWSER_INGESTION.md) 完成。

## 用户可见行为

上传者创建的任务与最终论文默认私有。同工作空间的其他成员及管理员不会因为知道文件哈希、任务 ID 或文档 ID 自动获得内容。共享论文的授权管理仍需后续产品设计，本批没有默认开启工作空间共享。

同一份 PDF 在不同工作空间或不同用户的新任务中获得独立的随机文档 ID。`IngestionService` 接收 Worker 分配的 ID，并在 MinerU 主解析、PaddleOCR 候选、canonical、chunk、向量 payload、路由与 Catalog 中保持一致。源文件的 SHA-256 独立保存，用于校验输入是否被替换。

## 执行时授权

`IngestionGuard` 绑定任务 ID 和服务端 `AccessContext`，每次检查当前会话、工作空间成员状态、当前角色、任务仍处于 parsing，以及原始执行会话是否匹配。角色快照不作为授权依据。

任务恢复或排队后，即使随后权限变化，也必须以最新状态执行。执行前校验暂存 PDF 的完整 SHA-256；不一致或文件缺失时终止，不调用供应商。任务期间还有按 `authorization_poll_seconds` 轮询的撤权检查，取消只作用于当前任务子协程，不会停止整个队列 Worker。

授权通过 ContextVar 传播到异步任务和 `asyncio.to_thread`。摄取 Guard 的检索文档范围固定为空：生成 embedding 的权限不等于读取工作空间文献的权限。

- MinerU 提交、轮询和签名存储请求前检查当前 Guard。
- SiliconFlow 沿用请求、排队获得容量后、响应和流读取期间的授权检查。
- PaddleOCR 回退阶段入口复核授权，执行中的撤权由监视器停止；尚未将其 SDK 的所有内部 HTTP 请求逐一包装为授权边界。
- 索引激活前与 Catalog 注册前检查授权。
- 最终发布在数据库写事务内再次检查，防止撤权后的迟到结果成为可读文档。

停止本地协程不能保证已提交给外部服务的解析立即停止，也不能撤销已发生的费用。同步计算、正在执行的线程写入和外部请求也可能在撤权后完成一部分工作；本批保证的是不以过期权限成功发布结果，不能宣称对供应商和线程具有强制事务取消能力。

## 发布与一致性

解析和索引成功后，`publish()` 验证：任务归属、原会话、parsing 状态、分配文档 ID、活动 parse 和 Catalog 源文件哈希。

同一事务内提交以下内容：

1. 上传者私有的 `auth_resources` 文档授权。
2. 任务 parse ID 和 ready 状态。
3. ready 状态事件。
4. `ingestion.published` 审计。

测试通过注入审计写入异常，确认授权、状态、parse ID 和事件一起回滚。撤权、用户取消或解析出错后不授予访问，也不追加 ready 事件。成功后删除上传暂存副本，保留 ArtifactStore 保存的源文件。

向量、文件、Catalog 与上述发布事务尚不是跨系统原子事务。未发布的解析可能留有文件、向量或活动 Catalog 记录，但没有可供工作空间读取的授权；现有检索仍必须通过授权文档集合和活动 parse 过滤。需要后续增加孤儿检测、索引回收、补偿和对象存储生命周期，不能将这些未发布记录当作已完成的文档。

ArtifactStore 当前仍在统一 `artifacts/<随机文档ID>/...` 布局下保存文件，文件系统目录本身不是租户 ACL。通过 API 的授权控制访问，后续迁移到工作空间对象存储前缀和受控下载。管理员直接访问主机文件系统仍属于可信运维边界。

## 故障、诊断与 Langfuse

对用户和任务事件只暴露固定错误码：`authorization_revoked`、`execution_stopped`、`execution_interrupted`、`ingestion_failed`，避免泄露供应商原始错误中的签名链接、路径或凭据。用户主动取消保留 canceled，不被后台错误覆盖；成功终态也不会被后续清理错误改写。

失败/取消的暂存文件和解析诊断产物暂时保留；它们需要独立的清理与保留策略，尚未实现账户级物理擦除。旧队列迁移也不删除其源文件。

可选 Langfuse 新增 `scholarrag.ingestion` 根 trace，使用随机 job ID 作为 trace ID，传播真实认证用户和工作空间 ID。该 trace 不发送 PDF、文件名、路径、源文件哈希或认证会话 token，embedding 调用沿用已有观测。默认仍关闭导出，内容采集仍需显式启用。MinerU/PaddleOCR 尚无完整阶段级观测，本批不宣称已经覆盖解析链所有耗时与费用。

## 验收

本批统一队列测试总计 21 项，其中执行链覆盖：

- 过期/撤销会话、移除成员、降为 Viewer 后，排队任务不会调用解析器。
- 已执行任务取消后，Worker 仍继续处理下一任务。
- 真实 `IngestionService` + 合成 MinerU 输出串联 canonical、chunk、索引接口、Catalog、授权发布和源文件保存，验证分配 ID 一致；供应商与索引网络操作被离线替换。
- 最后时刻撤权，Catalog 中已有结果也不能发布为 ready。
- 源文件被替换时提前拒绝；每次 MinerU 远端请求前复核授权。
- 数据库失败原子回滚，公开错误不含供应商敏感错误内容。

```bash
uv run --extra dev --extra observability pytest -q tests/test_workspace_ingestion.py
uv run --extra dev --extra observability pytest -q
uv run --extra dev --extra observability ruff check src tests
uv run --extra dev --extra observability ruff format --check src tests
cd web
npm run lint
npm run build
```

结果：本批完整后端 **217 项通过**；Ruff、全量格式、Web 类型检查与生产构建通过。没有调用真实供应商或验证其账单、远程取消与限速行为。

后续顺序：接通浏览器会话与工作空间流程，再开展独立 Worker、跨系统补偿、共享文档管理、预算结算与真实隔离验收。队列配置、使用和历史任务删除规则见 [A14](A14_UNIFIED_INGESTION_QUEUE.md)。
