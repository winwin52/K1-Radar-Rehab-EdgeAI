# realtime3.0 — 雷达情绪闭环康复系统

> 面向半月板损伤的雷达无感监测与边缘情绪闭环智能康复系统。

## 架构

```
手机/电脑浏览器  ──HTTP+WS──►  K1 (后端 + 屏幕 + 音频 + 感知)
                                         │
                                    ──SPI──► 雷达 MRS6240
                                         │
                                    ──HTTPS──► DeepSeek API (异步)
```

K1 是嵌入式设备，默认 IDLE 待机，由网页发指令进入 WORKING。

## 进程结构

| 进程 | 文件 | 职责 |
|---|---|---|
| A: 感知 | `rehab_engine.py` (包装在 `rehab_engine_proc.py`) | SPI 10Hz + 特征 + 情绪推理 |
| B: 后端 | `backend/server.py` | FastAPI + Coordinator + Coach + Audio + AI |
| C: 屏幕 | `screen/app.py` | pygame HDMI 渲染 + 触摸 |

进程间通信：A↔B `mp.Queue`，B↔C `ZMQ`，B↔Web `HTTP/WS`。

## 目录约定

| 路径 | 说明 |
|---|---|
| `rehab_engine.py` | 感知核心（v3.0 双线程版，已 commit 21ac6f3） |
| `feature_extractor.py` | 特征提取 |
| `model.pkl` | 已训练情绪分类模型（29 特征） |
| `collect_realtime_v3.py` | 独立 CLI 工具（无 GUI） |
| `backend/` | FastAPI 后端 + LLM 子系统 |
| `screen/` | pygame HDMI 屏幕渲染 |
| `webapp/` | Vue 3 单文件网页前端（K1 自托管） |
| `tools/` | Windows 端离线工具（音频预渲染等） |
| `config/` | 参数 + 密钥（secrets.env 不入 git） |
| `prompts/` | LLM prompt 文件（版本化 .md） |
| `audit/` | 运行期：LLM 调用审计日志 |
| `queue/` | 运行期：AI 失败重试队列 |
| `patients/` | 运行期：患者档案与历史 session |
| `assets/` | 运行期：预渲染音频素材 |

## 数据归属

K1 本地 `patients/<姓名>/sessions/<id>/` 是唯一真理源。网页前端只是窗口，每次显示都向 K1 拉取。

## 状态

- **Phase 0 完成**：旧 GUI 已删除，新骨架已建立
- **进行中**：Phase 1 - 嵌入式骨架（IDLE/WORKING）

详见 `D:/A_the_game/dev_log.md`。
