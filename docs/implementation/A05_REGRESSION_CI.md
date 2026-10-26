# A05：离线回归基线与 CI

日期：2026-10-25。状态：本地验证通过，Actions 配置已添加；未推送触发远端执行。

## 实施结果

1. `tests/conftest.py` 在测试收集前关闭私有 `.env` 读取并设置假凭据，防止导入默认 FastAPI app 时初始化真实遥测。测试期间禁止真实 socket 连接，Provider 使用 MockTransport。
2. Settings 开启 populate_by_name，修复带环境变量别名的字段通过 Python 字段名传入时被忽略的问题；环境变量别名继续可用。
3. 将公共 Parser Gold Schema 加入 `evaluation/schemas/parser_annotation.schema.json`，随代码包分发；prepare、audit 与人工审核在无本地自定义 Schema 时使用公共版本，保留自定义覆盖入口。
4. 人工验收冻结清单额外记录所用 Schema 的 SHA-256。真实标注、样本和论文仍保留在 gitignore 的本地 evals 目录。
5. 新增固定合成 Claim 样例与范围、上传、容量回归测试。原有依赖缺失本地 Schema 的失败已修复，不通过跳过测试隐藏问题。
6. 新增 `.github/workflows/ci.yml`：后端依赖锁定安装、Ruff、格式和测试；Web 干净安装、TypeScript、生产构建，以及 high/critical 审计门禁。

## 验证

命令见[实施索引](README.md)。所有测试离线执行，不需要真实供应商 Key。Langfuse 测试使用内存 exporter；CI 不默认安装或执行真人数据评测。

新增 Schema 校验结构与格式；正面积、元素关联与双人复核等语义约束继续由现有 ParserGoldPreparer 和 HumanReviewService 执行。公共 Schema 不代表任何标注已通过人工审核。

## 兼容性与限制

本轮结果：Ruff 与全量格式检查通过，129 项后端测试通过；Web 类型检查与生产构建通过。之前缺失 Schema 的人工验收测试已实际通过。远端 Actions 尚未运行。

本地自定义 Schema 仍优先，因此不同部署的约束可能不同，应在正式评测中保留哈希并审查版本。CI 测试不能证明真实供应商质量、生产吞吐或浏览器交互已完整验收。

固定合成样例仅防止“引用存在被误标为语义支持”等回归，没有真人 Gold 标签，不能据此发布准确率。后续需要独立论文测试集、浏览器端到端、迁移回滚、故障注入与发布质量门槛。
