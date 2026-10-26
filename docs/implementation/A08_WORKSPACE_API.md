# A08：工作空间接口、显式文档归属与迁移保护

日期：2026-10-25。状态：基础接口已实现并测试；个人模式仍为默认，多人问答尚未开放。

后续状态更新：A09/A10 已在新的工作空间路由接通私有问答和 SSE；本文件记录 A08 交付时的接口边界，当前能力以[实施索引](README.md)和[A09](A09_PRIVATE_WORKSPACE_RUNS.md)为准。旧个人接口仍关闭。

## 1. 模式选择

新增配置：

```dotenv
SCHOLARRAG_AUTH_MODE=local
```

- `local`：兼容现有个人 Web/API；共享 Bearer 配置保持原行为，新身份接口返回 404。
- `session`：要求数据库中有效的用户会话；启用下述工作空间接口。共享 SCHOLARRAG_API_BEARER_TOKEN 不能作为此模式的身份凭据。

切换为 session 并重启 API 后，现有 Web 的上传和聊天暂不可使用，因为登录界面、用户私有会话和问答归属尚未迁移。本轮没有修改用户本地 `.env` 或自动切换部署模式。

## 2. 已开放的 HTTP 接口

所有业务接口都要求 `Authorization: Bearer <session-token>`，会话先校验，再按空间成员关系授权。前置代理必须提供 TLS；不能将 Token 放在 URL 中。

| 方法与路径 | 行为 |
| --- | --- |
| GET /api/me | 当前用户 ID 与显示名称，不返回 issuer/subject 或 Token |
| DELETE /api/me/session | 撤销当前会话，保留该用户的其他会话 |
| GET /api/workspaces | 仅列出本人有效成员关系对应的空间 |
| GET /api/workspaces/{workspace_id}/members | 成员可查看空间内有效成员与角色 |
| PUT /api/workspaces/{workspace_id}/members/{user_id} | Owner/Admin 管理成员，正文为 role；不能绕过 Owner/Admin 保护 |
| DELETE /api/workspaces/{workspace_id}/members/{user_id} | 按权限移除成员，不能直接移除 Owner |
| POST /api/workspaces/{workspace_id}/transfer-owner | Owner 原子转移所有权，正文为 user_id |
| GET /api/workspaces/{workspace_id}/documents | 仅列出显式分配且获授权的 active 文档 |
| GET /api/workspaces/{workspace_id}/documents/{document_id}/pdf | 授权后返回原始 PDF |

受保护响应标记 `Cache-Control: private, no-store`。匿名、过期或已撤销会话返回 401；空间或操作无权访问返回 403；Owner 约束冲突等返回 409。

## 3. 未迁移入口默认关闭

session 模式下，旧 `/api/documents`、Query Run、会话、评测、审核、看板、Artifact 等业务入口均拒绝执行，返回 501。该检查对所有 HTTP 方法生效，不只是 GET。认证和部分接口可用不能被误当作原接口已经具备租户隔离。

健康检查仍可访问。启动 session 模式不会启动个人摄取 Worker，也不会恢复或改写旧的个人 Run 终态。未来迁移一个接口族时，应先完成其所有读取、写入、导出、文件与流式事件的权限测试，再加入允许范围。

请求入口在认证前按 IP 限流，认证后按用户限流；限流状态定期回收并限制最多 10,000 个活动键，避免字典长期无界增长。它仍是单进程策略，NAT、可信代理和跨实例额度需后续部署设计。

## 4. 文档归属登记与回退

旧数据不自动采用新身份。可信本机管理员显式执行：

```bash
uv run scholarrag auth assign-document <document_id> <workspace_id> <owner_id>
```

要求原文档已存在，Owner 参数为该空间内有效成员。归属记录默认工作空间共享；当前 HTTP 没有任意认领旧文档的接口，也没有跨空间直接共享接口。

相同归属重复登记是幂等的；改分配到其他空间或所有者会拒绝，必须走显式回退和再分配。回退命令：

```bash
uv run scholarrag auth unassign-document <document_id> <workspace_id>
```

回退只删除授权映射并写审计，不删除 PDF、Chunk、向量或个人数据；下一次工作空间访问即不再授权。管理员可随后显式分配到新的目标空间。这不是数据库、对象与索引的完整迁移，也没有改变当前按 PDF 哈希生成的全局文档 ID。

目标架构仍需使用独立业务文档 ID 与工作空间内去重，以支持不同空间上传同一 PDF 时的独立版本和生命周期。当前桥接登记限制一个旧文档 ID 归属一个空间，避免隐式跨空间复用。

## 5. 存储与工具边界

- Catalog.list_documents 支持 SQL 层 document_ids 过滤，空授权集合直接为空；接口不返回 source_path、artifact_dir 等服务器路径。
- PDF 路径只能从已授权 Catalog 记录读取，不采用客户端文件路径。
- ToolRuntime 可接收 AccessContext 和 IdentityStore。session 模式缺少这两者时直接拒绝构造，不能沿用本地全库默认值。
- 每次工具读取前刷新授权，检索完成、登记证据前再检查；会话/成员已失效则拒绝。有效范围为初始范围与当前授权的交集，后续新增授权不能扩大正在执行的 Run。

这些机制尚未自动传递到全部 RAG 服务和持久化 Run，因此问答入口继续关闭；不能仅凭工具检查就开放最终生成、历史恢复和 SSE。

## 6. 验证

本轮结果：新增的 21 项身份/授权测试通过，完整后端 150 项通过；文档链接检查通过。HTTP 测试通过 ASGI 应用入口执行，非仅对权限函数打桩。

`tests/test_workspace_identity.py` 的两个工作空间和多角色样例覆盖 HTTP 身份伪造、越权成员管理、文档列表/PDF 隔离、未分配文档不可见、旧接口关闭、退出与缓存头、认证前限流、本地模式兼容、工具中途撤权，以及文档归属回退保留源文件。

```bash
uv run --extra dev --extra observability pytest tests/test_workspace_identity.py -q
```

所有样例与会话均为测试生成，无真实身份提供方、供应商调用或 Cloud 导出。未执行真实数据迁移或外部部署，前端本轮未变化。

下一步：为 Conversation、Query Run、Job 和个人状态增加归属与可见性，接通经过授权的工作空间问答和 SSE，再完成浏览器登录。首次生产切换仍须备份、迁移演练与权限回归。
