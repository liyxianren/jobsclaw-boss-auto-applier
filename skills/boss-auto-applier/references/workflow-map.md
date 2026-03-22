# Workflow Map

## Canonical Flow
1. Stage 1: search + rank + JD scrape
2. Stage 2: resume/JD evaluation + greeting generation
3. Stage 3: send + reconcile

## Script Ownership
- Search/browser bootstrap:
  - `skills/boss-zhipin-search/scripts/run_pipeline.py`
  - `skills/boss-zhipin-search/scripts/scrape_jobs_browser.py`
  - `skills/jd-greeting-generator/scripts/start_boss_chrome.sh`
- Ranking and JD files:
  - `skills/boss-auto-applier/scripts/rank_jobs.py`
  - `skills/boss-auto-applier/scripts/scrape_ranked_jds.py`
  - `skills/jd-greeting-generator/scripts/scrape_jd.py`
- Evaluation and copy:
  - `skills/boss-auto-applier/scripts/analyzer_worker.py`
  - `skills/boss-auto-applier/scripts/analyze_batch.py`
  - `skills/boss-auto-applier/scripts/generate_fit_messages.py`
  - `skills/jd-greeting-generator/scripts/generate_message.py`
- Delivery and recovery:
  - `skills/boss-auto-applier/scripts/send_batch.py`
  - `skills/boss-auto-applier/scripts/sender_worker.py`
  - `skills/boss-auto-applier/scripts/self_heal_agent.py`
  - `skills/jd-greeting-generator/scripts/send_greeting.py`
  - `skills/boss-auto-applier/scripts/reconcile_receipt.py`

## Model vs Script
- Scripts own navigation, retries, captcha detection, dedup, and receipt reconciliation.
- The model only reads files and writes structured fit/message results.

## Key Artifacts
- `search/jobs.json`: raw search result set
- `ranked_jobs.json`: ranked candidates before JD scrape
- `jd/manifest.json`: which JD pages were scraped successfully
- `eval/<jobId>.json`: per-job analysis result
- `eval_summary.json`: fitJobs + skippedJobs summary
- `send/<jobId>.json`: per-job send result
- `receipt.json`: final run result

## Notes on Alternate Scripts
- `orchestrate_apply.py` is a full CLI orchestrator and test harness, not the only public route.
- `generate_fit_messages.py` only backfills messages for existing `fitJobs`; it is not the full Stage 2 contract by itself.
