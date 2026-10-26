# A01：证据状态与原文定位

日期：2026-10-25。状态：本项已实现；语义验证器尚未实现。

## 问题与结果

原来 `supported=bool(spans)` 把“找到了引用片段”表达成“证据支持结论”。现在引用存在只产生 `evidence_located=true`，语义状态默认 `not_checked`；前端显示“已定位引用 · 支持性未验证”。不相关证据和矛盾数字都不会仅凭 `[C1]` 被标为支持。

## 实现

- `domain/models.py`：Claim 增加 `evidence_located` 与 `support_status`；保留 `supported` 兼容字段，其含义收紧为明确语义支持且存在证据。
- `support_status` 预留 `not_checked / supported / contradicted / insufficient`；当前生成流程不自动写入后三种校验结论。
- 旧回答缺少新状态时，反序列化为 `not_checked`，不沿用旧 `supported=true` 作语义判断。显式 supported 但没有证据时归一化为 insufficient。
- `ingestion/chunker.py`：生成真实 `block_offsets`，记录 Chunk 中每个 Block 的字符起止位置，覆盖 Markdown 与 Block 内部空行。
- `rag/service.py`：引用片段带 parse_id 和 `location_status`，按映射定位 Block；旧多 Block Chunk 无映射时标 approximate，不再猜测精确 bbox/Block。旧页码只可作范围提示。
- `web/app/page.tsx`：显示新的状态；原有引用展开和问答接口继续使用。

## 兼容性与数据

无需数据库 DDL 迁移；新 Block 映射保存在已有 Chunk metadata 字段。新摄取立即具备映射，旧数据可在后续版本化重建时补齐；本项没有触发付费重解析或全库重建。旧 `supported` 字段的语义有意修正，外部客户端应改用新字段。

## 验收

`tests/test_claim_states.py` 与现有引用测试覆盖正确数字、矛盾数字、不相关引用、无引用、无效 ID、旧布尔状态，以及 Block 内空行导致的定位偏移。固定合成样例在 `tests/fixtures/claim_cases.json`，明确标为非真人 Gold。

```bash
uv run --extra dev --extra observability pytest tests/test_claim_states.py tests/test_pipeline.py -q
```

验收标准：有效引用不等同于支持；矛盾数字不会显示支持；带映射时原文切片与 quote 一致并落在正确 Block；旧多 Block 数据缺少映射时不伪造精确定位。

结果：8 个新增状态与定位测试通过，包含在本轮 129 项全通过的后端回归中；Web 类型检查及生产构建通过。

## 限制与下一步

词项重合仍只用于挑选候选片段，无法判定语义蕴含、实验口径或数值正确性。下一步建立带人工标签的 Claim 验证集，再实现独立验证、补检索和结论收缩。此项不增加模型费用。
