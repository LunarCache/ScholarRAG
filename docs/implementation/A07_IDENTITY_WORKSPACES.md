# A07：身份、工作空间与成员授权基础

日期：2026-10-25。状态：已实现并完成本地测试；当前是多用户基础设施阶段，不是完整多人 Web 产品。

## 1. 范围与结果

新增真实持久化的 User、Workspace、Membership、Session、资源归属登记与审计。没有自动创建默认用户，没有把旧论文库默认分配给所有人，没有增加匿名注册或自制密码登录。

本轮采用本机管理员预置账户、短期不透明会话 Token 的方式，便于先验证身份、撤权和角色边界。后续 OIDC 或浏览器登录会复用相同的稳定用户 ID 与工作空间模型。

## 2. 数据结构与迁移

实现位置：`domain/access.py`、`storage/identity.py`。

在现有 SQLite 数据库中新增以下表，不改写个人数据：

| 表 | 内容与约束 |
| --- | --- |
| auth_schema_versions | 版本标记，当前 v1；拒绝更高版本的降级运行 |
| auth_users | user_id、issuer、subject、display_name、active；issuer/subject 唯一 |
| auth_workspaces | workspace_id、名称、active |
| auth_memberships | 空间与用户复合主键、角色、active；外键约束 |
| auth_sessions | session_id、Token 哈希、用户、绝对过期时间、撤销状态 |
| auth_resources | 资源类型/ID、空间、所有者、private/workspace 可见性 |
| auth_audit | 主体、工作空间、操作、目标与时间，不存会话原文 |

初始化 DDL 与版本标记在同一事务中提交，可重复初始化。写操作使用 BEGIN IMMEDIATE；读取使用独立读取事务。成员变更、Owner 转移和审计写入共同提交。

这是后续 PostgreSQL 迁移前的数据契约与 SQLite 实现。没有进行 PostgreSQL 迁移，没有为旧 Run、笔记或评测自动推断所有者。

## 3. 权限与上下文

AccessContext 为不可变对象，包含 user_id、session_id、workspace_id 和角色快照。它只能由服务端验证后的 Principal 建立；每次授权重新检查会话有效期、撤销状态、用户/空间状态和当前成员角色，不信任旧角色快照。

| 能力 | Viewer | Member | Admin | Owner |
| --- | --- | --- | --- | --- |
| 读取获授权资源、发起查询的策略许可 | 是 | 是 | 是 | 是 |
| 上传的策略许可 | 否 | 是 | 是 | 是 |
| 管理普通成员、管理文档的策略许可 | 否 | 否 | 是 | 是 |
| 授予/变更 Admin | 否 | 否 | 否 | 是 |
| 转移 Owner | 否 | 否 | 否 | 是 |

“策略许可”不代表对应业务接口已经开放；当前 session 模式仍关闭问答和上传，接口覆盖范围见 A08。

Owner 不可通过普通角色修改或移除接口被降级/删除。转移要求目标为当前有效成员，原 Owner 在同一事务中降为 Admin；唯一索引保证一个空间至多有一个有效 Owner，受控创建和转移流程维持一个 Owner。并发转移只允许一方成功，旧上下文无法继续执行 Owner 操作。

未知权限动作默认拒绝。Admin 不能给自己提权，也不能修改其他 Admin；成员与角色参数都不来自可伪造的用户身份 Header。

## 4. 会话模型

- 使用 `secrets.token_urlsafe(32)` 生成不透明 Token，仅签发时返回原文；数据库保存 SHA-256 哈希。
- 默认有效期 8 小时，可设 60 秒至 30 天；绝对过期，不自动滑动延长。
- Token 通过 Authorization Bearer Header 使用，不接受 URL Token，也不接受客户端提供的 X-User-Id 作为身份。
- 可由本机管理员按 session_id 撤销；用户可通过 HTTP 退出当前会话。
- 当前没有密码、刷新 Token、Cookie、CSRF 登录流程、公开注册或 OIDC 集成。

会话有效性每次操作检查，成员撤销不依赖 Token 过期。已发送给客户端的文件字节无法撤回；未来长连接和后台任务还需分别设计持续授权与取消策略。

## 5. 管理员操作

以下命令只能在拥有服务数据目录访问权的可信主机上执行；不应包装成任意远程执行服务。它们使用 `SCHOLARRAG_DATA_DIR` 指向的数据库。

```bash
uv run scholarrag auth create-user "Alice"
uv run scholarrag auth create-workspace <user_id> "Research team"
```

第一条返回稳定 user_id 和本地 issuer/subject；第二条创建工作空间并将指定用户设为 Owner。其他用户也先由管理员预置，再由 Owner/Admin 通过成员 API 添加到空间。

签发会话会输出秘密，不要提交或粘贴到聊天中。可在权限受限的临时文件中接收：

```bash
umask 077
uv run scholarrag auth issue-session <user_id> > /tmp/scholarrag-session.json
uv run scholarrag auth revoke-session <session_id>
```

使用后安全清理该临时文件；日志、终端录屏和 CI 产物也不得保留 Token。服务端数据库与审计不会记录 Token 原文。

## 6. 验证与边界

本轮结果：21 项新增身份/授权测试通过，完整后端 150 项测试通过，Ruff 和格式检查通过；没有执行真实部署或外部登录。

`tests/test_workspace_identity.py` 覆盖迁移幂等、凭据哈希、角色矩阵、伪造角色/主体、身份唯一性、会话过期和撤销、用户/空间停用、成员撤销、Owner 不变量和并发转移、CLI 签发/撤销。完整结果见[实施索引](README.md)。测试仅使用合成身份和临时数据库。

尚需后续工作：OIDC 身份绑定与登录界面、邀请流程、用户停用管理入口、跨实例限流、PostgreSQL/RLS、后台任务身份策略、会话与审计保留策略，以及真实部署威胁模型。不要直接用可变邮箱或显示名称自动合并不同身份。

下一步：基于已验证身份接管会话与 Query Run 归属，再开放工作空间内问答及其 SSE，而不是直接移除旧接口保护。
