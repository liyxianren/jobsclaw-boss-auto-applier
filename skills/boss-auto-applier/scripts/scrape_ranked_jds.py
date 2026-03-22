#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量抓取 ranked_jobs.json 中每个岗位的 JD 详情。

机械性脚本：仅负责循环 scrape，不做任何匹配度判断。
匹配度评估由 subagent LLM 完成。

输入：
  --ranked-jobs <path>   ranked_jobs.json 路径
  --outdir <path>        JD 输出目录（如 $RUN_DIR/jd）
  --cdp-port <port>      CDP 端口
  --manifest <path>      输出 manifest.json（成功抓取的岗位清单）

输出 manifest.json：
  {
    "total": 20,
    "scraped": 15,
    "failed": 5,
    "jobs": [
      {
        "jobId": "xxx",
        "jobUrl": "https://...",
        "jdPath": "/abs/path/to/jd/xxx.json",
        "title": "从JD提取",
        "company": "从JD提取",
        "salary": "从JD提取",
        "experience": "从JD提取"
      }
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

SCRAPE_SCRIPT = Path(__file__).resolve().parents[3] / "skills/jd-greeting-generator/scripts/scrape_jd.py"


def extract_job_id(url: str) -> str:
    m = re.search(r"/job_detail/([a-zA-Z0-9_\-]+)\.html", url)
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:40] or "unknown"


def is_valid_job_url(url: str) -> bool:
    return bool(re.search(r"^https://www\.zhipin\.com/job_detail/[a-zA-Z0-9_\-]+\.html", url))


def extract_json_from_output(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty")
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", s):
        try:
            obj, _ = decoder.raw_decode(s[m.start():])
            return obj
        except Exception:
            continue
    raise ValueError("no json")


def scrape_one(job_url: str, cdp_port: int) -> Dict[str, Any]:
    cp = subprocess.run(
        [sys.executable, str(SCRAPE_SCRIPT), "--job-url", job_url, "--cdp-port", str(cdp_port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    return extract_json_from_output(cp.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch scrape JDs from ranked_jobs.json")
    ap.add_argument("--ranked-jobs", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--cdp-port", type=int, default=18801)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--retry-wait", type=float, default=6.0, help="seconds to wait before retry on scrape failure")
    ap.add_argument("--min-delay", type=float, default=2.5, help="min random delay between successful scrapes")
    ap.add_argument("--max-delay", type=float, default=4.0, help="max random delay between successful scrapes")
    args = ap.parse_args()

    min_delay = float(args.min_delay)
    max_delay = float(args.max_delay)
    retry_wait = max(0.0, float(args.retry_wait))
    if min_delay < 0:
        min_delay = 0.0
    if max_delay < 0:
        max_delay = 0.0
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with open(args.ranked_jobs, "r", encoding="utf-8") as f:
        ranked = json.load(f)
    if isinstance(ranked, dict) and "jobs" in ranked:
        ranked = ranked["jobs"]

    seen: set = set()
    jobs_out: List[Dict[str, Any]] = []
    failed = 0

    total = len(ranked)
    for idx, job in enumerate(ranked):
        url = (job.get("link") or "").strip()
        if not is_valid_job_url(url) or url in seen:
            failed += 1
            continue
        seen.add(url)
        job_id = extract_job_id(url)
        jd_path = outdir / f"{job_id}.json"

        # Scrape with retry
        jd_data = None
        for attempt in range(2):
            try:
                jd_data = scrape_one(url, args.cdp_port)
                if isinstance(jd_data, dict) and jd_data.get("status") == "ok":
                    break
            except Exception:
                pass
            if attempt == 0:
                time.sleep(retry_wait)

        if not jd_data or not isinstance(jd_data, dict) or jd_data.get("status") != "ok":
            failed += 1
            print(f"[FAIL] {url}", flush=True)
            continue

        # Save JD to file
        with jd_path.open("w", encoding="utf-8") as f:
            json.dump(jd_data, f, ensure_ascii=False, indent=2)
            f.write("\n")

        jobs_out.append({
            "jobId": job_id,
            "jobUrl": url,
            "jdPath": str(jd_path.resolve()),
            "title": jd_data.get("jobTitle") or "",
            "company": jd_data.get("company") or "",
            "salary": jd_data.get("salary") or "",
            "experience": jd_data.get("experience") or "",
        })
        print(f"[OK] {jd_data.get('jobTitle', '')} @ {jd_data.get('company', '')} → {jd_path.name}", flush=True)

        # Anti-bot: short random delay between scrapes to reduce burst navigation.
        # Skip delay for the final item to avoid unnecessary tail latency.
        if idx < total - 1 and max_delay > 0:
            delay = random.uniform(min_delay, max_delay)
            print(f"[WAIT] {delay:.1f}s before next scrape", flush=True)
            time.sleep(delay)

    manifest = {
        "total": len(ranked),
        "scraped": len(jobs_out),
        "failed": failed,
        "jobs": jobs_out,
    }
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n[DONE] {len(jobs_out)}/{len(ranked)} scraped, {failed} failed → {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
