# A04：Provider 共享并发与排队超时

日期：2026-10-25。状态：单进程并发基础已实现；分布式配额与完整费用预算待后续交付。

## 问题与实现

原来每个 SiliconFlowClient 有独立 Semaphore，多个问答各自创建客户端时，总并发可以超过配置。现在同一事件循环内，相同 endpoint、凭据摘要和并发配置的客户端共享容量。

- 非流式请求的每次 HTTP 尝试占一个槽；退避等待不占槽。
- 流式请求从发送到退出流上下文持续占槽，不能在收到 Header 后提前释放。
- 等待槽位有超时；取消与失败释放已获取的槽。
- 使用弱引用注册表，避免将 asyncio Semaphore 跨事件循环共享，注册表不保存原始密钥。
- HTTP 客户端所有权规则不变，调用方注入的 AsyncClient 不被擅自关闭。

## 配置

| 配置 | 默认值 |
| --- | --- |
| SCHOLARRAG_PROVIDER_MAX_CONCURRENCY | 8 |
| SCHOLARRAG_PROVIDER_QUEUE_TIMEOUT_SECONDS | 30 |

同一服务应统一使用相同 Provider 配置；不同凭据或不同并发配置的实例属于不同容量池。本机制不是供应商整个账户的全局硬配额。

## 验收

`tests/test_provider_capacity.py` 验证多个客户端共享一个槽、流式请求阻挡其他请求、退出后继续、排队超时和等待取消不会泄露容量。真实 SDK 的遥测测试继续验证并发 Trace 不串联。

```bash
uv run --extra dev --extra observability pytest tests/test_provider_capacity.py tests/test_observability.py -q
```

## 限制与下一步

本轮结果：2 个新增容量测试和 9 个既有 Langfuse 测试均通过，完整后端 129 项通过。HTTP 使用内存模拟，没有供应商费用。

此项未实现跨进程/跨实例协调、RPM/TPM 限额、在线任务优先级、全服务排队数量上限、费用预留或 Run 总预算；进程内的等待槽超时不能替代这些机制。现有熔断仍需后续按供应商/凭据/模型隔离。

下一步建立统一 Provider 调度服务及 Run 预算上下文，在每次重试与工具调用前检查剩余时间、Token 和费用，并将不同工作空间纳入公平调度。共享并发本身不执行额外付费调用。
