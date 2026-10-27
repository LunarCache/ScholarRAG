# A25：工作空间解析版本审核与切换

实施日期：2026-10-27。本轮交付版本查看和已注册版本的激活，**未完成整个解析审核体系或 OIDC**。

## 用户行为与范围

浏览器新增“解析审核”页签。成员可查看有权限论文的解析版本、质量分数与决策、当前版本及最近 50 条审核记录；所有者和管理员可填写理由，切换已经注册到目录的版本。私有论文的访问规则继续生效，管理员身份不赋予读取其他用户私有论文的权限。

未注册候选显示“待索引，暂不可激活”，不出现在激活目标列表中，服务端也拒绝直接提交。这一版不在 API 请求中执行 embedding 或注册未索引候选。现有摄取流程成功注册版本的前置条件是索引完成；该约束不代表已经对历史向量逐条完成了完整性对账。

`fallback` 质量候选必须显式勾选“已人工检查，允许覆盖”才能激活。每次操作保存服务端身份、前后版本、理由、force、时间与修订号。客户端提交 actor 会被拒绝，操作者不再固定为旧入口的 `local_user`。

切换论文或工作空间清除相应草稿。激活冲突或请求结果未确认时保留当前理由，禁用重复激活，要求显式刷新记录后重新选择。刷新后的草稿不会被自动提交。

## 实现与权限

- `ingestion/workspace_reviews.py`：工作空间权限、受限版本快照、请求校验、事务激活和审核记录。
- `api/workspaces.py`：认证接口。
- `web/app/workspace/WorkspaceParseReview.tsx`：版本查看、理由、质量覆盖确认与冲突处理。
- `web/lib/workspace-api.ts`：Bearer、JSON Content-Type、no-store、可取消请求及范围 URL。

接口：

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/api/workspaces/{ws}/documents/{doc}/parse-review` | 当前成员、当前会话、该论文读取权限 |
| POST | `/api/workspaces/{ws}/documents/{doc}/parses/{parse}/activate` | 同上，且 Owner/Admin |

激活请求包含 `expected_revision`、`expected_parse_id`、3–2000 字符理由及 `force`。多余字段拒绝。返回快照仅白名单输出必要字段，不返回 artifact/source 路径、原始 manifest 或供应商响应。

服务端在 `BEGIN IMMEDIATE` 写事务内重新验证权限和文档可见性；目标必须属于该论文，manifest、Canonical、Chunk 身份及源哈希必须匹配。版本路径也必须匹配注册记录。候选缺失、未注册、质量未确认或旧修订返回 409；无权限返回 403，缺少认证返回 401。

活动解析、文献标题、确定性元数据、知识关系、审核记录和权限审计在同一事务内更新。审核修订号与旧活动版本共同防止并发覆盖及 A→B→A 后的旧请求重用。注入审计失败时上述更新一起回滚。已有 FTS/Chunk 和向量不重写，检索仍按活动解析过滤。

审核记录以 SQLite 为准。工作空间入口不调用旧 `ParseReviewService.activate`，不写它的 `local_user` JSON 审核文件，也不改写 manifest 的 ready 状态；工作空间页面从目录读取活动状态。旧的全局解析版本/激活与人工 Gold/QA/Judge 审核接口在 session 模式仍被阻断，不能用它们绕过空间权限。

## Schema、兼容与回滚

新增 `workspace_parse_reviews` 辅助表，按空间/论文保存递增修订和审核明细；不修改身份 Schema v5，不改写已有任务、解析文件或授权。初始化幂等，无历史单用户审核记录自动导入。

回滚前停止新版 API，再使用旧版本。辅助表可保留，旧代码忽略它；已完成的版本切换保留。恢复此前活动版本应执行一次新的审核切换，保留完整审计。不要通过删除审核记录重置修订号。历史审核的保留与完整资源删除仍需后续完善。

不要同时用旧 CLI/本地模式工具改动同一开发库的活动版本；这些可信维护入口不参与工作空间审核的修订契约。

## 验收结果

新增 **17 项后端回归**和 **1 项前端客户端回归**：

- 快照不泄露内部路径、切换与元数据一致、服务端真实审核人。
- Member/Viewer 可读共享版本但不可激活；Admin 不可读他人私有文档。
- 跨空间访问、注销后的缓存上下文、未知/跨文档版本不可用。
- 旧修订与 ABA 冲突、并发只有一个成功、审计失败整事务回滚。
- 源哈希、manifest、Chunk 不一致拒绝；未注册版本明确显示待索引。
- fallback 必须明确 force；HTTP 不接受 actor、旧全局入口仍 501。
- 前端范围 URL 编码、JSON Content-Type、预期版本/修订与 no-store。

本地后端 **333 项通过**；Ruff 与全量格式检查通过。前端 **34 项通过**、TypeScript 和 Next.js 生产构建通过。Node 原有 MODULE_TYPELESS_PACKAGE_JSON 提示仍存在，不影响测试结果。

浏览器使用 `tests/parse_review_browser_fixture.py` 创建一次性身份、合成 PDF、两个注册版本与一个未注册候选；无真实供应商、Worker 或用户论文。已验证：

1. Owner 激活旧版本，修订 0→1，论文标题与当前版本一起更新。
2. 第二窗口恢复历史并激活新版本，修订 1→2；第一窗口旧请求 409，理由保留，重复激活按钮禁用。
3. 刷新登录后 Viewer 可见修订 2 和两条历史，没有激活表单；待索引候选不能选作目标。

[浏览器截图](screenshots/A25_parse_review_desktop.png)。本轮未做手机布局、真实向量损坏、真实供应商或 OIDC 联调。浏览器过程中发现并修复 JSON Content-Type 缺失；修改客户端类后的开发热更新会影响旧内存实例，重新加载登录后完成最终验收。

复现：

```bash
.venv/bin/python tests/parse_review_browser_fixture.py
# 另开终端
cd web
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8832 npm run dev -- --hostname 127.0.0.1 --port 3132
```

该 fixture 的登录值是本地合成账户 `review-owner-fixture` / `review-viewer-fixture`，不是供应商凭据。启动前已屏蔽项目 .env 并设置临时数据目录。普通 pytest 不启动该 fixture。

## 后续方向与排期调整

用户已明确“不考虑任务预算和费用问题”：任务预算、费用预留/结算退出当前实施范围；并发、速率和故障控制继续作为可用性需求。此次未增加财务配额功能。

接下来继续 OIDC，以及未注册候选的持久化索引准备与审核任务；还需迁移 Parser Gold、QA、Judge 校准接口的真实人工身份与权限。PostgreSQL、对象存储、索引批次恢复与恢复演练仍在后续计划中。当前改动尚未提交或推送。
