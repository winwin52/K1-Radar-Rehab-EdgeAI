# backend/workers/ — 异步后台任务

## 模块

```
backend/workers/
└─ ai_worker.py           异步处理 AI 任务队列 (assessment / encourage)
                          失败入 queue/ai_retry.jsonl，30 分钟后重试
```

## 启动方式

由 `backend/server.py` 的 lifespan 启动：

```python
@asynccontextmanager
async def lifespan(app):
    ai_task = asyncio.create_task(ai_worker.run())
    yield
    ai_task.cancel()
```
