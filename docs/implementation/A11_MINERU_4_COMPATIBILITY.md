# A11：MinerU 4.0 与现有云端 API 的兼容性检查

核对日期：2026-10-25。状态：已核对官方资料并补充现有协议适配和离线契约测试；未调用真实解析服务。

## 1. 结论

**MinerU 4.x 已发布，但不能将其产品版本号直接当作项目现有 API 或 model_version。** 当前项目接入官方云端精准解析 API，沿用 `/api/v4`、`vlm/pipeline`、上传后轮询、结果 ZIP 的流程有官方文档依据；不需要仅因产品升级而改成 `/api/v1` 或 `model_version="4.0"`。[云端 API 文档](https://mineru.net/doc/docs/index_en/)

自部署 MinerU 4.x 的 V1 服务属于另一种协议，迁移时需要独立 Client、结果归一化和验收，不能只替换 Base URL。[官方 4.0 迁移指南](https://opendatalab.github.io/MinerU/reference/migration_4/)

检查时官方发布页列出 MinerU 4.0.12；该发布包含自部署 V1 的便利解析入口等变更。这不证明当前云端账号后端也恰好运行 4.0.12。[官方发布记录](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.12-released)

## 2. 项目现状与比对

| 项目代码 | 当前判断 | 处理 |
| --- | --- | --- |
| MinerUClient 的 `/file-urls/batch` | 属于云端 v4 链路 | 保留 |
| 获取上传 URL 后 PUT，自动开始解析 | 与云端流程一致 | 保留，隔离签名 URL 与 API 凭据 |
| `/extract-results/batch/{batch_id}` | 云端 v4 查询方式 | 保留，检查响应结构和状态 |
| model_version 默认 vlm | 云端 PDF 选择，不是软件版本 | 限定 PDF 适配为 vlm/pipeline |
| content_list_v2 + layout + images | 当前 Normalizer 的输入契约 | 保留，增加嵌套目录处理与未知格式拒绝 |
| pyproject.toml 没有 mineru 本地包依赖 | 当前是 HTTP 集成 | 不安装或盲目升级本地 MinerU 包 |

以上对照证明代码与已核对文档的契约方向一致，不是实际云端连通或解析质量的验证。

## 3. 4.x 独立适配的边界

4.x V1 使用上传、解析任务和结果文件资源，并有 `tier` 与不同终态；不能把旧 batch_id 或 done 状态套入新协议。具体能力以目标服务发现及 OpenAPI 为准。[V1 HTTP API](https://opendatalab.github.io/MinerU/usage/http_api/)

原生 4.x ZIP 可包含 middle_json.json、structured_content.json、markdown.md 和素材；新版共享中间结构有明确的 schema 身份，与旧 pdf_info 不是同一契约。现有 Content List V2 归一化器不能宣称原生结果兼容。[输出格式契约](https://opendatalab.github.io/MinerU/reference/output_files/)

本次不实现或启用自部署 V1，不改变供应商或自动重新解析旧论文。需要接入时应：

1. 新建独立 Provider 与配置档，发现目标服务能力，而不是复用 Cloud Client 的请求体。
2. 定义 V1 任务、部分成功、取消、下载和重启边界，结合 ScholarRAG 的持久化任务实现恢复。
3. 依据真实目标 ZIP 构建新 Normalizer，验证原始页号、尺寸、坐标、公式、表格、标题与图片资源。
4. 保留原始产物和实际 producer 版本，固定语料对比后再选择性重建；原有解析和引用不覆盖。

## 4. 本次代码改动

- MinerUClient 明确标注为 Hosted Precision v4，Base URL 明显指向 `/v1` 时给出独立适配器提示。
- Settings 的 mineru_model 限定为本项目 PDF 支持的 vlm/pipeline，误填 4.0 在配置阶段失败。项目仍仅支持 PDF 摄取，不因云端支持 HTML 就自动扩展产品范围。
- 上传 URL 响应校验 batch_id、URL 数量和内容，异常响应不会继续 PUT；查询响应校验 extract_result，未知任务状态拒绝误判为完成。
- 签名上传/下载请求移除继承的 Authorization、Cookie、Proxy-Authorization，避免注入共享 HTTP Client 时把凭据发给素材域名；控制 API 显式使用 MinerU Bearer，不被注入 Client 的默认认证覆盖。
- 保留云端契约内的 waiting-file、pending、running、converting、done、failed 处理。
- Content List V2 支持 ZIP 内嵌套目录，图片和 layout 相对所在结果目录解析。只有原生 middle_json 的结果明确拒绝，不伪造成功。
- 归一化或候选准备失败也记录失败 manifest，并保留已下载 raw artifacts，便于检查供应商结果变化。

## 5. 配置

```dotenv
SCHOLARRAG_MINERU_BASE_URL=https://mineru.net/api/v4
SCHOLARRAG_MINERU_MODEL=vlm
SCHOLARRAG_MINERU_LANGUAGE=en
SCHOLARRAG_MINERU_IS_OCR=false
```

语言与扫描件 OCR 开关现在可配置；处理中文测试资料可设 language=ch，需要 OCR 时设 is_ocr=true。这两项属于现有云端协议参数，不是本次发现的 4.x 新参数，实际模型行为仍需目标账号验证。

## 6. 验收与限制

本轮结果：11 项 MinerU 契约测试通过，完整后端 175 项通过，Ruff 与格式检查通过。没有进行在线解析或自部署 4.x 实测。

tests/test_mineru_contract.py 的 11 个离线用例验证提交、签名上传、轮询、ZIP 下载、规范化完整链路；覆盖嵌套素材、参数传递、凭据不外发、错误响应、失败状态、错误版本配置、原生新格式拒绝及失败 manifest/raw 保留。只使用手写合成响应和 MockTransport，不是厂商真实样例或线上兼容认证。

```bash
uv run --extra dev --extra observability pytest tests/test_mineru_contract.py -q
```

未提交真实论文、未消耗解析额度、未读取用户 Key。当前仍需真实账号小样本测试确认云端实际输出、配额和版本信息；大型下载/解压资源预算、原始页号映射及自部署 V1 适配属于后续独立改造。
