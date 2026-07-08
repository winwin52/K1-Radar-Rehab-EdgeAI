# tools/ — Deployment & maintenance tools

跨平台开发流程：在 Windows 写代码 → 部署到 K1 → 在 K1 验证。

## 工具一览

| 文件 | 在哪跑 | 用途 |
|---|---|---|
| `deploy.sh` | Windows (Git Bash) | tar+ssh 同步代码到 K1 |
| `k1_setup.sh` | K1 (一次性) | 装系统包 + 字体 + Python 依赖 |
| `k1_start.sh` | K1 | 一键启动 backend (+ screen 可选) |
| `check_env.py` | 任意平台 | 健康检查 (本地或 --remote) |
| `systemd/rehab-backend.service` | K1 (生产) | 后端 systemd 服务 |
| `systemd/rehab-screen.service`  | K1 (生产) | 屏幕 systemd 服务 |

后续 Phase 4 + 5 会加：
- `render_ambient.py` (Windows) — 预渲染 3 段 ambient wav
- `render_cues.py` (Windows) — 预渲染节拍音
- `render_tts_templates.py` (Windows) — 预录通用 TTS

## 典型工作流

### 首次部署 (一次性, ~15 分钟)

```bash
# 1. 在 Windows 上 push 代码
cd D:/A_the_game/scripts/realtime3.0
bash tools/deploy.sh

# 2. SSH 到 K1 执行 setup
ssh winwin51@10.126.135.110
cd ~/radar_r/realtime3.0
bash tools/k1_setup.sh
exit

# 3. 健康检查
python tools/check_env.py --remote winwin51@10.126.135.110
```

### 日常开发循环

```bash
# 每次改完代码 (Windows):
bash tools/deploy.sh

# 跑起来 (在 K1 上, SSH 或 tmux):
bash tools/k1_start.sh                    # 前台运行,Ctrl-C 停
# 或
bash tools/k1_start.sh --background       # 后台运行,记录 PID
bash tools/k1_start.sh --with-screen      # 同时跑屏幕

# 测试 (浏览器): http://10.126.135.110:8000
```

### 生产部署 (systemd, 比赛/演示前)

```bash
# 在 K1 上:
sudo cp tools/systemd/rehab-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rehab-backend rehab-screen
sudo systemctl start rehab-backend rehab-screen

# 查看日志:
journalctl -u rehab-backend -f
journalctl -u rehab-screen -f

# K1 开机后会自动进入 IDLE,等待网页指令
```

## 环境配置

`deploy.sh` 和 `k1_setup.sh` 都支持环境变量覆盖：

```bash
K1_USER=winwin51 K1_HOST=10.126.135.110 bash tools/deploy.sh
VENV=/path/to/venv bash tools/k1_setup.sh
```

## SSH 免密 (推荐)

每次同步代码要输密码很烦。设置 SSH key:

```bash
# Windows 上 (Git Bash):
ssh-keygen -t ed25519                      # 全部回车
ssh-copy-id winwin51@10.126.135.110        # 输一次密码
# 之后 ssh / deploy.sh 都不要密码了
```
