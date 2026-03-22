#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reconcile receipt with state/send artifacts and DB for a given run directory.

This script performs reconciliation across:
1. state.json - runtime state tracking (current run)
2. send/*.json - sender result artifacts (current run)
3. receipt.json - final summary
4. boss_greeting.db - persistent storage (cross-run)

Output: reconciled_receipt.json with:
- reconciled_stats: sent/failed/skipped/already_contacted/unknown
- inconsistencies: list of each discrepancy (job_url, state_value, send_value, db_value, evidence)
- overall_status: 'consistent' or 'partial_with_inconsistency'

Design note:
- For run-scoped stats, current-run evidence (send/state) should take precedence
  over historical DB status to avoid false failure inflation (e.g. intent_already_failed).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE = str(Path(__file__).resolve().parents[3])
DB_PATH = os.environ.get("BOSS_GREETING_DB", f"{WORKSPACE}/data/boss_greeting.db")


def log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


def load_state(run_dir: str) -> Dict[str, Any]:
    """Load state.json from run directory (hard fail if missing)."""
    state_path = os.path.join(run_dir, "state.json")
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"state.json not found in {run_dir}")
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_receipt(run_dir: str) -> Dict[str, Any]:
    """Load receipt.json from run directory (hard fail if missing)."""
    receipt_path = os.path.join(run_dir, "receipt.json")
    if not os.path.exists(receipt_path):
        raise FileNotFoundError(f"receipt.json not found in {run_dir}")
    with open(receipt_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_db_records(job_urls: List[str]) -> Dict[str, Dict[str, Any]]:
    """Get greeting records from DB for given job URLs."""
    if not job_urls:
        return {}
    
    conn = sqlite3.connect(DB_PATH)
    try:
        placeholders = ",".join(["?"] * len(job_urls))
        query = f"SELECT job_url, job_title, company, status, sent_at, error FROM greetings WHERE job_url IN ({placeholders})"
        rows = conn.execute(query, job_urls).fetchall()
        
        records = {}
        for row in rows:
            job_url = row[0]
            records[job_url] = {
                "job_url": job_url,
                "job_title": row[1],
                "company": row[2],
                "status": row[3],
                "sent_at": row[4],
                "error": row[5],
            }
        return records
    finally:
        conn.close()


def load_send_results(run_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load send/*.json and build map by job_url."""
    send_dir = Path(run_dir) / "send"
    if not send_dir.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for fp in send_dir.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        send_result = data.get("sendResult")
        if not isinstance(send_result, dict):
            continue
        job_url = str(send_result.get("jobUrl") or "").strip()
        if not job_url:
            continue
        out[job_url] = {
            "status": send_result.get("status"),
            "reason": send_result.get("reason"),
            "error": send_result.get("error"),
            "path": str(fp),
        }
    return out


def extract_job_urls_from_state(state: Dict[str, Any]) -> List[str]:
    """Extract all job URLs from state.json."""
    urls = []
    jobs = state.get("jobs", [])
    for job in jobs:
        link = job.get("link", "")
        if link:
            urls.append(link)
    return urls


def normalize_status(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in {"ok", "sent"}:
        return "sent"
    if s in {"already_contacted"}:
        return "already_contacted"
    if s in {"skipped", "invalid_or_duplicate", "dry_run_done"}:
        return "skipped"
    if s in {"failed", "send_failed", "scrape_failed", "analyze_failed", "verification_timeout"}:
        return "failed"
    return "unknown"


def normalize_send_status(raw_status: str, raw_reason: str) -> str:
    status = normalize_status(raw_status)
    reason = str(raw_reason or "").strip().lower()
    if status == "skipped" and reason == "already_contacted":
        return "already_contacted"
    return status


def is_expected_send_db_divergence(send_status: str, send_reason: str, db_status: str) -> bool:
    """Known valid cross-run divergence patterns."""
    if send_status == "skipped" and send_reason.startswith("intent_already_failed") and db_status == "failed":
        return True
    if send_status == "skipped" and send_reason.startswith("already_sent_by_") and db_status in {"sent", "already_contacted"}:
        return True
    if send_status == "already_contacted" and db_status in {"already_contacted", "sent"}:
        return True
    return False


def reconcile(run_dir: str) -> Dict[str, Any]:
    """Perform three-way reconciliation."""
    log("INFO", f"Reconciling run: {run_dir}")
    
    # Load data sources
    state = load_state(run_dir)
    receipt = load_receipt(run_dir)
    
    # Get job URLs from state
    job_urls = extract_job_urls_from_state(state)
    log("INFO", f"Found {len(job_urls)} jobs in state.json")
    
    # Get DB records
    db_records = get_db_records(job_urls)
    log("INFO", f"Found {len(db_records)} records in DB")
    send_records = load_send_results(run_dir)
    log("INFO", f"Found {len(send_records)} send artifacts")
    
    # Build reconciliation
    inconsistencies = []
    stats = {
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "already_contacted": 0,
        "unknown": 0,
    }
    
    # Process each job from state
    state_jobs = {job.get("link"): job for job in state.get("jobs", [])}
    
    for job_url in job_urls:
        state_job = state_jobs.get(job_url, {})
        state_status_raw = state_job.get("state", "unknown")
        state_reason = str(state_job.get("reason") or "")
        state_status = normalize_status(state_status_raw)
        db_record = db_records.get(job_url, {})
        db_status = normalize_status(db_record.get("status", "unknown"))
        send_record = send_records.get(job_url, {})
        send_status = normalize_send_status(
            send_record.get("status", "unknown"),
            send_record.get("reason", ""),
        )
        send_reason = str(send_record.get("reason") or "")

        # Determine final status (current-run send/state first, DB as fallback)
        if send_status != "unknown":
            final_status = send_status
        elif state_status != "unknown":
            final_status = state_status
        elif db_status != "unknown":
            final_status = db_status
        else:
            final_status = "unknown"

        # state vs send inconsistency (both current-run sources)
        if send_status != "unknown" and state_status != "unknown" and send_status != state_status:
            inconsistencies.append({
                "job_url": job_url,
                "state_value": state_status_raw,
                "send_value": send_record.get("status"),
                "db_value": db_record.get("status", "unknown"),
                "evidence": f"state.state={state_status_raw}, send.status={send_record.get('status')}",
            })

        # send vs DB inconsistency (allow known expected divergences)
        if (
            send_status != "unknown"
            and db_status != "unknown"
            and send_status != db_status
            and not is_expected_send_db_divergence(send_status, send_reason, db_status)
        ):
            inconsistencies.append({
                "job_url": job_url,
                "state_value": state_status_raw,
                "send_value": send_record.get("status"),
                "db_value": db_record.get("status", "unknown"),
                "evidence": f"send.status={send_record.get('status')}, db.status={db_record.get('status', 'unknown')}",
            })

        # If no send evidence, fall back to state-vs-db check.
        if send_status == "unknown" and state_status != "unknown" and db_status != "unknown" and state_status != db_status:
            benign_skip = state_status == "skipped"
            benign_contact = state_status == "already_contacted" and db_status in {"already_contacted", "sent"}
            benign_dry_run = state_reason == "dry_run"
            if not (benign_skip or benign_contact or benign_dry_run):
                inconsistencies.append({
                    "job_url": job_url,
                    "state_value": state_status_raw,
                    "send_value": "unknown",
                    "db_value": db_record.get("status", "unknown"),
                    "evidence": f"state.state={state_status_raw}, db.status={db_record.get('status', 'unknown')}",
                })
        
        # Count stats with conservative sent logic:
        # "sent" cannot be determined by state-only evidence.
        if final_status == "sent":
            # If only state says sent (no send/db evidence), keep conservative.
            if send_status != "sent" and db_status != "sent":
                pass
            else:
                stats["sent"] += 1
        elif final_status == "failed":
            stats["failed"] += 1
        elif final_status == "skipped":
            stats["skipped"] += 1
        elif final_status == "already_contacted":
            stats["already_contacted"] += 1
        else:
            stats["unknown"] += 1
    
    # Also check receipt stats
    receipt_stats = receipt.get("stats", {})
    log("INFO", f"Receipt stats: {receipt_stats}")
    
    # Determine overall status
    overall_status = "consistent" if not inconsistencies else "partial_with_inconsistency"
    
    result = {
        "run_dir": run_dir,
        "run_id": os.path.basename(run_dir),
        "reconciled_stats": stats,
        "receipt_stats": receipt_stats,
        "inconsistencies": inconsistencies,
        "overall_status": overall_status,
        "reconciled_at": datetime.now().isoformat(),
    }
    
    log("INFO", f"Reconciliation complete: {overall_status}")
    log("INFO", f"Stats: {stats}")
    log("INFO", f"Inconsistencies: {len(inconsistencies)}")
    
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile receipt with state and DB")
    ap.add_argument("run_dir", help="Run directory path")
    ap.add_argument("--output", "-o", help="Output JSON path (default: reconciled_receipt.json in run_dir)")
    ap.add_argument("--write-back", action="store_true", default=False, help="Write reconciliation results back to receipt.json")
    args = ap.parse_args()
    
    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        print(f"Error: {run_dir} is not a directory", file=sys.stderr)
        sys.exit(1)
    
    try:
        result = reconcile(run_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Write output
    output_path = args.output
    if not output_path:
        output_path = os.path.join(run_dir, "reconciled_receipt.json")
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    log("INFO", f"Reconciliation result written to: {output_path}")
    
    # Write back to receipt.json if --write-back is set
    if args.write_back:
        receipt_path = os.path.join(run_dir, "receipt.json")
        if os.path.exists(receipt_path):
            with open(receipt_path, "r", encoding="utf-8") as f:
                receipt = json.load(f)
            
            # Get reconciled stats (snake_case)
            reconciled_stats = result["reconciled_stats"]
            
            # Update receipt with reconciliation results
            receipt["reconciled"] = True
            receipt["reconciled_stats"] = reconciled_stats
            receipt["inconsistencies"] = result["inconsistencies"]
            
            # Override stats with reconciled values (use camelCase for receipt.stats)
            receipt["stats"] = {
                "sent": reconciled_stats.get("sent", 0),
                "failed": reconciled_stats.get("failed", 0),
                "skipped": reconciled_stats.get("skipped", 0),
                "alreadyContacted": reconciled_stats.get("already_contacted", 0),
            }
            
            # Set overallStatus: if inconsistency exists, use partial_with_inconsistency
            if result["inconsistencies"]:
                receipt["overallStatus"] = "partial_with_inconsistency"
            else:
                receipt["overallStatus"] = result["overall_status"]
            
            # Mark stats source
            receipt["stats_source"] = "reconciled_send_state_db"
            
            receipt["reconciled_at"] = result["reconciled_at"]
            
            # Write back
            with open(receipt_path, "w", encoding="utf-8") as f:
                json.dump(receipt, f, ensure_ascii=False, indent=2)
            
            log("INFO", f"Receipt updated: {receipt_path}")
        else:
            print(f"Error: receipt.json not found at {receipt_path}", file=sys.stderr)
            sys.exit(1)
    
    # Print summary
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    sys.exit(0)


if __name__ == "__main__":
    main()
