---
name: boss-jd-evaluator
description: "根据 JD 和简历判断岗位是否适合候选人。只输出 fit 判断，不发送消息。"
user-invocable: true
---

# BOSS JD Evaluator

> fit 判断的职责只在这里，不负责发送，不负责浏览器动作。

## 输入
- JD JSON
- `candidate-resume.md`
- `candidate-preferences.json`

## 输出
```json
{
  "fit": true,
  "matchScore": "高",
  "reasoning": "..."
}
```

## 判断原则
- 看“入职后能不能做”，不是看关键词有没有重合
- 纯硬件、纯销售、纯运维、纯算法研究通常直接 `fit=false`
- `reasoning` 必须引用 JD 的具体要求

## 禁止
- 不生成招呼消息
- 不发送消息
- 不操作浏览器
