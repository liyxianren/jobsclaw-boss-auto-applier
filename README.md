# BOSS Auto Apply

基于 OpenClaw skills 和本地脚本的 BOSS直聘自动投递工作流。

这份仓库是开源整理版，保留了完整的技能路由、阶段划分和脚本入口，但移除了私有运行数据、真实简历、真实偏好和历史投递产物。

## 项目目标

- 脚本负责浏览器控制、搜索、翻页、JD 抓取、发送、重试、容灾和对账
- 模型只负责读取简历和 JD，然后输出 `fit`、`reasoning`、`message`
- 所有最终发送结果以 `receipt.json` 为准

## 工作流

```mermaid
flowchart LR
  A["Stage 1 搜索"] --> B["Stage 2 评估与话术"]
  B --> C["Stage 3 发送与对账"]
```

### Stage 1

- 搜索岗位
- 抓取岗位卡片
- 排序
- 抓取已选 JD

主入口:

- `skills/boss-job-searcher/SKILL.md`
- `skills/boss-zhipin-search/scripts/run_pipeline.py`
- `skills/boss-auto-applier/scripts/rank_jobs.py`
- `skills/boss-auto-applier/scripts/scrape_ranked_jds.py`

### Stage 2

- 读取简历和 JD
- 判断 `fit` 或 `skip`
- 生成打招呼文案

主入口:

- `skills/boss-job-analyzer/SKILL.md`
- `skills/boss-auto-applier/scripts/analyze_batch.py`
- `skills/boss-auto-applier/scripts/analyzer_worker.py`
- `skills/jd-greeting-generator/scripts/generate_message.py`

### Stage 3

- 打开 JD 或聊天页
- 进入正确会话
- 注入文案
- 发送并校验结果
- 统一对账

主入口:

- `skills/boss-job-sender/SKILL.md`
- `skills/boss-auto-applier/scripts/send_batch.py`
- `skills/boss-auto-applier/scripts/sender_worker.py`
- `skills/jd-greeting-generator/scripts/send_greeting.py`
- `skills/boss-auto-applier/scripts/reconcile_receipt.py`

## 仓库结构

```text
.
├── AGENTS.md
├── candidate-preferences.json
├── candidate-resume.md
├── data/
└── skills/
    ├── boss-auto-applier/
    ├── boss-job-searcher/
    ├── boss-job-analyzer/
    ├── boss-job-sender/
    ├── boss-zhipin-search/
    ├── boss-jd-evaluator/
    ├── boss-greeting-sender/
    └── jd-greeting-generator/
```

说明:

- `AGENTS.md` 是主入口说明
- `skills/*/SKILL.md` 是技能路由层
- `skills/*/scripts/*.py` 和 `*.sh` 是实际执行层
- `candidate-resume.md` 和 `candidate-preferences.json` 是模板文件
- `data/` 仅保留空目录占位，不包含真实数据库

## 关键文件

总入口:

- `AGENTS.md`
- `skills/boss-auto-applier/SKILL.md`
- `skills/boss-auto-applier/references/workflow-map.md`

BOSS 页面元素参考:

- `skills/boss-zhipin-search/references/boss-search-elements.md`
- `skills/jd-greeting-generator/references/boss-chat-elements.md`

浏览器辅助脚本:

- `skills/jd-greeting-generator/scripts/start_boss_chrome.sh`
- `skills/jd-greeting-generator/scripts/ab_boss.sh`

## 运行依赖

- macOS 或 Linux
- `python3`
- Google Chrome 或 Chromium
- `curl`
- `agent-browser`
- 一个已经登录 BOSS直聘 的 Chrome profile

这套脚本默认依赖 Chrome CDP 端口 `18801`。

## 配置

### 简历模板

编辑:

- `candidate-resume.md`

用于提供:

- 基本信息
- 技能栈
- 项目经历
- 优势与边界

### 偏好模板

编辑:

- `candidate-preferences.json`

当前模板包含:

- `targetCity`
- `searchKeywords`
- `profile.coreTech`
- `profile.strengths`
- `profile.avoid`
- `jobPreferences.salaryCodes`
- `jobPreferences.experienceCode`
- `jobPreferences.degreeCode`
- `jobPreferences.jobTypeCode`
- `jobPreferences.scaleCodes`

## 启动浏览器

启动或连接 BOSS 专用 Chrome:

```bash
bash skills/jd-greeting-generator/scripts/start_boss_chrome.sh
```

默认行为:

- 使用 CDP `18801`
- 使用已有 profile
- 若没有活动页面则补开页面

如果你已经自己启动并登录，只要 CDP `18801` 可用即可。

## 运行方式

### 1. 只跑搜索

```bash
python3 skills/boss-zhipin-search/scripts/run_pipeline.py \
  --input skills/boss-zhipin-search/templates/input.example.json \
  --outdir .openclaw-runs/demo-search \
  --cdp-port 18801
```

输出:

- `jobs.json`
- `results.json`
- `summary.md`

### 2. 跑完整链路

```bash
python3 skills/boss-auto-applier/scripts/orchestrate_apply.py \
  --run-dir .openclaw-runs/boss-apply/demo-$(date +%Y%m%d-%H%M%S) \
  --keyword "AI产品经理" \
  --city "深圳" \
  --max-apply 10 \
  --cdp-port 18801
```

常用参数:

- `--selection-mode broadcast|fit`
- `--retry-on-fail`
- `--capture-screenshot`
- `--dry-run`
- `--min-interval`
- `--max-interval`

说明:

- `broadcast` 表示广撒网，抓到即投
- `fit` 表示仅发送给评估通过的岗位
- `--dry-run` 只做分析，不做发送

## 产物目录

一次完整运行通常会生成这些文件:

- `search/jobs.json`
- `ranked_jobs.json`
- `jd/manifest.json`
- `eval/<jobId>.json`
- `eval_summary.json`
- `send/<jobId>.json`
- `receipt.json`

其中:

- `eval_summary.json` 是 Stage 2 汇总
- `send/*.json` 是单岗位发送记录
- `receipt.json` 是最终统计和结果来源

## OpenClaw 集成方式

这份仓库是 skill-first 设计。

建议调用顺序:

1. 由主 agent 进入 `skills/boss-auto-applier/SKILL.md`
2. 再按 Stage 1 -> Stage 2 -> Stage 3 路由到子 skill
3. 模型只输出分析和话术
4. 页面动作、重试和恢复全部走脚本

如果只需要其中一个能力，也可以单独调用:

- 搜索: `boss-zhipin-search`
- 评估: `boss-jd-evaluator` 或 `boss-job-analyzer`
- 发送: `boss-greeting-sender` 或 `boss-job-sender`

## 风控与稳定性约束

- 不直连 BOSS API
- 不创建隔离浏览器 session
- 不把点击逻辑和重试逻辑交给模型
- 搜索翻页走 URL 参数，不依赖 UI 过滤器点击
- 发送阶段必须校验当前页面是否真的是目标 JD 或目标聊天会话
- 遇到验证页时，优先保留已抓数据并等待人工处理

## 开源版说明

这份仓库刻意移除了以下内容:

- 真实简历
- 真实偏好配置
- 真实数据库
- 历史运行产物
- 本地私有路径和私有环境信息

你需要自己补充:

- BOSS 登录态
- 个人简历
- 投递偏好
- 实际运行目录

## 参考文档

- `AGENTS.md`
- `skills/boss-auto-applier/SKILL.md`
- `skills/boss-auto-applier/references/workflow-map.md`
- `skills/boss-zhipin-search/references/boss-search-elements.md`
- `skills/jd-greeting-generator/references/boss-chat-elements.md`
