# A13：用户私有论文集合与可撤销成员引用

日期：2026-10-25。状态：私有集合 CRUD API 已实现；空间共享集合与浏览器接入待后续交付。

## 1. 数据与归属

Schema v3 新增 auth_collections 与 auth_collection_documents。集合属于一个工作空间和一个用户，成员论文按位置保存并去重，保留用户提交的顺序。

同空间 Owner/Admin 不能访问或修改其他用户的私有集合；普通用户也不能通过猜测 collection_id 枚举他人集合。集合所有者只能来自认证上下文，正文不接受 owner_id 或 workspace_id。

旧 collections 表保持独立，新 API 不扫描或自动导入旧个人集合。新增集合不会改变个人模式或旧集合的内容。

## 2. API

前缀：`/api/workspaces/{workspace_id}`，要求有效 Bearer 会话。

| 方法与后缀 | 行为 |
| --- | --- |
| GET /collections | 当前用户的集合，默认 limit=50，允许 1–200 |
| POST /collections | 创建私有集合，返回 201 与 revision=1 |
| GET /collections/{collection_id} | 读取本人的集合 |
| PUT /collections/{collection_id} | 条件替换名称与完整论文列表 |
| DELETE /collections/{collection_id} | 清空并删除本人的集合，返回 204 |

创建示例：

```json
{"name":"待精读","document_ids":["doc_first","doc_second"]}
```

更新时增加 expected_revision，必须与读取到的 revision 一致：

```json
{"name":"已筛选","document_ids":["doc_second"],"expected_revision":1}
```

名称去首尾空白后为 1–120 字符，每次最多 1,000 个文档 ID，每个 ID 为 1–128 字符。空集合允许创建。当前限制是请求字段上限，不是账号总存储额度，入口请求体限制与空间配额仍需后续部署设计。

## 3. 一致性与权限

创建和修改只允许 Member/Admin/Owner；Viewer 只读本人既有集合，并可删除自己的旧集合。会话、成员状态和实际角色在事务内重查，不能凭旧角色快照写入。

保存前验证所有成员论文均为当前空间内获授权且存在 active parse 的文档。无效成员或外部文档导致整体拒绝，不留下改名成功但列表写入失败的半完成状态。

集合、顺序成员与审计共同提交。并发或旧页面的修订号不匹配返回 409；更新为完整替换，不是列表追加。用户重新加载后应确认实际列表再提交。

## 4. 文档授权撤销与清理

读取时重新计算有效文档范围。已撤销或失效的论文 ID 不出现在 document_ids 中；unavailable_document_count 给出被隐藏成员数量，不返回其标题、路径或内容。读取本身不改写集合。

若用户基于过滤后的列表执行完整 PUT，保存后的列表会替换原始成员，包括移除未提交的隐藏成员；客户端应据 unavailable_document_count 提示用户确认。授权恢复前不能通过集合绕过论文访问控制。

删除会清空名称和成员引用，保留最小删除标记、修订号和不含名称的审计。已删除 ID 不通过 PUT 复活，重复删除本人集合可返回 204。集合删除不删除论文、向量、其他人的笔记或集合。

集合共享、跨空间分享、成员邀请、分页游标、配额与最终物理清除仍未实现。当前私有边界不能被解释为已交付共享集合权限模型。

## 5. 验证

tests/test_workspace_library.py 的共享回归覆盖：跨用户私有隔离、Admin 越权拒绝、集合创建/读取/替换/删除、去重与排序、无效文档拒绝、数据库插入故障下名称/成员/修订号/审计整体回滚、修订冲突、文档撤权后隐藏、清空敏感名称及引用、旧表隔离与 v2→v3 升级。HTTP 用例检查 201/200/204/403/409/422 和 private,no-store。

```bash
uv run --extra dev --extra observability pytest tests/test_workspace_library.py -q
```

本轮连同 A12 新增 22 项测试，完整后端 197 项全部通过，Ruff 与全量格式检查通过；完整结果见[实施索引](README.md)。所有数据为临时数据库与合成文档，没有真实供应商费用。

下一步：将这些接口接入身份明确的工作空间前端，再根据产品需求设计共享可见性；现有集合的 document_ids 已能作为工作空间问答的选择范围，问答仍会独立重新授权。
