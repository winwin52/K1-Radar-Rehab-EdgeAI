# prompts/ — LLM Prompt 文件库

## 规范

- 所有 prompt 写成 markdown 文件
- 用 `_v1` `_v2` 后缀做版本管理(永不覆盖,只追加新版本)
- 模板变量用 `{{var}}` (Jinja2)
- 文件名映射在 `config/llm.toml` 的 `prompt_file` 字段

## 当前文件

```
prompts/
├─ assessment_v1.md           康复评估报告
├─ encourage_v1.md            鼓励语生成
├─ encourage_fallback.txt     无 LLM 时的预录鼓励语 (每行一句)
└─ coach_advice_v1.md         (预留) Coach 决策辅助
```

## Prompt 结构约定

每个 prompt 文件用 `# system` `# user` 分节,加载器按节切分:

```markdown
# system
你是一位资深康复医师助理...

# user
患者匿名 ID: {{patient_id}}
...
```

加载后生成 OpenAI messages 数组:
```python
[
  {"role": "system", "content": "你是一位资深康复医师助理..."},
  {"role": "user",   "content": "患者匿名 ID: p7f3a2..."}
]
```

## 演进规则

- 想改语气 → 直接改对应 `_v1.md`,热加载生效
- 想做 A/B → 新建 `_v2.md`,在 config 切换
- 测试新版稳定后,删除旧版或保留作为对照
