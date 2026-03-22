#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOSS直聘自动投递编排（拆分版）

流程：
  scrape_jd -> analyzer_worker(仅分析) -> (fit=true 才 sender_worker) -> state 更新 -> reconcile

说明：
- Analyzer Worker：绝不发送、绝不写 DB
- Sender Worker：仅发送，不做 fit 判断
- 最终口径：reconcile_receipt.py --write-back
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
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE = Path(__file__).resolve().parents[3]
SCRIPTS = {
    "search_pipeline": WORKSPACE / "skills/boss-zhipin-search/scripts/run_pipeline.py",
    "rank": WORKSPACE / "skills/boss-auto-applier/scripts/rank_jobs.py",
    "reconcile": WORKSPACE / "skills/boss-auto-applier/scripts/reconcile_receipt.py",
    "scrape_jd": WORKSPACE / "skills/jd-greeting-generator/scripts/scrape_jd.py",
    "analyzer_worker": WORKSPACE / "skills/boss-auto-applier/scripts/analyzer_worker.py",
    "sender_worker": WORKSPACE / "skills/boss-auto-applier/scripts/sender_worker.py",
    "self_heal_agent": WORKSPACE / "skills/boss-auto-applier/scripts/self_heal_agent.py",
}


def sh(cmd: List[str], *, timeout: Optional[int] = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=check)


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

    for i in reversed(starts):
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


def write_state(state_path: Path, state: Dict[str, Any]) -> None:
    dump_json(state_path, state)


def write_receipt(receipt_path: Path, receipt: Dict[str, Any]) -> None:
    dump_json(receipt_path, receipt)


def bump(d: Dict[str, int], k: str, n: int = 1) -> None:
    d[k] = int(d.get(k, 0)) + n


def append_phase(job_obj: Dict[str, Any], phase: str) -> None:
    phases = job_obj.get("phase")
    if not isinstance(phases, list):
        phases = []
    if phase not in phases:
        phases.append(phase)
    job_obj["phase"] = phases


def classify_send_failure(status: str, error_text: str) -> str:
    text = f"{status}\n{error_text}".lower()
    if "target_context_mismatch" in text:
        return "target_context_mismatch"
    if "send_unverified" in text:
        return "send_unverified"
    if "chat_navigation_failed" in text:
        return "chat_navigation_failed"
    if "navigation_mismatch" in text:
        return "navigation_mismatch"
    if "err_aborted" in text or "open_failed" in text or "preflight_recover_failed" in text:
        return "open_failed"
    if "preflight_failed" in text:
        return "preflight_failed"
    if "verify_blocked" in text or "captcha" in text or "security_check" in text:
        return "verification_timeout"
    return status or "send_failed"


def should_retry_send(status: str, error_text: str, stderr_summary: str) -> bool:
    text = f"{status}\n{error_text}\n{stderr_summary}".lower()
    transient_markers = (
        "err_aborted",
        "cdp endpoint is not reachable",
        "execution context was destroyed",
        "no page found",
        "open_failed",
        "navigation_mismatch",
        "preflight_failed",
        "url_mismatch",
        "target_context_mismatch_retry",
    )
    return any(marker in text for marker in transient_markers)


def run_scrape_jd(job_url: str, jd_path: Path, cdp_port: int) -> Dict[str, Any]:
    cp = sh([
        sys.executable,
        str(SCRIPTS["scrape_jd"]),
        "--job-url",
        job_url,
        "--cdp-port",
        str(cdp_port),
    ])
    out = cp.stdout.strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        data = {"status": "failed", "error": "invalid_json", "_raw": out[-2000:]}
    dump_json(jd_path, data)
    return data


def run_analyzer_worker(jd_json: Path, resume: Path, prefs: Path, out_path: Path) -> Dict[str, Any]:
    cp = sh([
        sys.executable,
        str(SCRIPTS["analyzer_worker"]),
        "--jd-json",
        str(jd_json),
        "--resume",
        str(resume),
        "--prefs",
        str(prefs),
        "--out",
        str(out_path),
    ])
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


def run_sender_worker(
    job_url: str,
    eval_json: Path,
    send_out: Path,
    cdp_port: int,
    screenshot_dir: Path,
    *,
    allow_intent_failed_retry: bool = False,
    no_retry: bool = True,
    capture_screenshot: bool = False,
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(SCRIPTS["sender_worker"]),
        "--job-url",
        job_url,
        "--eval-json",
        str(eval_json),
        "--send-out",
        str(send_out),
        "--cdp-port",
        str(cdp_port),
        "--screenshot-dir",
        str(screenshot_dir),
    ]
    if allow_intent_failed_retry:
        cmd.append("--allow-intent-failed-retry")
    if no_retry:
        cmd.append("--no-retry")
    if capture_screenshot:
        cmd.append("--capture-screenshot")
    cp = sh(cmd)
    if send_out.exists():
        try:
            return load_json(send_out)
        except Exception:
            pass

    out = cp.stdout.strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        data = {
            "sendResult": {
                "status": "failed",
                "error": "sender_invalid_json",
                "jobUrl": job_url,
            },
            "stderrSummary": out[-2000:] if out else "",
            "exitCode": cp.returncode,
        }
    dump_json(send_out, data)
    return data


def run_self_heal_agent(
    job_url: str,
    status: str,
    error_text: str,
    stderr_summary: str,
    cdp_port: int,
    out_path: Path,
) -> Dict[str, Any]:
    cp = sh([
        sys.executable,
        str(SCRIPTS["self_heal_agent"]),
        "--job-url",
        job_url,
        "--status",
        status or "failed",
        "--error-text",
        error_text or "",
        "--stderr-summary",
        stderr_summary or "",
        "--cdp-port",
        str(cdp_port),
    ])
    out = (cp.stdout or "").strip()
    try:
        data = extract_json_from_mixed_output(out)
    except Exception:
        data = {
            "applied": False,
            "retryRecommended": False,
            "failureKind": "invalid_json",
            "error": "self_heal_invalid_json",
            "raw": out[-2000:] if out else "",
        }
    if not isinstance(data, dict):
        data = {
            "applied": False,
            "retryRecommended": False,
            "failureKind": "invalid_payload",
            "error": "self_heal_invalid_payload",
        }
    data["exitCode"] = cp.returncode
    dump_json(out_path, data)
    return data


def search_and_rank(run_dir: Path, keyword: str, city: str, page_limit: int, max_apply: int, cdp_port: int, url_filters: Dict[str, str] = None) -> Tuple[Path, Path, Dict[str, Any]]:
    outdir = run_dir / f"search_p{page_limit}"
    outdir.mkdir(parents=True, exist_ok=True)
    filters_path = outdir / "filters.json"
    filters_data: Dict[str, str] = {"keyword": keyword, "city": city}
    # Merge URL filter params from candidate-preferences.json (server-side filtering)
    if url_filters:
        for k in ("salary", "experience", "degree", "jobType", "scale"):
            if url_filters.get(k):
                filters_data[k] = url_filters[k]
    dump_json(filters_path, filters_data)

    cp = sh([
        sys.executable,
        str(SCRIPTS["search_pipeline"]),
        "--input",
        str(filters_path),
        "--outdir",
        str(outdir),
        "--cdp-port",
        str(cdp_port),
        "--headed",
        "--page-limit",
        str(page_limit),
        "--max-jobs",
        str(max(120, page_limit * 50)),
    ])
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "logs" / f"search_p{page_limit}.log").write_text(cp.stdout or "", encoding="utf-8")

    jobs_path = outdir / "jobs.json"
    ranked_path = run_dir / f"ranked_p{page_limit}.json"

    if not jobs_path.exists():
        # Keep orchestration resilient: downstream loop can continue safely.
        dump_json(ranked_path, [])
        return jobs_path, ranked_path, {
            "count": 0,
            "searchExitCode": cp.returncode,
            "searchError": "jobs_json_missing",
        }

    cp2 = sh([
        sys.executable,
        str(SCRIPTS["rank"]),
        "--jobs",
        str(jobs_path),
        "--resume",
        str(WORKSPACE / "candidate-resume.md"),
        "--db",
        str(WORKSPACE / "data/boss_greeting.db"),
        "--output",
        str(ranked_path),
        "--city",
        city,
        "--max-count",
        str(max_apply * 3),
    ])
    (run_dir / "logs" / f"rank_p{page_limit}.log").write_text(cp2.stdout or "", encoding="utf-8")
    if not ranked_path.exists():
        dump_json(ranked_path, [])

    meta: Dict[str, Any] = {}
    try:
        meta = load_json(jobs_path)
    except Exception:
        meta = {}

    return jobs_path, ranked_path, meta


def build_receipt(run_dir: Path, state_path: Path, receipt_path: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    sent = int((state.get("stats") or {}).get("sent") or 0)
    max_apply = int(state.get("maxApply") or 0)
    overall = "success" if sent >= max_apply and max_apply > 0 else ("partial" if sent > 0 else "failed")
    return {
        "type": "boss-apply",
        "platform": "boss",
        "overallStatus": overall,
        "runDir": str(run_dir),
        "keyword": state.get("keyword"),
        "city": state.get("city"),
        "stats": state.get("stats") or {},
        "skipReasons": state.get("skipReasons") or {},
        "failReasons": state.get("failReasons") or {},
        "appliedJobs": [j for j in (state.get("jobs") or []) if j.get("state") == "sent"],
        "receipt": str(receipt_path),
        "state": str(state_path),
        "statusDb": str(WORKSPACE / "data/boss_greeting.db"),
        "finishedAt": now_iso(),
    }


def run_reconcile_and_assert(run_dir: Path) -> Path:
    state_path = run_dir / "state.json"
    receipt_path = run_dir / "receipt.json"
    missing = [str(p) for p in (state_path, receipt_path) if not p.exists()]
    if missing:
        raise RuntimeError(f"hard_fail_missing_state_or_receipt: {', '.join(missing)}")

    cp = sh([
        sys.executable,
        str(SCRIPTS["reconcile"]),
        str(run_dir),
        "--write-back",
    ])
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "logs" / "reconcile.log").write_text(cp.stdout or "", encoding="utf-8")
    if cp.returncode != 0:
        raise RuntimeError(f"reconcile_failed_exit_{cp.returncode}")

    reconciled_path = run_dir / "reconciled_receipt.json"
    if not reconciled_path.exists():
        raise RuntimeError("hard_fail_missing_reconciled_receipt")

    receipt = load_json(receipt_path)
    stats_source = receipt.get("stats_source")
    if stats_source not in {"reconciled_send_state_db", "reconciled_db_first"}:
        raise RuntimeError(f"hard_fail_receipt_stats_source:{stats_source}")

    return reconciled_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--max-apply", type=int, default=10)
    ap.add_argument("--cdp-port", type=int, default=18801)
    ap.add_argument("--min-interval", type=int, default=1)
    ap.add_argument("--max-interval", type=int, default=2)
    ap.add_argument("--selection-mode", choices=["broadcast", "fit"], default="broadcast",
                    help="broadcast=广撒网(默认，抓到即投); fit=仅投匹配岗位")
    ap.add_argument("--retry-on-fail", action="store_true", help="Enable one self-heal retry on send failure")
    ap.add_argument("--capture-screenshot", action="store_true", help="Capture screenshots during send")
    ap.add_argument("--dry-run", action="store_true", help="Analyze only, never call sender")
    args = ap.parse_args()
    if args.max_interval < args.min_interval:
        args.min_interval, args.max_interval = args.max_interval, args.min_interval

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jd").mkdir(exist_ok=True)
    (run_dir / "eval").mkdir(exist_ok=True)
    (run_dir / "send").mkdir(exist_ok=True)
    (run_dir / "repair").mkdir(exist_ok=True)

    resume_path = WORKSPACE / "candidate-resume.md"
    prefs_path = WORKSPACE / "candidate-preferences.json"

    # Load URL filter params from preferences (server-side BOSS search filtering)
    url_filters: Dict[str, str] = {}
    try:
        prefs_data = load_json(prefs_path)
        if isinstance(prefs_data, dict):
            url_filters = prefs_data.get("urlFilters", {})
    except Exception:
        pass

    state_path = run_dir / "state.json"
    receipt_path = run_dir / "receipt.json"

    max_apply = max(0, int(args.max_apply))
    state: Dict[str, Any] = {
        "runId": run_dir.name,
        "keyword": args.keyword,
        "city": args.city,
        "maxApply": max_apply,
        "dryRun": bool(args.dry_run),
        "startedAt": now_iso(),
        "stats": {
            "searchRounds": 0,
            "searched": 0,
            "ranked": 0,
            "evaluated": 0,
            "fit": 0,
            "selected": 0,
            "sendAttempts": 0,
            "repairsTriggered": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "alreadyContacted": 0,
            "invalid": 0,
        },
        "jobs": [],
        "skipReasons": {},
        "failReasons": {},
    }

    def persist_state() -> None:
        write_state(state_path, state)

    seen_links = set()
    page_limits = [1, 3, 6]

    try:
        persist_state()

        for page_limit in page_limits:
            if state["stats"]["sendAttempts"] >= max_apply and max_apply > 0:
                break

            if max_apply == 0:
                break

            _, ranked_path, meta = search_and_rank(
                run_dir, args.keyword, args.city, page_limit, max_apply, args.cdp_port,
                url_filters=url_filters,
            )
            state["stats"]["searchRounds"] += 1
            state["stats"]["searched"] += int((meta or {}).get("count") or 0)

            ranked_jobs = load_json(ranked_path)
            if isinstance(ranked_jobs, dict) and "jobs" in ranked_jobs:
                ranked_jobs = ranked_jobs["jobs"]
            if not isinstance(ranked_jobs, list):
                ranked_jobs = []
            state["stats"]["ranked"] += len(ranked_jobs)

            for job in ranked_jobs:
                if state["stats"]["sendAttempts"] >= max_apply and max_apply > 0:
                    break

                job_url = (job.get("link") or "").strip()
                title = job.get("title") or ""
                company = job.get("company") or ""
                city = job.get("city") or args.city
                salary = job.get("salary")

                if not is_valid_job_url(job_url) or job_url in seen_links:
                    state["stats"]["invalid"] += 1
                    state["stats"]["skipped"] += 1
                    bump(state["skipReasons"], "invalid_or_duplicate")
                    state["jobs"].append({
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "city": city,
                        "link": job_url,
                        "state": "invalid_or_duplicate",
                        "phase": [],
                        "at": now_iso(),
                    })
                    persist_state()
                    continue

                seen_links.add(job_url)
                job_id = extract_job_id(job_url)

                # 1) scrape JD
                jd_path = run_dir / "jd" / f"{job_id}.json"
                jd_data = run_scrape_jd(job_url, jd_path, cdp_port=args.cdp_port)
                if jd_data.get("status") != "ok":
                    time.sleep(5)
                    jd_data = run_scrape_jd(job_url, jd_path, cdp_port=args.cdp_port)

                if jd_data.get("status") != "ok":
                    state["stats"]["failed"] += 1
                    bump(state["failReasons"], "scrape_failed")
                    state["jobs"].append({
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "city": city,
                        "link": job_url,
                        "jobId": job_id,
                        "state": "scrape_failed",
                        "phase": [],
                        "error": jd_data.get("error"),
                        "at": now_iso(),
                    })
                    persist_state()
                    continue

                # 2) analyzer worker (analysis only)
                analysis_path = run_dir / "eval" / f"{job_id}.analysis.json"
                analyzer = run_analyzer_worker(jd_path, resume_path, prefs_path, analysis_path)
                state["stats"]["evaluated"] += 1

                fit = bool(analyzer.get("fit"))
                match_score = analyzer.get("matchScore") or "低"
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

                eval_payload = {
                    "fit": selected_for_send,
                    "fitByAnalyzer": fit,
                    "matchScore": normalize_match_score(match_score),
                    "skipReason": skip_reason,
                    "reasoning": reasoning,
                    "messageDraft": message_draft,
                    "message": message_draft,
                    "jobUrl": job_url,
                    "jobTitle": jd_data.get("jobTitle") or title,
                    "company": jd_data.get("company") or company,
                    "salary": jd_data.get("salary") or salary,
                    "evaluatedAt": now_iso(),
                }
                eval_path = run_dir / "eval" / f"{job_id}.json"
                dump_json(eval_path, eval_payload)

                job_entry: Dict[str, Any] = {
                    "title": eval_payload.get("jobTitle"),
                    "company": eval_payload.get("company"),
                    "salary": eval_payload.get("salary"),
                    "city": city,
                    "link": job_url,
                    "jobId": job_id,
                    "state": "pending",
                    "matchScore": eval_payload.get("matchScore"),
                    "sentAt": None,
                    "error": None,
                    "phase": [],
                    "at": now_iso(),
                }
                append_phase(job_entry, "analyzed")

                if not selected_for_send:
                    state["stats"]["skipped"] += 1
                    bump(state["skipReasons"], skip_reason or "fit_false")
                    job_entry["state"] = "skipped"
                    job_entry["reason"] = skip_reason
                    state["jobs"].append(job_entry)
                    persist_state()
                    continue

                state["stats"]["fit"] += 1
                state["stats"]["selected"] += 1
                state["stats"]["sendAttempts"] += 1

                # 3) sender worker (send only) for selected jobs
                if args.dry_run:
                    state["stats"]["skipped"] += 1
                    bump(state["skipReasons"], "dry_run")
                    job_entry["state"] = "dry_run_done"
                    job_entry["reason"] = "dry_run"
                    state["jobs"].append(job_entry)
                    persist_state()
                    continue

                send_path = run_dir / "send" / f"{job_id}.json"
                screenshot_dir = run_dir / "send" / "screenshots"
                sender: Dict[str, Any] = {}
                send_result: Dict[str, Any] = {}
                status = "failed"
                stderr_summary = ""
                max_send_attempts = 2 if args.retry_on_fail else 1
                for attempt in range(max_send_attempts):
                    sender = run_sender_worker(
                        job_url,
                        eval_path,
                        send_path,
                        args.cdp_port,
                        screenshot_dir,
                        allow_intent_failed_retry=(attempt > 0),
                        no_retry=(not args.retry_on_fail),
                        capture_screenshot=args.capture_screenshot,
                    )
                    send_result = sender.get("sendResult") if isinstance(sender, dict) else {}
                    if not isinstance(send_result, dict):
                        send_result = {"status": "failed", "error": "invalid_sender_result"}
                    status = send_result.get("status") or "failed"
                    stderr_summary = sender.get("stderrSummary") if isinstance(sender, dict) else ""
                    if status in {"ok", "sent", "already_contacted", "skipped"}:
                        break
                    if attempt == 0 and args.retry_on_fail:
                        err_text = str(send_result.get("error") or "")
                        repair_path = run_dir / "repair" / f"{job_id}_attempt1.json"
                        repair = run_self_heal_agent(
                            job_url=job_url,
                            status=status,
                            error_text=err_text,
                            stderr_summary=str(stderr_summary or ""),
                            cdp_port=args.cdp_port,
                            out_path=repair_path,
                        )
                        if bool(repair.get("applied")):
                            state["stats"]["repairsTriggered"] += 1
                        retry_by_repair = bool(repair.get("retryRecommended"))
                        retry_by_transient = should_retry_send(status, err_text, str(stderr_summary or ""))
                        if retry_by_repair or retry_by_transient:
                            time.sleep(1.5)
                            continue
                    break
                append_phase(job_entry, "sent")

                if not isinstance(send_result, dict):
                    send_result = {"status": "failed", "error": "invalid_sender_result"}

                if status in {"ok", "sent"}:
                    state["stats"]["sent"] += 1
                    job_entry["state"] = "sent"
                    job_entry["sentAt"] = now_iso()
                elif status == "already_contacted":
                    state["stats"]["alreadyContacted"] += 1
                    state["stats"]["skipped"] += 1
                    bump(state["skipReasons"], "already_contacted")
                    job_entry["state"] = "already_contacted"
                    job_entry["reason"] = "already_contacted"
                elif status == "skipped":
                    reason = str(send_result.get("reason") or "skipped")
                    state["stats"]["skipped"] += 1
                    if reason == "already_contacted":
                        state["stats"]["alreadyContacted"] += 1
                        job_entry["state"] = "already_contacted"
                    else:
                        job_entry["state"] = "skipped"
                    bump(state["skipReasons"], reason)
                    job_entry["reason"] = reason
                else:
                    err = str(send_result.get("error") or "")
                    fail_reason = classify_send_failure(status, err + "\n" + str(stderr_summary or ""))
                    state["stats"]["failed"] += 1
                    bump(state["failReasons"], fail_reason)
                    job_entry["state"] = "verification_timeout" if fail_reason == "verification_timeout" else "send_failed"
                    job_entry["reason"] = fail_reason

                    # 强约束：target_context_mismatch/send_unverified/chat_navigation_failed 一律 failed，不记 sent
                    if fail_reason in {"target_context_mismatch", "send_unverified", "chat_navigation_failed"}:
                        job_entry["state"] = "send_failed"

                job_entry["error"] = send_result.get("error") or (str(stderr_summary)[:1000] if stderr_summary else None)
                state["jobs"].append(job_entry)
                persist_state()

                if job_entry["state"] == "verification_timeout":
                    break

                if job_entry["state"] == "sent":
                    time.sleep(random.randint(args.min_interval, args.max_interval))

        state["finishedAt"] = now_iso()
        persist_state()

        receipt = build_receipt(run_dir, state_path, receipt_path, state)
        write_receipt(receipt_path, receipt)

        run_reconcile_and_assert(run_dir)

        # phase 轨迹补齐：所有已处理岗位在最终口径后标记 reconciled
        for job_obj in state.get("jobs", []):
            append_phase(job_obj, "reconciled")
        persist_state()

        print(str(receipt_path))
        return 0
    except Exception as exc:
        print(f"orchestrate_apply_failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
