---
name: jd-greeting-generator
description: "根据 JD 和简历生成 BOSS 直聘打招呼消息。skill 本身只负责文案生成，发送由独立 sender skill 完成。"
metadata: {"openclaw":{"emoji":"👋","os":["darwin","linux"],"requires":{"bins":["agent-browser"]}}}
user-invocable: true
---

# JD Greeting Generator

> 这个 skill bundle 里同时放了 `scrape_jd.py`、`generate_message.py`、`send_greeting.py` 等脚本。
> 但 skill 的主职责只有一件事：基于 JD 和简历生成消息内容。

## 输入
- JD JSON
- `candidate-resume.md`
- `matchScore`

## 输出
```json
{
  "message": "..."
}
```

## 生成原则
- 文案必须围绕当前 JD 的具体要求
- 只写和 JD 最相关的 1-2 个经历点
- 不编造成果
- 不把浏览器动作写进消息里

## bunded scripts
- `scrape_jd.py`：抓 JD
- `match_resume.py`：做 JD/简历匹配分析
- `generate_message.py`：生成消息
- `send_greeting.py`：发送消息
- `run_greeting_pipeline.py`：整条旧链路调试入口

## 注意
- 发送动作不属于本 skill 的主要职责，标准发送入口是 `boss-greeting-sender` 或 `boss-job-sender`
- 页面元素与发送动作说明见 `references/boss-chat-elements.md`
