# A06：前端依赖审计与兼容更新

日期：2026-10-25。状态：本次发现的 high/critical 项已修复；低危项未关闭。

## 发现与处理

首次 `npm ci` 后，`npm audit --omit=dev` 报告 8 项：4 low、3 high、1 critical，涉及 Next.js、sharp、nanoid、source-map-js 及 KaTeX 依赖链。此为依赖库审计结果，不表示已验证所有漏洞路径在本应用可利用。

执行不带 `--force` 的 `npm audit fix`，在现有 package.json 兼容范围内更新锁文件。关键解析版本：Next.js 16.4.3、sharp 0.35.5、nanoid 3.3.20、source-map-js 1.2.2。未批量强制跨版本升级，也未隐藏 advisory。

## 验证与发布门禁

```bash
cd web
npm ci
npm run lint
npm run build
npm audit --omit=dev --audit-level=high
```

生产依赖审计不再包含 high/critical；CI 将这些等级作为失败条件。类型检查与生产构建用于验证当前页面及依赖 API 的兼容性，不替代浏览器视觉和交互回归。

## 已知遗留

仍有 4 项 low，来自 KaTeX advisory [GHSA-238p-pmpm-9mq7](https://github.com/advisories/GHSA-238p-pmpm-9mq7) 及 rehype-katex、micromark-extension-math、remark-math 的依赖传播。npm 建议的部分修复包含不兼容版本调整，尚未执行。

后续单独升级数学渲染链并验证公式、异常输入和信任设置。high 门禁允许现有 low 项通过，不等同于零漏洞；依赖与 advisory 会变化，每次发布重新运行审计。
