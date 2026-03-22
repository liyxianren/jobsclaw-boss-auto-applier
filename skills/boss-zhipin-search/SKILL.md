---
name: boss-zhipin-search
description: "BOSS直聘搜索脚本包。负责启动/连接 BOSS Chrome、抓取岗位卡片并输出 jobs.json/results.json/summary.md。"
metadata: {"openclaw":{"emoji":"🔎","os":["darwin","linux"],"requires":{"bins":["agent-browser"]}}}
user-invocable: true
allowed-tools: Bash(agent-browser:*), Bash(python3:*), Bash(curl:*)
---

# BOSS直聘搜索脚本包

> 这是底层搜索 bundle，不负责 fit 判断，也不负责发送。

## 使用场景
- 用户只想搜索并导出岗位列表
- `boss-job-searcher` 在 Stage 1 中调用它

## 标准入口
```bash
python3 scripts/run_pipeline.py \
  --input templates/input.example.json \
  --outdir .openclaw-runs/demo \
  --cdp-port 18801 \
  --headed
```

## 内部职责
- 校验 filters
- 启动或重启 `18801` 上的 BOSS Chrome
- 通过 DOM 抓取岗位卡片
- 输出结构化结果和 markdown 摘要

## 输出
- `outdir/jobs.json`
- `outdir/results.json`
- `outdir/summary.md`

## 强约束
- 不直连 BOSS API
- 不创建隔离 browser session
- 不点击页面筛选按钮
- 翻页走 URL `&page=N`
- 检测到验证页时等待人工处理，超时保留已抓数据

## 相关脚本
- `scripts/run_pipeline.py`
- `scripts/validate_filters.py`
- `scripts/scrape_jobs_browser.py`
- `scripts/summarize_jobs.py`

## 参考
- `references/boss-search-elements.md`
