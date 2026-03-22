---
name: boss-job-searcher
description: "BOSS直聘 Stage 1。负责搜索岗位、排序结果并批量抓取 JD，不做 fit 判断。"
user-invocable: false
allowed-tools: Bash(python3:*), Bash(curl:*)
---

# BOSS 岗位搜索（Stage 1）

> 本阶段只负责拿到候选岗位和 JD 原文，不负责判断 fit，也不负责生成消息。

## 输入
- `RUN_DIR`
- `FILTERS_JSON_PATH`
- `CDP_PORT`，默认 `18801`
- `PAGE_LIMIT`
- `CITY`
- `MAX_APPLY`

## 执行步骤

### 1. 搜索岗位
```bash
python3 skills/boss-zhipin-search/scripts/run_pipeline.py \
  --input ${FILTERS_JSON_PATH} \
  --outdir ${RUN_DIR}/search \
  --cdp-port ${CDP_PORT} \
  --headed \
  --page-limit ${PAGE_LIMIT}
```

### 2. 排序
```bash
python3 skills/boss-auto-applier/scripts/rank_jobs.py \
  --jobs ${RUN_DIR}/search/jobs.json \
  --resume candidate-resume.md \
  --db data/boss_greeting.db \
  --output ${RUN_DIR}/ranked_jobs.json \
  --city "${CITY}" \
  --max-count ${MAX_APPLY}
```

### 3. 批量抓取 JD
```bash
python3 skills/boss-auto-applier/scripts/scrape_ranked_jds.py \
  --ranked-jobs ${RUN_DIR}/ranked_jobs.json \
  --outdir ${RUN_DIR}/jd \
  --cdp-port ${CDP_PORT} \
  --manifest ${RUN_DIR}/jd/manifest.json
```

## 输出
- `${RUN_DIR}/search/jobs.json`
- `${RUN_DIR}/search/results.json`
- `${RUN_DIR}/search/summary.md`
- `${RUN_DIR}/ranked_jobs.json`
- `${RUN_DIR}/jd/*.json`
- `${RUN_DIR}/jd/manifest.json`

## 成功标准
- `search/jobs.json` 存在
- `ranked_jobs.json` 存在
- `jd/manifest.json` 存在
- `manifest.scraped > 0`，否则本轮应提前结束，不进入发送阶段

## 硬约束
- 不点击页面上的筛选按钮
- 不手动输入筛选条件到 UI
- 不在本阶段做任何 fit 判断
- 只允许通过脚本访问浏览器

## 参考
- `../boss-zhipin-search/references/boss-search-elements.md`
