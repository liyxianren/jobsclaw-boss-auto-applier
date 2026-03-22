#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke validation for a run directory.

This script validates:
1. All job links in runDir are job_detail format
2. receipt/state fields are complete
3. Execute reconcile_receipt.py and output summary
4. Output PASS/FAIL with specific failure items
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

WORKSPACE = str(Path(__file__).resolve().parents[3])


def log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


def load_json(path: str) -> Dict[str, Any]:
    """Load JSON file."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_job_links(run_dir: str) -> Tuple[bool, List[str]]:
    """Validate all job links are job_detail format."""
    state = load_json(os.path.join(run_dir, "state.json"))
    jobs = state.get("jobs", [])
    
    failures = []
    for job in jobs:
        link = job.get("link", "")
        if not link:
            failures.append(f"Empty link for job: {job.get('title', 'unknown')}")
        elif "/job_detail/" not in link:
            failures.append(f"Invalid link format: {link} (expected job_detail)")
    
    is_valid = len(failures) == 0
    return is_valid, failures


def validate_receipt_fields(run_dir: str) -> Tuple[bool, List[str]]:
    """Validate receipt and state fields are complete."""
    receipt = load_json(os.path.join(run_dir, "receipt.json"))
    state = load_json(os.path.join(run_dir, "state.json"))
    
    failures = []
    
    # Check receipt required fields
    required_receipt = ["type", "overallStatus", "stats"]
    for field in required_receipt:
        if field not in receipt:
            failures.append(f"receipt.json missing field: {field}")
    
    # Check stats fields
    if "stats" in receipt:
        stats = receipt["stats"]
        required_stats = ["sent", "skipped", "failed"]
        for field in required_stats:
            if field not in stats:
                failures.append(f"receipt.stats missing field: {field}")
    
    # Check state required fields
    required_state = ["runId", "stats", "jobs"]
    for field in required_state:
        if field not in state:
            failures.append(f"state.json missing field: {field}")
    
    # Check each job has required fields
    if "jobs" in state:
        for i, job in enumerate(state["jobs"]):
            required_job = ["title", "link", "state"]
            for field in required_job:
                if field not in job:
                    failures.append(f"state.jobs[{i}] missing field: {field}")
    
    # NEW: Validate stats vs reconciled_stats mapping when reconciled == true
    if receipt.get("reconciled") == True:
        receipt_stats = receipt.get("stats", {})
        reconciled_stats = receipt.get("reconciled_stats", {})
        
        # Map field names: receipt.stats uses camelCase, reconciled_stats uses snake_case
        field_mapping = {
            "sent": "sent",
            "failed": "failed", 
            "skipped": "skipped",
            "alreadyContacted": "already_contacted",
        }
        
        for receipt_field, recon_field in field_mapping.items():
            receipt_val = receipt_stats.get(receipt_field, 0)
            recon_val = reconciled_stats.get(recon_field, 0)
            if receipt_val != recon_val:
                failures.append(
                    f"Stats mismatch: receipt.stats.{receipt_field}={receipt_val} " 
                    f"vs receipt.reconciled_stats.{recon_field}={recon_val}"
                )
    
    is_valid = len(failures) == 0
    return is_valid, failures


def run_reconciliation(run_dir: str) -> Tuple[bool, Dict[str, Any]]:
    """Execute reconcile_receipt.py and return result."""
    reconcile_script = os.path.join(
        WORKSPACE, 
        "skills/boss-auto-applier/scripts/reconcile_receipt.py"
    )
    
    if not os.path.exists(reconcile_script):
        log("WARN", f"Reconcile script not found: {reconcile_script}")
        return False, {"error": "reconcile_script_not_found"}
    
    try:
        result = subprocess.run(
            ["python3", reconcile_script, run_dir],
            capture_output=True,
            text=True,
            timeout=60,
        )
        
        if result.returncode != 0:
            log("ERROR", f"Reconciliation failed: {result.stderr}")
            return False, {"error": result.stderr}
        
        # Parse output JSON
        try:
            recon_result = json.loads(result.stdout)
            return True, recon_result
        except json.JSONDecodeError:
            log("ERROR", "Failed to parse reconciliation output")
            return False, {"error": "parse_error"}
            
    except subprocess.TimeoutExpired:
        log("ERROR", "Reconciliation timeout")
        return False, {"error": "timeout"}
    except Exception as e:
        log("ERROR", f"Reconciliation error: {e}")
        return False, {"error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Smoke validate run directory")
    ap.add_argument("run_dir", help="Run directory path")
    ap.add_argument("--skip-reconcile", action="store_true", help="Skip reconciliation check")
    args = ap.parse_args()
    
    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        print(f"Error: {run_dir} is not a directory", file=sys.stderr)
        sys.exit(1)
    
    log("INFO", f"Running smoke validation on: {run_dir}")
    
    all_failures = []
    
    # 1. Validate job links
    log("INFO", "Step 1: Validating job links...")
    links_valid, link_failures = validate_job_links(run_dir)
    all_failures.extend(link_failures)
    if not links_valid:
        log("WARN", f"Job link validation failed: {len(link_failures)} issues")
    
    # 2. Validate receipt/state fields
    log("INFO", "Step 2: Validating receipt/state fields...")
    fields_valid, field_failures = validate_receipt_fields(run_dir)
    all_failures.extend(field_failures)
    if not fields_valid:
        log("WARN", f"Field validation failed: {len(field_failures)} issues")
    
    # 3. Run reconciliation (unless skipped)
    recon_valid = True
    recon_result = {}
    recon_status_consistent = True
    if not args.skip_reconcile:
        log("INFO", "Step 3: Running reconciliation...")
        recon_valid, recon_result = run_reconciliation(run_dir)
        if not recon_valid:
            all_failures.append(f"Reconciliation failed: {recon_result.get('error', 'unknown')}")
        else:
            # Check if overall_status indicates consistency
            recon_overall = recon_result.get("overall_status", "")
            if recon_overall != "consistent":
                recon_status_consistent = False
                all_failures.append(f"Reconciliation inconsistency: overall_status={recon_overall}, {len(recon_result.get('inconsistencies', []))} issues found")
    else:
        log("INFO", "Step 3: Skipped (--skip-reconcile)")
    
    # Output result
    result = {
        "run_dir": run_dir,
        "run_id": os.path.basename(run_dir),
        "overall_status": "PASS" if len(all_failures) == 0 else "FAIL",
        "checks": {
            "job_links": {"valid": links_valid, "failures": link_failures},
            "receipt_fields": {"valid": fields_valid, "failures": field_failures},
            "reconciliation": {"valid": recon_valid, "result": recon_result},
        },
        "failures": all_failures,
        "validated_at": datetime.now().isoformat(),
    }
    
    # Write result
    output_path = os.path.join(run_dir, "smoke_validation.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    log("INFO", f"Validation result written to: {output_path}")
    
    # Print summary
    print("=" * 60)
    print(f"SMOKE VALIDATION: {result['overall_status']}")
    print("=" * 60)
    
    if all_failures:
        print("\nFailures:")
        for i, failure in enumerate(all_failures, 1):
            print(f"  {i}. {failure}")
    else:
        print("\nAll checks passed!")
    
    print(f"\nDetails: {output_path}")
    
    sys.exit(0 if result["overall_status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
