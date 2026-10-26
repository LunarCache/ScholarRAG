# A02：查询与工具范围强约束

日期：2026-10-25。状态：本项已实现；工作空间鉴权仍待实施。

## 问题与结果

原来模型可以传入不同的 document_ids 扩大范围，且 `[] or None` 会将空范围变成全库检索。现在 `None` 表示本地模式未限定范围，`[]` 明确表示不允许任何文档。模型只能在本次选择范围内缩小检索。

## 实现

- 新增不可变 `rag/scope.py::DocumentScope`，计算用户选择与未来服务端授权集合的交集。工具请求越界直接拒绝；空交集保持空。
- `ToolRuntime` 接受服务端传入的可选 authorized_document_ids；当前 API 尚未提供真实工作空间授权，因此不能据此声称完成租户隔离。
- `list_papers` 先过滤范围再返回；`get_paper` 在读取 metadata/canonical 前检查范围。
- `retrieve` 校验模型请求范围；空范围不调用检索 Provider，结果进入 EvidenceRegistry 前再检查实际所属文档。
- `read_chunk` 同时检查注册证据与数据库读取路径，越界时不读取图片或返回正文。
- Catalog 的 active_parse_ids、lexical_search、knowledge_graph 和 chunks_by_ids 保持空范围关闭；Chunk 查询支持 SQL 层文档过滤。
- 文本/视觉 VectorIndex 对空 document_ids 或 parse_ids 直接返回空，不构造无过滤查询。
- 检索 Provider 延迟到确认有 active parse 后创建；工具 Agent 的显式空范围使用无模型调用路径。
- 工具执行前使用现有 JSON Schema 校验参数类型，拒绝将字符串等错误类型当作文档集合。

## 兼容性

直接 API 调用方若曾用 `[]` 表示全部论文，应改为 `null` 或省略。Web 未选择论文时已使用未限定范围语义。此次不改变文档 ID，不迁移索引。

## 验证

`tests/test_scope_boundaries.py` 使用两个实际 Catalog 文档，验证所有工具、注册证据路径、SQL 路径、空集合、授权交集，以及文本/视觉索引。模拟检索返回越界候选时也不能进入回答证据。

```bash
uv run --extra dev --extra observability pytest tests/test_scope_boundaries.py tests/test_tool_agent.py -q
```

## 限制与下一步

本轮结果：8 个新增范围用例通过，既有工具 Agent 测试继续通过，全量后端为 129 项通过。

这里约束的是查询选择，不是多租户身份。普通文档 API、历史 Run、SSE、父轮上下文与 Artifact 授权仍要由未来 AccessContext 统一接管。下一步让工作空间授权集合从认证主体产生，所有存储与入口执行相同过滤。此项不会扩大外部 API 权限或产生新的付费步骤。
