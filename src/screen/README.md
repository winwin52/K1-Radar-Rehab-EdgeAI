# screen/ — K1 HDMI 屏幕进程 (Process C)

pygame 全屏应用，运行在 K1 的 7 寸 HDMI 触摸屏上，专供**正在做康复的患者**看。

## 模块

```
screen/
├─ app.py                 主循环 + ZMQ 订阅 + pygame 事件
├─ render/                
│   ├─ idle.py            IDLE 页面: 二维码 + 设备就绪
│   ├─ baseline.py        基线采集: 静坐倒计时 + 呼吸引导
│   ├─ training.py        训练: 抬腿引导大字 + 倒计 + 进度
│   ├─ rest.py            组间休息
│   ├─ summary.py         总结 + 触屏键盘备注输入
│   └─ error.py           错误弹窗
├─ widgets.py             按钮 / 进度环 / 大字数字 / 触屏键盘
└─ touch.py               触摸事件 → ZMQ REQ 发到 backend
```

## 设计原则

- **大字优先**：康复时距离屏幕 1-2 米，字号必须够大
- **高对比**：户内光线复杂，需深色背景 + 亮色字
- **触摸目标 ≥ 80×80 px**：手部不灵活的患者也能点中
- **低延迟**：状态切换 → 显示更新 ≤ 50ms
- **简洁**：信息密度低，一屏只回答"现在该做什么"
