---
name: boss-auto-applier
description: "BOSS直聘自动投递总入口。用于搜索岗位、抓取JD、评估匹配度、生成招呼语并批量发送。"
metadata: {"openclaw":{"emoji":"🎯","os":["darwin","linux"],"requires":{"bins":["agent-browser"]}}}
user-invocable: true
allowed-tools: Bash(agent-browser:*), Bash(python3:*), Bash(curl:*)
---

# BOSS直聘自动投递

> 这是整套流程的总入口。主 agent 负责路由和汇报；worker 负责执行脚本、读取 JD、写评估结果、发送消息。

## 什么时候使用
- 用户要“投递 / 投简历 / 自动投递 / 批量投递”
- 用户要“搜索并投递某类岗位”

如果用户只想搜索，不发送，改走 `boss-zhipin-search`。

## 责任边界
- 脚本负责：浏览器启动、搜索、翻页、排序、JD 抓取、发送、容灾、对账。
- 模型负责：读取 `candidate-resume.md` 和抓下来的 JD，写 `fit`、`matchScore`、`reasoning`、`message`。
- 不允许把页面点击逻辑、重试逻辑、去重逻辑写回到模型步骤里。

详细文件关系见 `references/workflow-map.md`。

## 输入
- `keyword`：用户指定优先；未指定默认 `AI产品经理`
- `city`：默认 `candidate-preferences.json.targetCity`
- `maxApply`：默认 `9`
- `dryRun`：默认 `false`
- `CDP_PORT`：默认 `18801`

## 标准流程

### Stage 1
调用 `boss-job-searcher`：
- 搜索岗位
- 排序
- 抓取已排序岗位的 JD
- 产出 `search/jobs.json`、`ranked_jobs.json`、`jd/manifest.json`

### Stage 2
调用 `boss-job-analyzer`：
- 读取简历
- 读取 `jd/manifest.json` 和各个 JD JSON
- 判断 `fit` / `skip`
- 为 `fit=true` 的岗位生成 `message`
- 产出 `eval/<jobId>.json` 和 `eval_summary.json`

### Stage 3
调用 `boss-job-sender`：
- 读取 `eval_summary.json`
- 批量发送消息
- 对账并生成 `receipt.json`

## 执行约束
- 整个流程必须按 `Stage 1 -> Stage 2 -> Stage 3` 顺序执行。
- `salary` 固定使用 `405`，不要在流程中扩展到 `405,406`。
- `Stage 1` 和 `Stage 3` 的浏览器动作只能走脚本，禁止临时手点 UI 来替代脚本逻辑。
- 所有最终结果以 `receipt.json` 为准。

## 关键产物
- `search/jobs.json`
- `ranked_jobs.json`
- `jd/manifest.json`
- `eval_summary.json`
- `send/*.json`
- `receipt.json`

## 相关文件
- `references/workflow-map.md`
- `skills/boss-job-searcher/SKILL.md`
- `skills/boss-job-analyzer/SKILL.md`
- `skills/boss-job-sender/SKILL.md`
