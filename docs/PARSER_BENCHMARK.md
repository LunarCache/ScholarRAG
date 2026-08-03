# MinerU / PaddleOCR 论文解析对比协议

## 1. 决策问题

本实验不是评选一个“看起来更漂亮的 Markdown”，而是回答四个工程问题：

1. 哪个解析器最适合作为 CS/AI 论文默认主解析器？
2. 哪个解析器适合作为失败回退或质量复核器？
3. 哪些页面特征可以可靠触发自动路由？
4. 额外解析或混合管线带来的质量收益是否值得时延和费用？

## 2. 实验组

### A. MinerU VLM

- API 模型：`vlm`；
- 开启公式和表格；
- 英文论文按英文设置；
- 保存 API response、完整 Zip、Markdown、模型 JSON、content list、layout 和图片。

### B. PaddleOCR-VL-1.6（主要对照）

- 官方托管 API 模型：`PaddleOCR-VL-1.6`；
- `use_layout_detection=true`；
- `use_chart_recognition=true`；
- `use_ocr_for_image_block=true`；
- `temperature=0`，降低重复实验随机性；
- `prettify_markdown=false`，避免美化步骤掩盖解析错误；
- `return_markdown_images=true`，保存可下载资源。

这一组本身就是官方完整“版面分析 → 元素级 VLM → 顺序合并”管线。

### C. PP-StructureV3（独立强基线）

- 官方托管 API 模型：`PP-StructureV3`；
- 开启表格、公式、图表和区域检测；
- `format_block_content=true`；
- `prettify_markdown=false`；
- `return_markdown_images=true`。

### D. PP-StructureV3 → 区域 VL（可选消融）

先用 PP-StructureV3 的 bbox 裁剪元素，再将高价值或低置信区域逐块送 VL。只有在 B/C 表明明显互补、且收益可能覆盖额外成本时实施。必须记录裁剪坐标变换、区域调用次数、并发、失败和总费用；不得把这组结果当作官方默认 PaddleOCR-VL 管线。

## 3. 语料与抽样

### 首轮工程验证

- `paper/MTGR.pdf` 全部 8 页；
- 三组使用同一源文件和全部页面；
- MinerU 复用已经保存的完成批次，Paddle 两组各调用一次；
- 目标是验证 artifact、统计口径和报告生成，不据此宣布总体胜负。

### 正式 gold set

从现有 7 篇论文分层抽样，至少覆盖：

- 标题、作者、摘要与目录结构；
- 单栏/双栏正文和跨栏阅读顺序；
- 行内公式、独立公式、公式编号和长推导；
- 有线/无线表格、跨页表格、复杂表头；
- 图片、流程图、架构图、曲线图及图注；
- 伪代码、代码片段、脚注、页眉页脚和参考文献；
- 扫描页、低分辨率、字体嵌入异常等困难页面（若当前语料不存在则补充）。

建议第一批 30–50 个页面，每类至少 5 个有效样本；随后扩展到 100+ 页面。标注人员在不知道解析器来源的情况下评分。

## 4. Gold 数据

每个 gold page 保存：

- 页面尺寸和渲染图 hash；
- 元素类型、bbox/polygon 和阅读顺序；
- 标题层级；
- 正文规范文本；
- 公式规范 LaTeX（保留等价形式规则）；
- 表格 HTML/单元格网格及跨行跨列；
- 图片区域、图注和关联关系；
- 不应进入正文的页眉、页脚、页码和装饰元素；
- 人工 QA 问题、正确证据区域和参考答案。

PDF text layer 只能作为弱参考，不能直接充当 gold：它本身可能缺字、乱序或无法表达表格、公式和图。

## 5. 指标

### 5.1 Artifact 完整性

- 目标页数、返回页数、空页数和失败页数；
- 原始响应、结构 JSON、Markdown、图片和可视化资源是否保存；
- job/batch ID、模型、选项、SDK 版本、时间和 trace 是否齐全；
- 所有 canonical block 是否能回到 raw JSON Pointer；
- 资源 URL 过期后，本地 artifact 是否仍可独立复核。

### 5.2 版面与结构

- 元素类别 precision/recall/F1；
- bbox IoU、polygon IoU 和检测 mAP；
- 阅读顺序 Kendall tau / pairwise accuracy；
- 标题层级 accuracy / tree edit distance；
- 页眉页脚误入率；
- 图与图注、表与表注的关联 F1。

### 5.3 内容保真

- 正文 CER/WER 和段落边界 F1；
- 公式 normalized edit distance、符号准确率和编号关联率；
- 表格 TEDS、cell F1、row/column span accuracy；
- 图片/图表召回率、裁剪完整率和 OCR/VL 描述保真度；
- 丢失、重复和幻觉字符/块的比例。

### 5.4 下游 RAG

- chunk 数、平均长度、跨节污染率和重建稳定性；
- retrieval recall@k、MRR、nDCG、证据区域命中率；
- Ragas context precision、context recall、faithfulness、answer relevancy、factual correctness；
- claim citation coverage、citation correctness、citation completeness；
- 不可回答问题的拒答准确率。

### 5.5 运行指标

- 成功率、重试率、错误类型；
- 端到端 latency、每页 latency、P50/P95；
- 每页/每篇成本与免费额度消耗；
- 下载字节、artifact 大小、峰值内存；
- 同一输入重复运行的一致性。

## 6. 公平性约束

1. 使用完全相同的 PDF 字节和页面范围；
2. 固定并记录模型版本、SDK 版本、所有非默认选项和随机参数；
3. 原始结果先冻结，再运行 provider-specific normalizer；
4. 不允许只为某一解析器手工修复输出；
5. canonical schema 和评分程序使用同一版本；
6. 每项指标报告样本数和置信区间，不只给平均值；
7. 质量、时延和成本分别报告，再讨论 Pareto 前沿；
8. 供应商自报 benchmark 仅作背景，最终决策以本项目语料为准。

## 7. 产物结构

```text
data/benchmarks/parsers/{run_id}/
├── manifest.json
├── variants/
│   ├── mineru-vlm/
│   │   └── stats.json
│   ├── paddleocr-vl-1.6/
│   │   ├── raw/provider_response.json
│   │   ├── resources/
│   │   ├── canonical/document.json
│   │   ├── chunks/chunks.json
│   │   └── stats.json
│   └── pp-structure-v3/
│       ├── raw/provider_response.json
│       ├── resources/
│       ├── canonical/document.json
│       ├── chunks/chunks.json
│       └── stats.json
├── rag-evaluation/             # 每个解析器独立 SQLite/Qdrant/Ragas artifact
├── report.json
└── report.md
```

原始输出不可覆盖。报告必须包含 source SHA-256、页范围、模型、选项、provider job ID、开始/结束时间、时延、程序版本和错误信息。

## 8. 决策规则

- **默认主解析器**：在正文、阅读顺序、公式、表格、图注和下游 QA 上达到门槛，且位于质量—时延—成本 Pareto 前沿；
- **自动回退器**：与主解析器错误互补，在困难页面上有稳定增益；
- **双解析触发**：页缺失/空白、字符密度异常、乱码、块数骤变、表格/公式检测冲突、标题树异常或解析器分歧超过阈值；
- **人工复核**：两路均低置信、关键公式/表格冲突或引用证据无法精确定位；
- **混合管线**：只有在 gold set 上取得显著增益且额外费用/时延在预算内才进入产品路径。

首轮报告只验证管线和发现候选问题；主解析器选择必须等正式 gold set 完成后作出。
