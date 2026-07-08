# backend/llm/ — LLM 子系统

## 设计目标

将"调用外部 LLM"封装成一个**子系统**，业务代码看到的只有 `chat(messages)`，
背后是抽象层 + 防护层 + 配置层 + 审计层。

## 模块

```
backend/llm/
├─ client_base.py         LLMClient 抽象接口 + ChatResult dataclass
├─ client_deepseek.py     DeepSeek 实现 (OpenAI 兼容协议)
├─ client_mock.py         Mock 实现 (开发/CI 用，固定响应)
├─ factory.py             根据 config/llm.toml 选择实现
│
├─ prompts.py             加载 prompts/*.md + Jinja2 模板渲染 + 热加载
├─ cache.py               SQLite 缓存 (audit/llm_cache.db)
├─ sanitizer.py           隐私匿名化 (姓名 → patient_id hash)
├─ audit.py               每次调用写 audit/llm_calls.jsonl
├─ budget.py              预算追踪 + 熔断
└─ retry.py               重试 / 超时 / 熔断器
```

## 核心接口

```python
class LLMClient:
    async def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> ChatResult: ...

@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cache_hit: bool
```

## 切换 provider

只改 `config/llm.toml`，不动业务代码：

```toml
[default]
provider = "deepseek"   # 改成 "mock" 即开发模式
```

## 失败语义

- 网络超时 / 5xx → 自动重试 3 次，指数退避
- 4xx → 立即失败，不重试
- 超预算 → 直接 fallback，不调网
- 全部失败 → 业务层退化到本地 fallback (如预录鼓励语)
