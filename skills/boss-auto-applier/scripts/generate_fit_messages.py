#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 2: 为 eval_summary.fitJobs 生成并回填 messageDraft。

输入：
  --eval-summary <path>
  --resume <path>
  --run-dir <path, optional>

行为：
  1) 对 fitJobs 逐个调用 generate_message.py 生成消息
  2) 回填到 eval/<job_id>.json 的 messageDraft/message 字段
  3) 更新 eval_summary.json 中 fitJobs.messageDraft 与 fitCount
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

WORKSPACE = Path(__file__).resolve().parents[3]
GEN_SCRIPT = WORKSPACE / "skills/jd-greeting-generator/scripts/generate_message.py"


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def init_log_path(run_dir: Path, explicit: str, default_name: str) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = run_dir / p
    else:
        p = run_dir / "logs" / default_name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def append_run_log(log_path: Path, level: str, event: str, **fields: Any) -> None:
    row = {"ts": now_iso(), "level": level, "event": event}
    row.update(fields)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


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
    for i in reversed(starts):
        try:
            obj, _ = decoder.raw_decode(s[i:])
            return obj
        except Exception:
            continue
    raise ValueError("failed to parse json from mixed output")


def extract_job_id(job_url: str) -> str:
    m = re.search(r"/job_detail/([a-zA-Z0-9_\-]+)\.html", job_url or "")
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "_", job_url or "").strip("_")[:40] or "unknown"


def resolve_run_dir(eval_summary_path: Path, run_dir: str) -> Path:
    if run_dir:
        return Path(run_dir)
    return eval_summary_path.parent


def resolve_scoped_path(run_dir: Path, raw_path: str, fallback: Path) -> Path:
    raw = (raw_path or "").strip()
    if not raw:
        return fallback
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        candidate.resolve().relative_to(run_dir.resolve())
        return candidate
    except Exception:
        return fallback


def resolve_paths(run_dir: Path, job: Dict[str, Any], job_id: str) -> Tuple[Path, Path]:
    jd_fallback = run_dir / "jd" / f"{job_id}.json"
    eval_fallback = run_dir / "eval" / f"{job_id}.json"
    jd_path = resolve_scoped_path(run_dir, str(job.get("jdPath") or ""), jd_fallback)
    eval_path = resolve_scoped_path(run_dir, str(job.get("evalPath") or ""), eval_fallback)
    return jd_path, eval_path


def run_generate_message(jd_path: Path, resume_path: Path, match_score: str) -> str:
    payload = {"matchScore": match_score or "中", "matchedKeywords": []}
    cp = subprocess.run(
        [
            sys.executable,
            str(GEN_SCRIPT),
            "--jd-file",
            str(jd_path),
            "--match-json",
            json.dumps(payload, ensure_ascii=False),
            "--resume",
            str(resume_path),
            "--style",
            "professional",
            "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out = (cp.stdout or "").strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    msg = data.get("message")
    if not isinstance(msg, str):
        return ""
    return msg.strip()


def safe_load_dict(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            data = load_json(path)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate message drafts for fitJobs and sync eval files")
    ap.add_argument("--eval-summary", required=True, help="Path to eval_summary.json")
    ap.add_argument("--resume", required=True, help="Path to candidate resume markdown")
    ap.add_argument("--run-dir", default="", help="Run directory override")
    ap.add_argument("--log-file", default="", help="Optional JSONL log path (default: <run-dir>/logs/stage2_message_generation.jsonl)")
    args = ap.parse_args()

    eval_summary_path = Path(args.eval_summary)
    resume_path = Path(args.resume)
    run_dir = resolve_run_dir(eval_summary_path, args.run_dir)
    log_path = init_log_path(run_dir, args.log_file, "stage2_message_generation.jsonl")
    append_run_log(
        log_path,
        "info",
        "stage2_start",
        evalSummary=str(eval_summary_path),
        runDir=str(run_dir),
        resume=str(resume_path),
    )

    summary = load_json(eval_summary_path)
    if not isinstance(summary, dict):
        raise ValueError("invalid_eval_summary")

    fit_jobs = summary.get("fitJobs") or []
    if not isinstance(fit_jobs, list):
        fit_jobs = []
    append_run_log(log_path, "info", "fit_jobs_loaded", fitJobs=len(fit_jobs))

    kept_jobs: List[Dict[str, Any]] = []
    failed_jobs: List[Dict[str, Any]] = []

    for job in fit_jobs:
        if not isinstance(job, dict):
            continue

        job_url = str(job.get("jobUrl") or "").strip()
        job_id = str(job.get("jobId") or "").strip() or extract_job_id(job_url)
        jd_path, eval_path = resolve_paths(run_dir, job, job_id)
        append_run_log(
            log_path,
            "info",
            "job_start",
            jobId=job_id,
            title=str(job.get("title") or ""),
            company=str(job.get("company") or ""),
            jdPath=str(jd_path),
            evalPath=str(eval_path),
        )

        if not jd_path.exists():
            failed_jobs.append({"jobId": job_id, "reason": "jd_not_found"})
            append_run_log(log_path, "error", "job_failed", jobId=job_id, reason="jd_not_found")
            continue

        match_score = str(job.get("matchScore") or "中")
        message = run_generate_message(jd_path, resume_path, match_score)
        if not message:
            failed_jobs.append({"jobId": job_id, "reason": "message_generation_failed"})
            append_run_log(log_path, "error", "job_failed", jobId=job_id, reason="message_generation_failed")
            continue

        eval_payload = safe_load_dict(eval_path)
        eval_payload["fit"] = bool(eval_payload.get("fit", True))
        eval_payload["jobUrl"] = job_url or eval_payload.get("jobUrl")
        eval_payload["jobId"] = job_id
        eval_payload["jobTitle"] = job.get("title") or eval_payload.get("jobTitle") or ""
        eval_payload["company"] = job.get("company") or eval_payload.get("company") or ""
        eval_payload["salary"] = job.get("salary") or eval_payload.get("salary") or ""
        eval_payload["matchScore"] = match_score
        eval_payload["reasoning"] = job.get("reasoning") or eval_payload.get("reasoning") or ""
        eval_payload["messageDraft"] = message
        eval_payload["message"] = message
        eval_payload["updatedAt"] = now_iso()
        dump_json(eval_path, eval_payload)

        next_job = dict(job)
        next_job["jobId"] = job_id
        next_job["jdPath"] = str(jd_path)
        next_job["evalPath"] = str(eval_path)
        next_job["messageDraft"] = message
        kept_jobs.append(next_job)
        append_run_log(
            log_path,
            "info",
            "job_message_generated",
            jobId=job_id,
            messageLength=len(message),
            evalPath=str(eval_path),
        )

    skip_reasons = summary.get("skipReasons") if isinstance(summary.get("skipReasons"), dict) else {}
    if failed_jobs:
        skip_reasons["message_generation_failed"] = int(skip_reasons.get("message_generation_failed", 0)) + len(failed_jobs)
    summary["skipReasons"] = skip_reasons

    summary["fitJobs"] = kept_jobs
    summary["fitCount"] = len(kept_jobs)
    summary["messageGenerated"] = len(kept_jobs)

    total_evaluated = summary.get("totalEvaluated")
    if isinstance(total_evaluated, int) and total_evaluated >= 0:
        summary["skipCount"] = max(0, total_evaluated - len(kept_jobs))
    else:
        old_skip = summary.get("skipCount")
        summary["skipCount"] = int(old_skip) if isinstance(old_skip, int) and old_skip >= 0 else len(failed_jobs)

    summary["completedAt"] = now_iso()
    dump_json(eval_summary_path, summary)
    append_run_log(
        log_path,
        "info",
        "stage2_summary",
        fitCount=len(kept_jobs),
        messageGenerated=len(kept_jobs),
        failedToGenerate=len(failed_jobs),
        evalSummary=str(eval_summary_path),
    )

    report = {
        "type": "boss-analyze",
        "evalSummaryPath": str(eval_summary_path),
        "fitCount": len(kept_jobs),
        "messageGenerated": len(kept_jobs),
        "failedToGenerate": len(failed_jobs),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
