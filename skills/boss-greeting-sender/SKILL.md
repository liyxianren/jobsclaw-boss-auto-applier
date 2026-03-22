---
name: boss-greeting-sender
description: "在 BOSS 直聘发送已生成好的消息。只负责浏览器发送，不负责匹配判断或文案生成。"
metadata: {"openclaw":{"emoji":"📨","os":["darwin","linux"],"requires":{"bins":["agent-browser"]}}}
user-invocable: false
allowed-tools: Bash(agent-browser:*), Bash(python3:*)
---

# BOSS 打招呼发送器

> send-only wrapper。

## 输入
- `job_url`
- `message_file`，文件内必须已有 `.message`

## 执行
```bash
python3 skills/jd-greeting-generator/scripts/send_greeting.py \
  --job-url "<job_url>" \
  --message-file "<eval_json_path>"
```

## 规则
- 只从 `message_file` 读取消息正文
- 不改文案
- 不做 fit 判断
- 发送后输出结构化 JSON

## 参考
- `../jd-greeting-generator/references/boss-chat-elements.md`
