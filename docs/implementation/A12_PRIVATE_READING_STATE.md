# A12：用户私有阅读状态、笔记与收藏

日期：2026-10-25。状态：工作空间 API 与存储已实现；浏览器界面尚未接入。

## 1. 问题与结果

旧 personal_paper_state 以 document_id 为主键，同一文档只有一份笔记和阅读状态，不能用于多用户。新增 auth_personal_paper_state，以 workspace_id、owner_id、document_id 为复合主键，隔离每个人的阅读状态、标签、笔记与 favorite 标记。

同空间 Owner/Admin 不会获得其他成员的笔记查看权。用户身份与空间从已认证 AccessContext 获取，请求正文不能指定或覆盖 owner_id、workspace_id。

旧个人表保留，不读取为工作空间数据，不自动分配给某个新用户，也不自动复制给空间成员。现有 local 模式继续使用原表。

## 2. 实现与迁移

- domain/library.py：独立的工作空间请求与响应模型，拒绝多余字段；不改变旧个人 API 模型。
- storage/identity.py：Schema v3 的增量建表和事务授权接口。身份校验、当前角色校验、资源检查与写入使用同一 SQLite 事务。
- storage/workspace_library.py：私有读取、条件保存、清空笔记和审计。
- api/workspaces.py：session 模式下的工作空间 API。

v2 到 v3 只新增个人状态与集合表及版本标记，保留用户、会话、成员、论文归属和私有会话。现有 v1 升级测试也涵盖继续升级至 v3；不进行 PostgreSQL 或旧笔记归属迁移。

文档必须既获当前用户授权，又存在 active parse 才能读取或写入阅读状态。该 active 文档判断也统一用于工作空间查询范围，失效的文档不会继续进入新的检索快照。

## 3. API 与修订号

前缀：`/api/workspaces/{workspace_id}/documents/{document_id}`，要求有效 Bearer 会话。

| 方法与后缀 | 行为 |
| --- | --- |
| GET /personal | 当前用户的阅读状态，不接受其他用户 ID |
| PUT /personal | 完整替换本人状态，检查 expected_revision |
| DELETE /personal | 清空本人状态，保留修订号和最小删除记录 |

不存在记录时 GET 返回默认 unread、空笔记/标签、favorite=false、revision=0、updated_at=null，不创建数据库记录。

保存示例：

```json
{
  "reading_status": "reading",
  "tags": ["retrieval", "methods"],
  "note": "需要复核实验设置。",
  "favorite": true,
  "expected_revision": 0
}
```

成功返回 revision=1。后续保存必须使用最近读取到的 revision 作为 expected_revision；不匹配返回 409，客户端应重新读取并让用户合并，而不能无提示覆盖。

PUT 为完整替换，不是局部 PATCH；省略业务字段会使用其默认值。标签去重并保留顺序。笔记最多 20,000 字符，标签最多 64 个，每个去首尾空白后 1–64 字符。未知身份字段和无效状态返回 422。

## 4. 权限、删除与撤权

Member/Admin/Owner 可保存本人状态；Viewer 可读取本人状态，但不能新增或修改。降为 Viewer 后仍可删除自己已有的数据，不能借旧上下文继续写入。

删除要求用户仍是当前空间有效成员，但不要求仍拥有该论文的读取权。因此论文授权撤销后，笔记读取会拒绝，用户仍能清空自己的旧笔记；DELETE 不会删除其他用户的数据。不存在的本人记录和重复删除均安全返回 204。

删除时清空正文、标签和 favorite，将状态设回 unread，同时递增 revision 并保留 tombstone。重新创建笔记需要使用删除后的新修订号，防止删除前的旧页面命中重置为 1 的版本号后覆盖新数据。

审计只记录主体、空间、动作和文档 ID，不复制笔记正文。逻辑清空不保证 SQLite 历史页、WAL 或备份中的内容立即消失；完整的数据保留与备份删除仍需独立实现。成员被移出空间后的账户级数据清理流程也尚未提供。

## 5. 验证与后续

tests/test_workspace_library.py 验证私有状态、管理员无权查看他人笔记、跨空间文档拒绝、失效文档拒绝、旧角色快照失效、并发写入只成功一个、删除后修订号不重置、参数限制及真实 HTTP 409/422/204 响应。没有调用模型、读取私有 .env 或上传真实论文。

本轮连同 A13 新增 22 项回归，完整后端 197 项全部通过，Ruff 与全量格式检查通过；结果见[实施索引](README.md)。前端本轮未变更；后续 UI 必须保存 revision、处理冲突，并将笔记作为不可信文本安全渲染。

下一步：接入工作空间 UI，再按显式来源与目标用户设计旧个人状态迁移，不以自动全库复制代替归属确认。
