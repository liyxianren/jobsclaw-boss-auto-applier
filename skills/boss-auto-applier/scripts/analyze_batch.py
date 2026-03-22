#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 2: 批量 JD 分析 + 招呼生成。

对 ranked_jobs.json 中的每个岗位：
  1. scrape_jd.py 抓取 JD 详情
  2. analyzer_worker.py 评估匹配度 + 生成招呼消息
  3. 写入 eval/<job_id>.json

最终产出 eval_summary.json（fitJobs 列表 + 统计）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE = Path(__file__).resolve().parents[3]
SCRIPTS = {
    "scrape_jd": WORKSPACE / "skills/jd-greeting-generator/scripts/scrape_jd.py",
    "analyzer_worker": WORKSPACE / "skills/boss-auto-applier/scripts/analyzer_worker.py",
}


def sh(cmd: List[str], *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def extract_job_id(url: str) -> str:
    m = re.search(r"/job_detail/([a-zA-Z0-9_\-]+)\.html", url)
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:40] or "unknown"


def is_valid_job_url(url: str) -> bool:
    return bool(re.search(r"^https://www\.zhipin\.com/job_detail/[a-zA-Z0-9_\-]+\.html", url))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def extract_json_from_mixed_output(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty output")
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"[\[{]", s)]
    if not starts:
        raise ValueError("no json start")
    for i in starts:
        try:
            obj, _ = decoder.raw_decode(s[i:])
            return obj
        except Exception:
            continue
    raise ValueError("failed to parse json from mixed output")


def normalize_match_score(ms: Optional[str]) -> str:
    if not ms:
        return "低"
    ms = str(ms).strip().lower()
    if ms in {"high", "高"}:
        return "高"
    if ms in {"medium", "中"}:
        return "中"
    return "低"


def build_broadcast_message(job_title: str, company: str, city: str) -> str:
    jt = (job_title or "该岗位").strip()
    co = (company or "贵司").strip()
    city_text = f"{city}本地" if city else ""
    return (
        f"您好，看到{co}的{jt}岗位，我目前主要做AI应用与智能体落地"
        f"（Python/Agent/RAG/自动化），有真实项目交付经验。"
        f"{city_text}方向我也在持续关注，想进一步沟通岗位细节。"
    )


def run_scrape_jd(job_url: str, jd_path: Path, cdp_port: int) -> Dict[str, Any]:
    cp = sh([sys.executable, str(SCRIPTS["scrape_jd"]), "--job-url", job_url, "--cdp-port", str(cdp_port)])
    out = cp.stdout.strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        data = {"status": "failed", "error": "invalid_json", "_raw": out[-2000:]}
    dump_json(jd_path, data)
    return data


def run_analyzer_worker(jd_json: Path, resume: Path, prefs: Path, out_path: Path) -> Dict[str, Any]:
    cp = sh([sys.executable, str(SCRIPTS["analyzer_worker"]), "--jd-json", str(jd_json), "--resume", str(resume), "--prefs", str(prefs), "--out", str(out_path)])
    if out_path.exists():
        try:
            return load_json(out_path)
        except Exception:
            pass
    out = cp.stdout.strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        data = {
            "fit": False,
            "matchScore": "低",
            "skipReason": "analyzer_invalid_json",
            "reasoning": out[-500:] if out else "analyzer output empty",
            "messageDraft": "",
        }
    dump_json(out_path, data)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 2: Batch JD analysis + greeting generation")
    ap.add_argument("--ranked-jobs", required=True, help="Path to ranked_jobs.json from Stage 1")
    ap.add_argument("--resume", required=True, help="Path to candidate resume markdown")
    ap.add_argument("--prefs", required=True, help="Path to candidate preferences JSON")
    ap.add_argument("--outdir", required=True, help="Run dir or eval output directory")
    ap.add_argument("--cdp-port", type=int, default=18801, help="Chrome CDP port")
    ap.add_argument("--max-send", type=int, default=10, help="Max jobs selected to send")
    ap.add_argument("--jd-retry-wait", type=float, default=6.0, help="Seconds to wait before retrying a failed JD scrape")
    ap.add_argument("--min-jd-delay", type=float, default=2.5, help="Min random delay between JD scrapes")
    ap.add_argument("--max-jd-delay", type=float, default=4.0, help="Max random delay between JD scrapes")
    ap.add_argument("--selection-mode", choices=["broadcast", "fit"], default="broadcast",
                    help="broadcast=广撒网(默认，抓到即投); fit=仅投匹配岗位")
    args = ap.parse_args()

    min_jd_delay = max(0.0, float(args.min_jd_delay))
    max_jd_delay = max(0.0, float(args.max_jd_delay))
    if min_jd_delay > max_jd_delay:
        min_jd_delay, max_jd_delay = max_jd_delay, min_jd_delay
    jd_retry_wait = max(0.0, float(args.jd_retry_wait))

    outdir_arg = Path(args.outdir)
    if outdir_arg.name == "eval":
        run_dir = outdir_arg.parent
        eval_dir = outdir_arg
    else:
        run_dir = outdir_arg
        eval_dir = run_dir / "eval"

    run_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)
    jd_dir = run_dir / "jd"
    jd_dir.mkdir(parents=True, exist_ok=True)

    resume_path = Path(args.resume)
    prefs_path = Path(args.prefs)

    ranked_jobs = load_json(Path(args.ranked_jobs))
    if isinstance(ranked_jobs, dict) and "jobs" in ranked_jobs:
        ranked_jobs = ranked_jobs["jobs"]
    if not isinstance(ranked_jobs, list):
        ranked_jobs = []

    fit_jobs: List[Dict[str, Any]] = []
    skip_reasons: Dict[str, int] = {}
    total_analyzed = 0
    seen_urls: set = set()

    print(f"[INFO] Starting batch analysis of {len(ranked_jobs)} jobs", flush=True)

    for job in ranked_jobs:
        if len(fit_jobs) >= max(0, int(args.max_send)):
            break

        job_url = (job.get("link") or "").strip()
        title = job.get("title") or ""
        company = job.get("company") or ""
        salary = job.get("salary") or ""
        city = job.get("city") or ""

        if not is_valid_job_url(job_url) or job_url in seen_urls:
            skip_reasons["invalid_or_duplicate"] = skip_reasons.get("invalid_or_duplicate", 0) + 1
            continue

        seen_urls.add(job_url)
        job_id = extract_job_id(job_url)
        total_analyzed += 1

        # 1) Scrape JD
        jd_path = jd_dir / f"{job_id}.json"
        jd_data = run_scrape_jd(job_url, jd_path, args.cdp_port)
        if jd_data.get("status") != "ok":
            print(f"[WAIT] JD scrape retry cooldown {jd_retry_wait:.1f}s", flush=True)
            time.sleep(jd_retry_wait)
            jd_data = run_scrape_jd(job_url, jd_path, args.cdp_port)

        if jd_data.get("status") != "ok":
            skip_reasons["scrape_failed"] = skip_reasons.get("scrape_failed", 0) + 1
            print(f"[WARN] scrape failed: {title} @ {company}", flush=True)
            continue

        # 2) Analyze
        analysis_path = eval_dir / f"{job_id}.analysis.json"
        analyzer = run_analyzer_worker(jd_path, resume_path, prefs_path, analysis_path)

        fit = bool(analyzer.get("fit"))
        match_score = normalize_match_score(analyzer.get("matchScore"))
        reasoning = analyzer.get("reasoning") or ""
        skip_reason = analyzer.get("skipReason")
        message_draft = analyzer.get("messageDraft") or ""

        selected_for_send = fit
        if args.selection_mode == "broadcast":
            selected_for_send = True
            if not message_draft.strip():
                message_draft = build_broadcast_message(
                    jd_data.get("jobTitle") or title,
                    jd_data.get("company") or company,
                    city,
                )
            skip_reason = None

        # 3) Write eval JSON
        eval_payload = {
            "fit": selected_for_send,
            "fitByAnalyzer": fit,
            "matchScore": match_score,
            "skipReason": skip_reason,
            "reasoning": reasoning,
            "messageDraft": message_draft,
            "message": message_draft,
            "jobUrl": job_url,
            "jobId": job_id,
            "jobTitle": jd_data.get("jobTitle") or title,
            "company": jd_data.get("company") or company,
            "salary": jd_data.get("salary") or salary,
            "evaluatedAt": now_iso(),
        }
        eval_path = eval_dir / f"{job_id}.json"
        dump_json(eval_path, eval_payload)

        if selected_for_send:
            fit_jobs.append({
                "jobUrl": job_url,
                "jobId": job_id,
                "title": eval_payload["jobTitle"],
                "company": eval_payload["company"],
                "salary": eval_payload["salary"],
                "matchScore": match_score,
                "messageDraft": message_draft,
                "evalPath": str(eval_path),
            })
            print(f"[FIT] {eval_payload['jobTitle']} @ {eval_payload['company']} ({match_score})", flush=True)
        else:
            reason = skip_reason or "fit_false"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            print(f"[SKIP] {title} @ {company} ({reason})", flush=True)

        if len(fit_jobs) < max(0, int(args.max_send)) and max_jd_delay > 0:
            delay = random.uniform(min_jd_delay, max_jd_delay)
            print(f"[WAIT] JD scrape cooldown {delay:.1f}s", flush=True)
            time.sleep(delay)

    # Write summary
    summary = {
        "totalAnalyzed": total_analyzed,
        "fitCount": len(fit_jobs),
        "skipCount": total_analyzed - len(fit_jobs),
        "fitJobs": fit_jobs,
        "skipReasons": skip_reasons,
        "completedAt": now_iso(),
    }
    summary_path = run_dir / "eval_summary.json"
    dump_json(summary_path, summary)

    print(f"\n[OK] Analyzed {total_analyzed} jobs: {len(fit_jobs)} fit, {total_analyzed - len(fit_jobs)} skipped", flush=True)
    print(f"[OK] Summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
