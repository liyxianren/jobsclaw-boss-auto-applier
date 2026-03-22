---
name: boss-job-analyzer
description: "BOSS直聘 Stage 2。负责读取简历和 JD，写 fit 判断并为匹配岗位生成招呼语。"
user-invocable: false
allowed-tools: Bash(python3:*), Bash(curl:*)
---

# BOSS JD 评估与话术生成（Stage 2）

> 本阶段是唯一允许模型参与业务判断的阶段。
> 输入是抓好的 JD 文件，输出是 `eval/*.json` 和 `eval_summary.json`。

## 输入
- `RUN_DIR`
- `candidate-resume.md`
- `candidate-preferences.json`
- `${RUN_DIR}/jd/manifest.json`
- `${RUN_DIR}/jd/*.json`

## 你要做什么
1. 读取 `candidate-resume.md`
2. 读取 `${RUN_DIR}/jd/manifest.json`
3. 对每个成功抓取的 JD：
   - 判断 `fit=true/false`
   - 写 `matchScore`
   - 写 `reasoning`
   - 如果 `fit=true`，写 `message`
4. 写回：
   - `${RUN_DIR}/eval/<jobId>.json`
   - `${RUN_DIR}/eval_summary.json`

## fit 判断原则
- 只看“方向是否能做”，不要重复做脚本已经做过的硬过滤
- 可以宽一点，但不要把纯硬件、纯算法研究、纯销售、纯运维也打成 fit
- `reasoning` 必须指向 JD 的具体要求，不能写泛泛的“方向契合”

## message 生成原则
- 50-120 字正文即可
- 只引用和 JD 最相关的 1-2 个经历点
- 不要生成浏览器动作、发送动作或结果假设

## 输出约定
`eval/<jobId>.json` 至少包含：
```json
{
  "fit": true,
  "matchScore": "高",
  "reasoning": "...",
  "message": "..."
}
```

`eval_summary.json` 至少包含：
```json
{
  "totalEvaluated": 10,
  "fitCount": 3,
  "skipCount": 7,
  "fitJobs": [],
  "skippedJobs": []
}
```

## 相关脚本
- `skills/boss-auto-applier/scripts/analyzer_worker.py`：单 JD 分析 worker
- `skills/boss-auto-applier/scripts/analyze_batch.py`：批量分析脚本
- `skills/boss-auto-applier/scripts/generate_fit_messages.py`：只补消息，不负责完整 fit 判断

## 硬约束
- 不发送消息
- 不写数据库
- 不直接操作浏览器
- `generate_fit_messages.py` 不是本阶段的唯一权威入口，它只是已有 fitJobs 的消息回填工具
