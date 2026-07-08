# backend/services/ — 业务用例层

每个 service 对应一个 LLM 用例。

## 模块

```
backend/services/
├─ ai_assessment.py       康复评估报告生成 (异步, session 结束触发)
├─ encourage_gen.py       个性化鼓励语文本生成 (Coach 触发)
└─ coach_advisor.py       (空壳预留) Coach 决策辅助
```

## 使用方式

```python
from backend.services.ai_assessment import generate_assessment

# 入异步队列
await ai_queue.put(("assessment", session_id))

# Worker 调用
report_md = await generate_assessment(session_data)
```

## 失败回退

| Service | 失败回退 |
|---|---|
| ai_assessment | 入 retry_queue.jsonl，下次启动或 30 分钟后重试 |
| encourage_gen | 从 prompts/encourage_fallback.txt 随机挑一句 |
| coach_advisor | 当前不调用，规则引擎兜底 |
