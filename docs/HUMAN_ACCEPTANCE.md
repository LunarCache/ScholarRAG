# 人工 Gold 与 Judge 校准操作手册

自动化可以生成候选、校验格式和链接证据，但不能替代盲标和独立复核。本手册定义剩余的人工作业和可审计完成条件。

启动应用后点击顶栏“验收工作台”。顶部三个计数分别对应 Parser `42/42`、QA 至少 `30 reviewed` 且全部 38 条已处置、Judge `30/30` 双评分；只有三者同时通过才显示 `ALL HUMAN GATES PASSED`。

## 1. Parser gold 双人流程

1. 标注者只查看 `evals/parser-gold/rendered/{sample_id}.png` 和 source PDF，不查看解析器名称或输出。
2. 在 Parser Gold 页选择元素类型并在原图拖拽 bbox；工作台生成 `normalized_0_1000` 坐标。继续补充阅读顺序、标题层级、公式 LaTeX、表格 HTML、chart 数据和 caption 关联。
3. 填写 `annotation.annotator`、`annotated_at`，将状态改为 `annotated`。
4. 第二名人员对照原页独立复核；修订完成后填写不同的 `reviewer`、`reviewed_at`，将状态改为 `reviewed`。
5. 若 `annotator == reviewer`、缺少时间、页面 hash 不符或 schema 校验失败，正式 benchmark 必须拒绝该页。
6. reviewed 页面必须至少包含一个元素；重复 ID/reading order、零面积 bbox、失效 caption/parent 引用和 QA 引用未知元素都会被门禁拒绝。

准备/重跑候选页：

```bash
uv run scholarrag prepare-parser-gold evals/parser-gold/manifest.json --paper-dir paper
```

审计全部 42 页；未全部双人复核时命令返回退出码 2，CI/正式验收必须据此失败：

```bash
uv run scholarrag audit-parser-gold evals/parser-gold/manifest.json
```

当前基线：`proposed=42`、`annotated=0`、`reviewed=0`、`gold_ready=false`。

## 2. QA 候选与 EasyDataset

`evals/initial-42-candidates.jsonl` 当前 38 条均是 `candidate`，不是 gold。推荐把 `evals/easydataset-contexts.jsonl` 导入独立 EasyDataset 工作台，在其中编辑问题/参考答案，然后导出 JSON/JSONL：

```bash
uv run scholarrag import-easydataset <easydataset-export.jsonl> evals/reviewed-import.jsonl
uv run scholarrag link-eval-dataset evals/reviewed-import.jsonl
```

导入后仍保持 `candidate`。可在内置 QA 页逐条检查：问题是否清晰、参考答案是否回答问题、active chunk 原文是否支持答案、原页是否一致、document/page/chunk/block 是否匹配。`reviewed` 必须链接真实 active chunk，伪造或过期 ID 会被拒绝；3 条未链接样例必须修复或明确 reject。当前自动生成集中有明显错误样例（参考答案可能抽到页眉/arXiv 行），因此禁止整批自动晋升。

EasyDataset 使用 AGPL-3.0；ScholarRAG 只通过 JSONL 边界与外部工作台交互，不复制或嵌入其代码。

另有 `evals/qwen3.6-27b-synthetic.jsonl`：它使用 `Qwen/Qwen3.6-27B`、`temperature=0`、`enable_thinking=true` 从相同的 42 个候选页重新生成，42/42 已绑定 active chunk/block。该文件只用于当前无法人工标注阶段的 synthetic 评测；即使格式、原文引文和链接全部通过自动校验，也不能计入 reviewed/gold 门禁。

## 3. Ragas Judge 人工校准

1. 从 reviewed QA 分层抽取至少 30 条，覆盖事实、方法、公式、表格、图、跨论文和不可回答。
2. 至少两名评分者在不知道 Judge 分数的情况下，对 faithfulness、answer relevancy 和 citation correctness 独立打分。
3. 保存评分者、时间、评分 rubric、争议仲裁和 Query Run ID。
4. 计算评分者一致性、Judge 与人工的 Spearman/Pearson 相关性、分层均值偏差和 false-pass 样例。
5. 只在校准结果可接受后设置回归阈值；模型、prompt 或上下文策略变化后重新校准。

内置 Judge 页在提交前隐藏自动分数以降低锚定偏差；每个 run 按评分者保留最新版，同时原始提交保持追加式记录。工作台自动计算 Pearson、Spearman 与 Judge-human 均值偏差。

Judge 固定为 `Qwen/Qwen3.6-27B`，与生成模型统一；上下文窗口记录为 262,144 tokens，客户端不发送任何输出 token 上限，并设置 `enable_thinking=true`。批量评测会因此更慢，但保留模型完整推理能力。

## 4. 最终门禁

```bash
uv run scholarrag audit-human-gates
```

命令会在 `data/evaluations/human-review/gate_*.json` 冻结 Parser manifest、QA 数据、Judge ratings 和审计日志的 SHA-256。任一人类门禁未通过时退出码为 2。当前基线 artifact 正确记录 `human_gate_ready=false`，不得绕过。
