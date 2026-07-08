# 面向半月板损伤的雷达无感监测与边缘情绪闭环智能康复系统

> **Radar-Based Contactless Monitoring & Edge Emotion Closed-Loop Intelligent Rehabilitation System for Meniscus Injury**

[![RISC-V](https://img.shields.io/badge/CPU-RISC--V%20K1%20(8--core)-blue)](https://www.spacemit.com)
[![Edge AI](https://img.shields.io/badge/AI-Edge%202.0%20TOPS-green)](https://www.spacemit.com)
[![Python](https://img.shields.io/badge/Python-3.10+-yellow)](https://www.python.org)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

## 📋 竞赛信息

| 项目 | 内容 |
|------|------|
| **竞赛名称** | 全国大学生嵌入式芯片与系统设计竞赛 2026 |
| **赛道** | 芯片应用赛道 — 进迭时空赛题 |
| **选题方向** | 选题二：边缘 AI 应用 |
| **硬件平台** | 进迭时空 K1 MUSE Pi Pro (RISC-V 8核, 2.0 TOPS AI) |
| **雷达模组** | MRS6240_P2512 (2T4R MIMO 毫米波雷达) |

---

## 🎯 项目概述

### 背景与问题

半月板损伤是常见的膝关节损伤，术后康复需要长期、规律的坐姿抬腿训练。然而：

- ❌ 康复动作**单调枯燥**，患者难以坚持
- ❌ 传统穿戴式传感器**束缚感强**，依从性差
- ❌ 缺乏**实时情绪感知**与个性化反馈

> **核心目标：提高康复人员的康复依从性，让患者"明天还愿意再来"。**

### 解决方案

```
毫米波雷达无感监测 → 生理参数(呼吸/心跳/姿态) → 边缘情绪计算 → 康复计划调整/正面反馈 → 依从性提升
       ↑                                                                              ↓
       └────────────────────────── 闭环：检测反馈效果 ←─────────────────────────────────┘
```

- **无感监测**：毫米波雷达非接触感知呼吸、心率、HRV、腿部颤抖、抬腿姿态
- **边缘情绪计算**：端侧 RandomForest 模型实时分类 3 类情绪（平静/沮丧/愉悦）
- **闭环反馈**：根据情绪状态智能调整康复计划，通过 HDMI 像素山岳界面 + 蓝牙音频给予正面激励

---

## 🏗️ 系统架构

### 硬件链路

```
MRS6240 毫米波雷达 ──SPI(56MHz)──► K1 MUSE Pi Pro ──HDMI──► 显示器(山岳界面)
                                         │
                                         ├──WiFi──► 手机浏览器(控制面板)
                                         │
                                         └──3.5mm──► 蓝牙耳机(A2DP 音频反馈)
```

### 软件架构

| 进程 | 文件 | 职责 |
|------|------|------|
| **感知引擎** | `rehab_engine.py` | SPI 数据采集(10Hz) + 特征提取 + 情绪推理 |
| **后端服务** | `backend/server.py` | FastAPI + Coordinator + Coach + Audio + LLM |
| **HDMI 屏幕** | `screen/app.py` | pygame 像素山岳渲染 + 登山进度可视化 |
| **网页前端** | `webapp/` | Vue 3 SPA — 患者管理 + 实时监测 + 共情选择 |

**进程间通信**：感知↔后端 `mp.Queue`，后端↔屏幕 `ZMQ`，后端↔网页 `HTTP/WebSocket`

---

## 🔬 核心技术

### 1. 毫米波雷达生命体征感知

| 检测指标 | 信号源 | 方法 | 精度 |
|----------|--------|------|------|
| 呼吸频率 | 1D FFT 相位 (gain_factor) | 三频带滤波 + 谐波验证 | 12-20 BPM |
| 心率 | 微动点云质心轨迹 | 带通滤波 + FFT | 60-100 BPM |
| HRV | 心跳间隔序列 | RMSSD / SDNN | - |
| 抬腿姿态 | 运动点云 + 目标跟踪 | 速度矢量分析 | 次数/幅度/速度 |
| 腿部颤抖 | 微动点云高频分量 | 频带能量比 | 疲劳指标 |

### 2. 边缘情绪分类

- **模型**：RandomForest (29 维特征，含呼吸域/运动域/微动域/跨模态/时序)
- **推理平台**：K1 CPU（端侧推理，无需云端）
- **分类类别**：calm(平静) / frustration(沮丧) / pleasure(愉悦)
- **验证方法**：留一被试交叉验证 (LOSO)，加权 F1 = 0.75
- **后处理**：ThresholdClassifier + Sticky 状态机（连续 3 次一致才切换）

### 3. 山岳征途 — 游戏化依从性设计

以"攀登 6000 米高山"为隐喻，将康复训练转化为登山征途：

- 🏔️ **像素山岳 HDMI 界面**：康复人员看到登山进度、海拔、连续天数
- 🧗 **登山小人**：每完成一次抬腿 = 前进 5 米
- 🎭 **情绪感知反馈**：沮丧时同伴鼓励，愉悦时庆祝
- 📱 **手机控制面板**：创建患者、查看数据、共情选择

### 4. LLM 智能康复教练

- **模型**：DeepSeek API（异步调用，七层防护容错）
- **功能**：session 结束后生成 AI 评估 + 个性化鼓励语
- **审计**：所有 LLM 调用记录可追溯

### 5. 实时雷达健康诊断 (Phase 10)

- 帧率监控 (avg_fps, slow_loops)
- SPI 超时/丢帧检测
- 推理延迟追踪
- 队列积压预警
- 三层 stale 标记：frame stale (3s) / inference stale (6s)

---

## 📂 目录结构

```
K1-Radar-Rehab-EdgeAI/
├── README.md                   # 项目说明（本文件）
├── LICENSE                     # MIT 开源协议
├── .gitignore
│
├── docs/                       # 设计文档
│   └── 设计报告.pdf             # 完整设计报告（26页）
│
├── src/                        # 核心源代码 (realtime3.0)
│   ├── rehab_engine.py         # 感知引擎：SPI + 特征 + 情绪推理
│   ├── feature_extractor.py    # 52维特征提取
│   ├── model.pkl               # RandomForest 情绪分类模型
│   ├── collect_realtime_v3.py  # CLI 数据采集工具
│   ├── requirements.txt        # Python 依赖
│   ├── backend/                # FastAPI 后端 + Coordinator + LLM
│   │   ├── server.py           # FastAPI 主服务
│   │   ├── coordinator.py      # 会话协调整合
│   │   ├── sensing_proc.py     # 感知进程管理
│   │   ├── coach.py            # 康复教练逻辑
│   │   └── audio_engine.py     # 音频引擎
│   ├── screen/                 # pygame HDMI 像素山岳渲染
│   │   └── app.py              # 屏幕主程序
│   ├── webapp/                 # Vue 3 网页前端
│   │   ├── index.html          # 单页入口
│   │   ├── app.js              # 前端逻辑
│   │   └── style.css           # 样式
│   ├── prompts/                # LLM prompt 模版（.md）
│   ├── config/                 # 配置文件
│   ├── tools/                  # 部署/调试工具
│   │   ├── deploy.sh           # 一键部署到 K1
│   │   ├── k1_start.sh         # K1 启动脚本
│   │   └── check_env.sh        # 环境检查
│   └── assets/                 # 音频素材
│       ├── ambient_calm.wav    # 平静环境音
│       ├── ambient_pleasure.wav
│       ├── ambient_frustration.wav
│       └── cue_*.wav           # 动作指令音效
│
├── figures/                    # 系统截图与实物照片
│   ├── hdmi/                   # HDMI 屏幕截图（待机/平静/沮丧）
│   ├── web/                    # Web 控制台截图（7页）
│   └── hardware/               # 硬件实物照片（整机/SPI连接/K1/雷达）
│
├── demo/                       # 演示视频（待补充）
│   └── .gitkeep
│
└── firmware/                   # 雷达固件源码
    ├── README.md                # 固件说明
    └── app/                     # 固件应用层源码
        ├── inc/                 # 头文件 (prj_config, 1d/2d dsp, msg_handler)
        └── src/                 # 源文件 (main, 1d/2d dsp, debug_tool, msg_handler)
```

---

## 🚀 快速开始

### 环境要求

- **硬件**：进迭时空 K1 MUSE Pi Pro + MRS6240 雷达模组
- **系统**：Bianbu OS (Ubuntu 22.04 based)
- **Python**：3.10+
- **依赖**：见 `src/requirements.txt`

### 部署到 K1

```bash
# Windows 端执行（一键部署）
bash src/tools/deploy.sh

# 或手动部署
scp -r src/ winwin51@<K1_IP>:/home/winwin51/radar_r/realtime3.0/
```

### 启动系统

```bash
# SSH 到 K1
ssh winwin51@<K1_IP>

# 启动后端（真实雷达模式）
cd /home/winwin51/radar_r/realtime3.0
python3 -m backend.server

# 启动 HDMI 屏幕
SDL_VIDEODRIVER=x11 python3 -m screen.app --fullscreen

# Mock 模式（无雷达时调试用）
REHAB_MOCK_SENSING=1 python3 -m backend.server
```

### 访问控制面板

- 手机/电脑浏览器打开：`http://<K1_IP>:8000/`
- API 文档：`http://<K1_IP>:8000/docs`

---

## 🔧 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 雷达帧率 | 8-10 fps | SPI 56MHz, burst=3 |
| FFT 分辨率 | 0.249 BPM/帧 | 4805 帧 / 240s |
| 呼吸正常范围 | 12-20 BPM | 医学标准 |
| 心率正常范围 | 60-100 BPM | 医学标准 |
| 情绪分类数 | 3 类 | RandomForest 29特征 |
| Baseline 时长 | 60s | 个性化基线采集 |
| Frame Stale | 3.0s | 雷达数据暂停阈值 |
| Inference Stale | 6.0s | 推理暂停阈值 |
| 音频采样率 | 44100 Hz | 三层混音输出 |

---

## 🏆 创新点

1. **无感监测**：毫米波雷达非接触感知，解决穿戴式设备束缚问题
2. **边缘情绪闭环**：端侧推理 6 类情绪，实时调整康复策略，无需上云
3. **游戏化依从性设计**：山岳征途隐喻 + 像素艺术风格 + 同伴型话术
4. **全栈 RISC-V 边缘 AI**：K1 单板承载感知→推理→反馈→展示全链路
5. **诊断可观测性**：三层 stale 检测 + 实时健康诊断 API
6. **多模态反馈**：HDMI 视觉 + 蓝牙音频 + 手机网页三通道协同

---

## 📝 待补充

- [ ] 演示视频（`demo/`）
- [ ] 团队成员信息

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE)。

---

## 🙏 致谢

- 竞赛平台：进迭时空 K1 MUSE Pi Pro
- 雷达模组：MRS6240 (2T4R MIMO)
- LLM 服务：DeepSeek API
- 中文字体：Noto Sans CJK
