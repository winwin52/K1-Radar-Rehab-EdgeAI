# backend/ — FastAPI 后端

K1 设备的主进程。常驻运行，开机自启。

## 计划模块

```
backend/
├─ server.py              FastAPI 应用入口 + lifespan
├─ device_state.py        IDLE / WORKING 设备级状态管理
├─ session_fsm.py         会话内部状态机 (BASELINE → TRAINING → SUMMARY)
├─ coach.py               情绪 → 计划动态调整决策
├─ patient_store.py       患者档案 CRUD
├─ plan.py                Plan dataclass + 临床默认模板
├─ logger.py              JSONL 落盘
├─ audio_engine.py        sounddevice 持续输出流 + 三层混音
├─ tts_local.py           K1 本地 TTS 合成
├─ coordinator.py         监听 mp.Queue (A) + ZMQ (C) + 分发
├─ zmq_bridge.py          与 Process C 通信
│
├─ routes/                HTTP 路由
│   ├─ device.py          /api/device/*
│   ├─ patient.py         /api/patient/*
│   ├─ plan.py            /api/plan/*
│   ├─ session.py         /api/session/*
│   └─ history.py         /api/history/*
│
├─ ws/                    WebSocket
│   └─ live.py            /ws/live (10Hz 状态推送)
│
├─ llm/                   LLM 子系统 (见 llm/README.md)
├─ services/              业务用例 (见 services/README.md)
└─ workers/               异步任务 (见 workers/README.md)
```

## 设计原则

- **业务与 LLM 解耦**：业务代码只调 `LLMClient.chat()`
- **进程间消息以数据结构为主**：避免 pickle 复杂对象
- **失败不阻塞**：LLM 失败、WS 断连、雷达掉线，主流程都继续
- **可观测**：所有状态变更都有结构化日志
