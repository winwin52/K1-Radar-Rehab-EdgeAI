# webapp/ — 网页前端（Vue 3 单文件）

由 K1 后端通过 FastAPI 静态文件挂载托管，手机/电脑浏览器扫码即用。

## 计划页面

```
webapp/
├─ index.html             首页: 设备状态 + 患者列表 + [启动 session]
├─ patient.html           新建/编辑患者档案
├─ plan.html              计划编辑 (默认 / 自定义)
├─ live.html              实时监控 (情绪曲线 + 进度 + WS 推流)
├─ history.html           历史 session 列表 (所有患者所有 session)
├─ assessment.html        AI 评估报告查看
├─ app.js                 Vue 3 应用共享代码
├─ style.css              样式
└─ vendor/
    ├─ vue.global.prod.js
    ├─ chart.umd.js       (情绪曲线图)
    └─ qrcode.min.js      (K1 IP 二维码)
```

## 设计原则

- **无构建**：Vue 通过 CDN/本地 vendor 加载，K1 直接 serve 静态文件
- **响应式**：手机竖屏 + 电脑横屏均可
- **离线友好**：vendor 全部本地，演示现场不依赖外网
- **实时刷新**：所有动态数据通过 WebSocket `/ws/live` 订阅
