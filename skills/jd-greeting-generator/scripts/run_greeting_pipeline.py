#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Full greeting pipeline: scrape JD -> match resume -> generate message -> preview -> send.

Usage:
  # Full pipeline (single job, with interactive preview)
  python3 run_greeting_pipeline.py \\
    --job-url "https://www.zhipin.com/job_detail/xxx.html" \\
    --preview

  # Full pipeline (batch from jobs.json)
  python3 run_greeting_pipeline.py \\
    --jobs-file /path/to/jobs.json \\
    --preview --max-send 10

  # No-preview mode (for OpenClaw agent automation)
  python3 run_greeting_pipeline.py \\
    --job-url "https://www.zhipin.com/job_detail/xxx.html" \\
    --no-preview

  # Skip scraping, provide JD directly
  python3 run_greeting_pipeline.py \\
    --jd-file /tmp/jd.json \\
    --preview

  # Dry run (scrape + match + generate, no send)
  python3 run_greeting_pipeline.py \\
    --job-url "https://www.zhipin.com/job_detail/xxx.html" \\
    --dry-run

Output: JSON summary of pipeline execution.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE = str(Path(__file__).resolve().parents[3])
SKILL_DIR = f"{WORKSPACE}/skills/jd-greeting-generator"
DEFAULT_RESUME = f"{WORKSPACE}/candidate-resume.md"

# Add scripts dir to path for imports
sys.path.insert(0, f"{SKILL_DIR}/scripts")

from scrape_jd import scrape_jd
from match_resume import match_resume_jd
from generate_message import build_message
from send_greeting import send_greeting, cleanup_browser, init_db, log, jitter_sleep


# ────────────────────────── Preview display ──────────────────────────

def format_preview(
    jd_data: Dict[str, Any],
    match_data: Dict[str, Any],
    message: str,
    index: int = 0,
    total: int = 1,
) -> str:
    """Format a preview card for terminal display."""
    width = 60
    sep = "-" * width

    title = jd_data.get("jobTitle", "未知岗位")
    company = jd_data.get("company", "未知公司")
    salary = jd_data.get("salary", "")
    recruiter = jd_data.get("recruiter", "")
    recruiter_title = jd_data.get("recruiterTitle", "")

    # Match info
    score = match_data.get("matchScore", "?")
    score_label = {"high": "高", "medium": "中", "low": "低"}.get(score, score)
    matched_kws = match_data.get("matchedKeywords", [])
    gaps = match_data.get("gaps", [])

    # Build match line
    match_parts = []
    for kw in matched_kws[:5]:
        match_parts.append(f"  + {kw}")
    for g in gaps[:3]:
        match_parts.append(f"  - {g}")

    # JD summary (first 120 chars of description)
    desc = jd_data.get("description", "")
    desc_preview = desc[:120].replace("\n", " ") + ("..." if len(desc) > 120 else "")

    # Build preview
    lines = [
        "",
        f"  [{index + 1}/{total}] {title} @ {company} {salary}",
    ]
    if recruiter:
        hr_line = f"  HR: {recruiter}"
        if recruiter_title:
            hr_line += f" ({recruiter_title})"
        lines.append(hr_line)

    lines.append(sep)

    if desc_preview:
        lines.append(f"  JD: {desc_preview}")
        lines.append(sep)

    lines.append(f"  Match: {score_label} ({len(matched_kws)} keywords)")
    for mp in match_parts:
        lines.append(mp)
    lines.append(sep)

    lines.append(f"  Message ({len(message)} chars):")
    # Wrap message for display
    msg_lines = []
    line = ""
    for char in message:
        line += char
        if len(line) >= 50:
            msg_lines.append(f"    {line}")
            line = ""
    if line:
        msg_lines.append(f"    {line}")
    lines.extend(msg_lines)

    lines.append(sep)
    lines.append("  [y] Send  [n] Skip  [e] Edit  [r] Regenerate  [q] Quit")
    lines.append("")

    return "\n".join(lines)


def interactive_confirm(
    jd_data: Dict[str, Any],
    match_data: Dict[str, Any],
    message: str,
    resume_text: str,
    index: int = 0,
    total: int = 1,
    style: str = "professional",
) -> Optional[str]:
    """Show preview and get user confirmation. Returns message to send, or None to skip."""

    current_message = message

    while True:
        preview = format_preview(jd_data, match_data, current_message, index, total)
        print(preview, file=sys.stderr)

        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice in ("y", "yes", ""):
            return current_message
        elif choice in ("n", "no", "skip"):
            return None
        elif choice in ("e", "edit"):
            print("  Paste new message (end with empty line):", file=sys.stderr)
            lines = []
            try:
                while True:
                    line = input()
                    if not line:
                        break
                    lines.append(line)
            except (EOFError, KeyboardInterrupt):
                pass
            if lines:
                current_message = "\n".join(lines)
        elif choice in ("r", "regen", "regenerate"):
            current_message = build_message(jd_data, match_data, resume_text, style=style)
            print("  (Regenerated)", file=sys.stderr)
        elif choice in ("q", "quit", "exit"):
            raise KeyboardInterrupt("User quit")
        else:
            print("  Invalid choice. Use y/n/e/r/q", file=sys.stderr)


# ────────────────────────── Pipeline for one job ──────────────────────────

def pipeline_one(
    job_url: str,
    resume_text: str,
    cdp_port: int = 18801,
    preview: bool = True,
    style: str = "professional",
    jd_data: Optional[Dict[str, Any]] = None,
    screenshot_dir: str = f"{WORKSPACE}/.openclaw-runs/boss-greeting/screenshots",
    index: int = 0,
    total: int = 1,
) -> Dict[str, Any]:
    """Run full pipeline for one job URL."""

    result: Dict[str, Any] = {"jobUrl": job_url}

    try:
        # ── Step 1: Scrape JD (or use provided data) ──
        if jd_data is None:
            log("INFO", f"[Step 1/4] Scraping JD: {job_url}")
            jd_data = scrape_jd(job_url, cdp_port=cdp_port)
            if jd_data.get("status") != "ok":
                result["status"] = "failed"
                result["error"] = f"scrape_failed: {jd_data.get('error', 'unknown')}"
                result["step"] = "scrape"
                return result
        else:
            log("INFO", "[Step 1/4] Using provided JD data")

        result["jobTitle"] = jd_data.get("jobTitle")
        result["company"] = jd_data.get("company")
        result["salary"] = jd_data.get("salary")

        # ── Step 2: Match resume ──
        log("INFO", "[Step 2/4] Matching resume against JD")
        match_data = match_resume_jd(jd_data, resume_text)
        result["matchScore"] = match_data.get("matchScore")
        result["matchedKeywords"] = match_data.get("matchedKeywords", [])

        if match_data.get("matchScore") == "low":
            log("WARN", f"Low match score ({len(match_data.get('matchedKeywords', []))} keywords). Message quality may be limited.")

        # ── Step 3: Generate message ──
        log("INFO", "[Step 3/4] Generating greeting message")
        message = build_message(jd_data, match_data, resume_text, style=style)
        result["messageGenerated"] = message
        log("INFO", f"Generated message ({len(message)} chars): {message[:80]}...")

        # ── Step 4: Preview & send ──
        if preview:
            log("INFO", "[Step 4/4] Showing preview for confirmation")
            confirmed_message = interactive_confirm(
                jd_data, match_data, message, resume_text,
                index=index, total=total, style=style,
            )
            if confirmed_message is None:
                result["status"] = "skipped"
                result["reason"] = "user_skipped"
                log("INFO", "User skipped this job.")
                return result
            message = confirmed_message
        else:
            log("INFO", "[Step 4/4] No-preview mode, sending directly")

        # ── Send ──
        log("INFO", f"Sending message to {jd_data.get('jobTitle')} @ {jd_data.get('company')}")
        send_result = send_greeting(
            job_url=job_url,
            message=message,
            cdp_port=cdp_port,
            screenshot_dir=screenshot_dir,
            skip_navigation=True,
            no_retry=True,
            capture_screenshot=False,
        )

        result["status"] = send_result.get("status", "failed")
        result["messageSent"] = message
        if send_result.get("screenshotPath"):
            result["screenshotPath"] = send_result["screenshotPath"]
        if send_result.get("error"):
            result["error"] = send_result["error"]

        return result

    except KeyboardInterrupt:
        result["status"] = "aborted"
        result["reason"] = "user_quit"
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        return result


# ────────────────────────── Load jobs from file ──────────────────────────

def load_jobs(jobs_file: str) -> List[Dict[str, Any]]:
    """Load jobs from boss-zhipin-search output (jobs.json)."""
    with open(jobs_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "jobs" in data:
        return data["jobs"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected jobs.json format: {type(data)}")


# ────────────────────────── Main ──────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Full BOSS greeting pipeline: scrape -> match -> generate -> preview -> send",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Input sources
    input_group = ap.add_argument_group("Input")
    input_group.add_argument("--job-url", help="Single BOSS job detail URL")
    input_group.add_argument("--jobs-file", help="Path to jobs.json (batch mode)")
    input_group.add_argument("--jd-file", help="Pre-scraped JD JSON file (skip browser scraping)")
    input_group.add_argument("--resume", default=DEFAULT_RESUME, help="Path to resume markdown")

    # Behavior
    mode_group = ap.add_argument_group("Mode")
    mode_group.add_argument("--preview", action="store_true", default=True,
                           help="Show interactive preview before sending (default)")
    mode_group.add_argument("--no-preview", action="store_true",
                           help="Send without preview (for agent automation)")
    mode_group.add_argument("--dry-run", action="store_true",
                           help="Run pipeline but don't send (test scrape+match+generate)")
    mode_group.add_argument("--style", default="professional",
                           choices=["professional", "casual", "technical"],
                           help="Message style (default: professional)")

    # Limits
    limit_group = ap.add_argument_group("Limits")
    limit_group.add_argument("--max-send", type=int, default=10, help="Max greetings per batch")
    limit_group.add_argument(
        "--pace",
        choices=["fast", "safe"],
        default="fast",
        help="Pace profile for inter-send wait (default: fast)",
    )
    limit_group.add_argument("--min-interval", type=float, default=None, help="Min seconds between sends")
    limit_group.add_argument("--max-interval", type=float, default=None, help="Max seconds between sends")

    # Technical
    tech_group = ap.add_argument_group("Technical")
    tech_group.add_argument("--cdp-port", type=int, default=18801, help="CDP port")
    tech_group.add_argument("--screenshot-dir",
                           default=f"{WORKSPACE}/.openclaw-runs/boss-greeting/screenshots")

    args = ap.parse_args()

    # Resolve pace defaults only when explicit interval values are not provided.
    pace_defaults = {
        "fast": (1.0, 2.0),
        "safe": (8.0, 15.0),
    }
    default_min, default_max = pace_defaults[args.pace]
    if args.min_interval is None:
        args.min_interval = default_min
    if args.max_interval is None:
        args.max_interval = default_max
    if args.max_interval < args.min_interval:
        args.min_interval, args.max_interval = args.max_interval, args.min_interval

    # Resolve preview mode
    do_preview = not args.no_preview
    if args.dry_run:
        do_preview = False

    # Load resume
    resume_path = Path(args.resume)
    if not resume_path.exists():
        log("ERROR", f"Resume not found: {resume_path}")
        sys.exit(1)
    resume_text = resume_path.read_text(encoding="utf-8")
    log("INFO", f"Loaded resume: {resume_path} ({len(resume_text)} chars)")

    # Init DB + one-time browser cleanup
    init_db()
    cleanup_browser(args.cdp_port)

    results: List[Dict[str, Any]] = []

    # Pre-loaded JD data (optional)
    preloaded_jd = None
    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            preloaded_jd = json.load(f)
        log("INFO", f"Pre-loaded JD from: {args.jd_file}")

    try:
        if args.job_url or args.jd_file:
            # ── Single job mode ──
            job_url = args.job_url
            if not job_url and preloaded_jd:
                job_url = preloaded_jd.get("link", "")
            if not job_url:
                log("ERROR", "No job URL. Use --job-url or ensure JD file has 'link' field.")
                sys.exit(1)

            if args.dry_run:
                log("INFO", "=== DRY RUN MODE ===")
                jd_data = preloaded_jd
                if jd_data is None:
                    jd_data = scrape_jd(job_url, cdp_port=args.cdp_port)
                    if jd_data.get("status") != "ok":
                        log("ERROR", f"Scrape failed: {jd_data.get('error')}")
                        sys.exit(2)

                match_data = match_resume_jd(jd_data, resume_text)
                message = build_message(jd_data, match_data, resume_text, style=args.style)

                dry_result = {
                    "mode": "dry_run",
                    "jd": jd_data,
                    "match": match_data,
                    "message": message,
                    "messageLength": len(message),
                }
                print(json.dumps(dry_result, ensure_ascii=False, indent=2))
                return
            else:
                result = pipeline_one(
                    job_url=job_url,
                    resume_text=resume_text,
                    cdp_port=args.cdp_port,
                    preview=do_preview,
                    style=args.style,
                    jd_data=preloaded_jd,
                    screenshot_dir=args.screenshot_dir,
                )
                results.append(result)

        elif args.jobs_file:
            # ── Batch mode ──
            jobs = load_jobs(args.jobs_file)
            total = min(len(jobs), args.max_send)
            log("INFO", f"Batch mode: {len(jobs)} jobs loaded, processing up to {total}")

            sent_count = 0
            for i, job in enumerate(jobs):
                if sent_count >= args.max_send:
                    log("INFO", f"Reached max-send limit ({args.max_send}). Stopping.")
                    break

                job_url = job.get("link")
                if not job_url:
                    log("WARN", f"Skipping job without URL: {job.get('title')}")
                    continue

                log("INFO", f"\n{'='*60}")
                log("INFO", f"[{i+1}/{total}] Processing: {job.get('title', '?')} @ {job.get('company', '?')}")

                result = pipeline_one(
                    job_url=job_url,
                    resume_text=resume_text,
                    cdp_port=args.cdp_port,
                    preview=do_preview,
                    style=args.style,
                    screenshot_dir=args.screenshot_dir,
                    index=i,
                    total=total,
                )
                results.append(result)

                if result.get("status") == "ok":
                    sent_count += 1
                elif result.get("status") == "aborted":
                    log("INFO", "User quit. Stopping batch.")
                    break
                elif result.get("status") == "failed" and "verification" in str(result.get("error", "")):
                    log("ERROR", "Verification encountered. Stopping batch.")
                    break

                # Rate limiting between jobs
                if i < total - 1:
                    interval = random.uniform(args.min_interval, args.max_interval)
                    log("INFO", f"Waiting {interval:.1f}s before next job...")
                    time.sleep(interval)

        else:
            ap.print_help()
            print("\nError: Provide --job-url, --jd-file, or --jobs-file.", file=sys.stderr)
            sys.exit(1)

    except KeyboardInterrupt:
        log("INFO", "\nInterrupted by user.")

    # ── Summary ──
    summary = {
        "totalProcessed": len(results),
        "sent": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "aborted": sum(1 for r in results if r.get("status") == "aborted"),
        "results": results,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    log("INFO", f"Done. Sent={summary['sent']}, Skipped={summary['skipped']}, Failed={summary['failed']}")

    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
