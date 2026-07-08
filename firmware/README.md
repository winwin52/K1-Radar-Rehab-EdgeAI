# 雷达固件 (r3_databox_vs_pose_full_v2)

> MRS6240 毫米波雷达模组固件，基于 RS6x_7x_mmWave_sdk V2.0.6

## 固件功能

本固件运行于 MRS6240 (RS6240 芯片) 雷达模组，实现以下处理管线：

- **1D FFT**：256 点距离像（0.15–3.0 m 检测范围）
- **CFAR**：恒虚警率目标检测
- **运动点云**：每帧最多 16 个目标（三维坐标 + 径向速度 + 信噪比）
- **微动点云**：亚毫米级微动检测（胸腔位移 + 肌肉微颤）
- **SPI 输出**：通过 HIF 协议以 ~10 fps 帧率输出数据至 K1

## 文件结构

```
firmware/
└── app/
    ├── inc/                        # 头文件
    │   ├── prj_config.h            # 项目配置
    │   ├── r3_databox_1d_dsp.h     # 1D FFT 距离像处理
    │   ├── r3_databox_2d_dsp.h     # 2D 点云生成（运动+微动）
    │   ├── r3_databox_debug_tool.h # 调试工具
    │   └── r3_databox_msg_handler.h # HIF 消息处理（SPI 通信协议）
    └── src/                        # 源文件
        ├── main.c                  # 固件入口
        ├── r3_databox_1d_dsp.c     # 1D FFT + 相位提取
        ├── r3_databox_2d_dsp.c     # 运动点云 + 微动点云生成
        ├── r3_databox_debug_tool.c # CSV 输出 + 调试协议
        └── r3_databox_msg_handler.c # SPI 帧封装 + HIF 协议

## 编译环境

- SDK: RS6x_7x_mmWave_sdk V2.0.6
- 工具链: CSKY (平头哥) RISC-V GCC
- IDE: CDK (C-Sky Development Kit)

## 数据输出格式

每帧通过 SPI 输出：
- 1D FFT 距离像：256 个复数点 (int16 × 2)
- 运动点云：5 列 (x, y, z, vel_cm/s, snr_dB×100)，最大 16 点
- 微动点云：5 列 (x, y, z, vel, snr)，最大 16 点
- 1D gain_factor：256 点 (用于呼吸相位提取)
