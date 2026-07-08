# config/ — 配置文件

## 文件清单

| 文件 | 内容 | 入 git? |
|---|---|---|
| `system.toml` | 端口、路径、超时、采样率等系统参数 | ✅ |
| `llm.toml` | LLM provider / 模型 / 温度 / 预算 | ✅ |
| `default_plan.json` | 临床默认康复计划模板 | ✅ |
| `secrets.env.example` | 密钥模板 | ✅ |
| `secrets.env` | **真实密钥**，由 systemd EnvironmentFile= 注入 | ❌ gitignore |

## 部署密钥

```bash
# 1. 复制模板
cp config/secrets.env.example config/secrets.env

# 2. 编辑填入真实 key
vim config/secrets.env

# 3. 设置权限
chmod 600 config/secrets.env

# 4. systemd 注入 (生产)
# /etc/systemd/system/rehab-backend.service:
#   [Service]
#   EnvironmentFile=/opt/rehab/config/secrets.env
```

## 热加载

- `system.toml` — 重启服务生效
- `llm.toml` — 每次 LLM 调用前读取 (热加载)
- `default_plan.json` — 每次 session 启动时读取
- `prompts/*.md` — 每次调用前 stat mtime,变了重读
