#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 3: 批量发送招呼 + 对账。

读取 eval_summary.json 中的 fitJobs，逐个调用 sender_worker.py 发送，
最终调用 reconcile_receipt.py 对账，产出 receipt.json。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# ── SIGTERM graceful handler ──
_sigterm_received = False

def _handle_sigterm(signum, frame):
    global _sigterm_received
    _sigterm_received = True
    print(f"[WARN] SIGTERM received, finishing current job then exiting...", flush=True)

signal.signal(signal.SIGTERM, _handle_sigterm)


# ── Heartbeat thread (keeps no-output timer alive) ──
def _heartbeat_printer(stop_event: threading.Event, job_label: str, interval: float = 15.0):
    """Print heartbeat to stdout every `interval` seconds until stop_event is set."""
    tick = 0
    while not stop_event.wait(interval):
        tick += 1
        print(f"[HEARTBEAT] {job_label} running... ({tick * interval:.0f}s)", flush=True)

WORKSPACE = Path(__file__).resolve().parents[3]
SCRIPTS = {
    "sender_worker": WORKSPACE / "skills/boss-auto-applier/scripts/sender_worker.py",
    "reconcile": WORKSPACE / "skills/boss-auto-applier/scripts/reconcile_receipt.py",
    "self_heal_agent": WORKSPACE / "skills/boss-auto-applier/scripts/self_heal_agent.py",
}


def sh(cmd: List[str], *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def extract_job_id(url: str) -> str:
    m = re.search(r"/job_detail/([a-zA-Z0-9_\-]+)\.html", url or "")
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "_", url or "").strip("_")[:40] or "unknown"


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
    raise ValueError("failed to parse json from mixed output")


def bump(d: Dict[str, int], k: str, n: int = 1) -> None:
    d[k] = int(d.get(k, 0)) + n


def normalize_job_id(job: Dict[str, Any]) -> str:
    job_id = str(job.get("jobId") or "").strip()
    if job_id:
        return job_id
    return extract_job_id(str(job.get("jobUrl") or ""))


def load_attempted_job_ids(run_dir: Path) -> Set[str]:
    attempted: Set[str] = set()

    state_path = run_dir / "state.json"
    if state_path.exists():
        try:
            state = load_json(state_path)
            jobs = state.get("jobs") if isinstance(state, dict) else []
            if isinstance(jobs, list):
                for row in jobs:
                    if not isinstance(row, dict):
                        continue
                    job_id = str(row.get("jobId") or "").strip()
                    if job_id:
                        attempted.add(job_id)
        except Exception:
            pass

    send_dir = run_dir / "send"
    if send_dir.exists():
        for send_path in send_dir.glob("*.json"):
            try:
                payload = load_json(send_path)
            except Exception:
                payload = {}
            job_id = send_path.stem
            if isinstance(payload, dict):
                send_result = payload.get("sendResult")
                if isinstance(send_result, dict):
                    result_url = str(send_result.get("jobUrl") or "").strip()
                    if result_url:
                        parsed = extract_job_id(result_url)
                        if parsed:
                            job_id = parsed
            if job_id:
                attempted.add(job_id)

    return attempted


def resolve_eval_path(run_dir: Path, job: Dict[str, Any], job_id: str) -> Path:
    fallback = run_dir / "eval" / f"{job_id}.json"
    raw_eval = str(job.get("evalPath") or "").strip()
    if not raw_eval:
        return fallback

    candidate = Path(raw_eval)
    if not candidate.is_absolute():
        candidate = run_dir / candidate

    try:
        run_root = run_dir.resolve()
        candidate_root = candidate.resolve()
        candidate_root.relative_to(run_root)
        return candidate
    except Exception:
        return fallback


def load_eval_payload(eval_path: Path) -> Dict[str, Any]:
    if eval_path.exists():
        try:
            data = load_json(eval_path)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def resolve_message_draft(job: Dict[str, Any], eval_payload: Dict[str, Any]) -> str:
    for key in ("message", "messageDraft"):
        text = eval_payload.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    for key in ("message", "messageDraft"):
        text = job.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def sync_eval_payload(
    *,
    eval_path: Path,
    eval_payload: Dict[str, Any],
    job: Dict[str, Any],
    job_id: str,
    job_url: str,
    title: str,
    company: str,
    salary: str,
    message_draft: str,
) -> Dict[str, Any]:
    payload = dict(eval_payload)
    changed = False

    ensure_fields = {
        "jobUrl": job_url,
        "jobId": job_id,
        "jobTitle": title,
        "company": company,
        "salary": salary,
    }
    for key, value in ensure_fields.items():
        if value and (str(payload.get(key) or "").strip() != str(value).strip()):
            payload[key] = value
            changed = True

    match_score = job.get("matchScore")
    if match_score and str(payload.get("matchScore") or "").strip() != str(match_score).strip():
        payload["matchScore"] = match_score
        changed = True

    reasoning = job.get("reasoning")
    if reasoning and str(payload.get("reasoning") or "").strip() != str(reasoning).strip():
        payload["reasoning"] = reasoning
        changed = True

    if message_draft:
        if str(payload.get("messageDraft") or "").strip() != message_draft:
            payload["messageDraft"] = message_draft
            changed = True
        if str(payload.get("message") or "").strip() != message_draft:
            payload["message"] = message_draft
            changed = True

    if changed or not eval_path.exists():
        dump_json(eval_path, payload)
    return payload


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
        sys.executable, str(SCRIPTS["sender_worker"]),
        "--job-url", job_url,
        "--eval-json", str(eval_json),
        "--send-out", str(send_out),
        "--cdp-port", str(cdp_port),
        "--screenshot-dir", str(screenshot_dir),
    ]
    if allow_intent_failed_retry:
        cmd.append("--allow-intent-failed-retry")
    if no_retry:
        cmd.append("--no-retry")
    if capture_screenshot:
        cmd.append("--capture-screenshot")
    # Start heartbeat thread to keep no-output timer alive
    stop_hb = threading.Event()
    hb_label = f"sender_worker({Path(job_url).stem[:20]})"
    hb_thread = threading.Thread(target=_heartbeat_printer, args=(stop_hb, hb_label), daemon=True)
    hb_thread.start()
    try:
        cp = sh(cmd)
    finally:
        stop_hb.set()
        hb_thread.join(timeout=2)
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
            "sendResult": {"status": "failed", "error": "sender_invalid_json", "jobUrl": job_url},
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


def run_reconcile_and_assert(run_dir: Path) -> Path:
    state_path = run_dir / "state.json"
    receipt_path = run_dir / "receipt.json"
    missing = [str(p) for p in (state_path, receipt_path) if not p.exists()]
    if missing:
        raise RuntimeError(f"hard_fail_missing_state_or_receipt: {', '.join(missing)}")

    cp = sh([sys.executable, str(SCRIPTS["reconcile"]), str(run_dir), "--write-back"])
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
    ap = argparse.ArgumentParser(description="Stage 3: Batch send greetings + reconcile")
    ap.add_argument("--eval-summary", required=True, help="Path to eval_summary.json from Stage 2")
    ap.add_argument("--run-dir", required=True, help="Run directory (shared with Stage 1/2)")
    ap.add_argument("--cdp-port", type=int, default=18801, help="Chrome CDP port")
    ap.add_argument("--max-send", type=int, default=10, help="Max jobs to send")
    ap.add_argument("--min-interval", type=float, default=3.0, help="Min delay between sends (seconds)")
    ap.add_argument("--max-interval", type=float, default=6.0, help="Max delay between sends (seconds)")
    ap.add_argument("--retry-on-fail", action="store_true", help="Enable one self-heal retry on send failure")
    ap.add_argument("--capture-screenshot", action="store_true", help="Capture screenshots during send")
    ap.add_argument("--dry-run", action="store_true", help="Skip actual sending")
    ap.add_argument("--log-file", default="", help="Optional JSONL log path (default: <run-dir>/logs/stage3_send.jsonl)")
    args = ap.parse_args()
    if args.max_interval < args.min_interval:
        args.min_interval, args.max_interval = args.max_interval, args.min_interval

    run_dir = Path(args.run_dir)
    send_dir = run_dir / "send"
    send_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir = send_dir / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    repair_dir = run_dir / "repair"
    repair_dir.mkdir(exist_ok=True)
    log_path = init_log_path(run_dir, args.log_file, "stage3_send.jsonl")
    append_run_log(
        log_path,
        "info",
        "stage3_start",
        evalSummary=str(args.eval_summary),
        runDir=str(run_dir),
        cdpPort=args.cdp_port,
        maxSend=args.max_send,
        dryRun=args.dry_run,
        retryOnFail=args.retry_on_fail,
    )

    eval_summary = load_json(Path(args.eval_summary))
    fit_jobs = eval_summary.get("fitJobs") or []

    if not fit_jobs:
        print("[WARN] No fit jobs to send", flush=True)
        append_run_log(log_path, "warn", "no_fit_jobs")

    stats = {"sent": 0, "failed": 0, "skipped": 0, "alreadyContacted": 0}
    skip_reasons: Dict[str, int] = {}
    fail_reasons: Dict[str, int] = {}
    applied_jobs: List[Dict[str, Any]] = []
    all_jobs: List[Dict[str, Any]] = []

    attempted_job_ids = load_attempted_job_ids(run_dir)
    pending_jobs = []
    for job in fit_jobs:
        if not isinstance(job, dict):
            continue
        if normalize_job_id(job) in attempted_job_ids:
            continue
        pending_jobs.append(job)

    if attempted_job_ids:
        print(
            f"[INFO] Found {len(attempted_job_ids)} previously-attempted jobs; selecting pending jobs only",
            flush=True,
        )
        append_run_log(log_path, "info", "skip_attempted_jobs", attemptedCount=len(attempted_job_ids))

    jobs_to_send = pending_jobs[:args.max_send]
    target_send = min(args.max_send, len(pending_jobs))
    print(
        f"[INFO] Sending to {len(jobs_to_send)} fit jobs (max_send={args.max_send}, pending={len(pending_jobs)}, dry_run={args.dry_run})",
        flush=True,
    )
    append_run_log(
        log_path,
        "info",
        "send_plan",
        fitJobs=len(fit_jobs),
        pendingJobs=len(pending_jobs),
        selectedJobs=len(jobs_to_send),
        targetSend=target_send,
    )

    for job in jobs_to_send:
        job_url = str(job.get("jobUrl") or "").strip()
        job_id = normalize_job_id(job)
        eval_path = resolve_eval_path(run_dir, job, job_id)
        eval_payload = load_eval_payload(eval_path)
        title = job.get("title") or ""
        company = job.get("company") or ""
        salary = job.get("salary") or ""

        message_draft = resolve_message_draft(job, eval_payload)
        sync_eval_payload(
            eval_path=eval_path,
            eval_payload=eval_payload,
            job=job,
            job_id=job_id,
            job_url=job_url,
            title=title,
            company=company,
            salary=salary,
            message_draft=message_draft,
        )
        append_run_log(
            log_path,
            "info",
            "job_prepared",
            jobId=job_id,
            title=title,
            company=company,
            hasMessageDraft=bool(message_draft),
            evalPath=str(eval_path),
        )

        job_entry = {
            "title": title,
            "company": company,
            "salary": salary,
            "link": job_url,
            "jobId": job_id,
            "state": "pending",
            "sentAt": None,
            "error": None,
        }

        if args.dry_run:
            stats["skipped"] += 1
            bump(skip_reasons, "dry_run")
            job_entry["state"] = "dry_run_done"
            all_jobs.append(job_entry)
            print(f"[DRY] {title} @ {company}", flush=True)
            append_run_log(log_path, "info", "job_dry_run", jobId=job_id, title=title, company=company)
            continue

        send_path = send_dir / f"{job_id}.json"
        sender: Dict[str, Any] = {}
        send_result: Dict[str, Any] = {}
        status = "failed"
        stderr_summary = ""
        forced_result: Optional[Dict[str, Any]] = None
        if not message_draft:
            forced_result = {
                "sendResult": {
                    "status": "failed",
                    "error": "empty_message_draft",
                    "jobUrl": job_url,
                },
                "stderrSummary": "",
                "exitCode": 2,
            }
            dump_json(send_path, forced_result)
            append_run_log(log_path, "warn", "job_missing_message_draft", jobId=job_id, sendPath=str(send_path))

        max_send_attempts = 2 if args.retry_on_fail else 1
        for attempt in range(max_send_attempts):
            append_run_log(
                log_path,
                "info",
                "send_attempt_start",
                jobId=job_id,
                attempt=attempt + 1,
                maxAttempts=max_send_attempts,
            )
            if forced_result is not None:
                sender = forced_result
            else:
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
            append_run_log(
                log_path,
                "info",
                "send_attempt_result",
                jobId=job_id,
                attempt=attempt + 1,
                status=status,
                error=str(send_result.get("error") or ""),
            )
            if status in {"ok", "sent", "already_contacted", "skipped"}:
                break
            if forced_result is not None:
                break
            if attempt == 0 and args.retry_on_fail:
                err_text = str(send_result.get("error") or "")
                repair_path = repair_dir / f"{job_id}_attempt1.json"
                repair = run_self_heal_agent(
                    job_url=job_url,
                    status=status,
                    error_text=err_text,
                    stderr_summary=str(stderr_summary or ""),
                    cdp_port=args.cdp_port,
                    out_path=repair_path,
                )
                retry_by_repair = bool(repair.get("retryRecommended"))
                retry_by_transient = should_retry_send(status, err_text, str(stderr_summary or ""))
                if retry_by_repair or retry_by_transient:
                    print(f"[RETRY] self-heal + retry: {title} @ {company}", flush=True)
                    append_run_log(
                        log_path,
                        "warn",
                        "send_retry",
                        jobId=job_id,
                        retryByRepair=retry_by_repair,
                        retryByTransient=retry_by_transient,
                    )
                    time.sleep(1.5)
                    continue
            break

        if status in {"ok", "sent"}:
            stats["sent"] += 1
            job_entry["state"] = "sent"
            job_entry["sentAt"] = now_iso()
            applied_jobs.append({"title": title, "company": company, "salary": salary, "state": "sent"})
            print(f"[SENT] {title} @ {company}", flush=True)
            append_run_log(log_path, "info", "job_sent", jobId=job_id, title=title, company=company)
        elif status == "already_contacted":
            stats["alreadyContacted"] += 1
            stats["skipped"] += 1
            bump(skip_reasons, "already_contacted")
            job_entry["state"] = "already_contacted"
            print(f"[SKIP] {title} @ {company} (already_contacted)", flush=True)
            append_run_log(log_path, "info", "job_already_contacted", jobId=job_id, title=title, company=company)
        elif status == "skipped":
            reason = str(send_result.get("reason") or "skipped")
            stats["skipped"] += 1
            if reason == "already_contacted":
                stats["alreadyContacted"] += 1
                job_entry["state"] = "already_contacted"
            else:
                job_entry["state"] = "skipped"
            bump(skip_reasons, reason)
            print(f"[SKIP] {title} @ {company} ({reason})", flush=True)
            append_run_log(log_path, "info", "job_skipped", jobId=job_id, title=title, company=company, reason=reason)
        else:
            err = str(send_result.get("error") or "")
            fail_reason = classify_send_failure(status, err + "\n" + str(stderr_summary or ""))
            stats["failed"] += 1
            bump(fail_reasons, fail_reason)
            job_entry["state"] = "send_failed"
            job_entry["error"] = send_result.get("error") or (str(stderr_summary)[:1000] if stderr_summary else None)
            print(f"[FAIL] {title} @ {company} ({fail_reason})", flush=True)
            append_run_log(
                log_path,
                "error",
                "job_failed",
                jobId=job_id,
                title=title,
                company=company,
                failReason=fail_reason,
                error=job_entry["error"],
            )

            if fail_reason == "verification_timeout":
                all_jobs.append(job_entry)
                print("[STOP] Verification timeout, stopping", flush=True)
                append_run_log(log_path, "error", "stop_on_verification_timeout", jobId=job_id)
                break

        all_jobs.append(job_entry)

        # Check SIGTERM — graceful exit after current job
        if _sigterm_received:
            print(f"[STOP] SIGTERM received, stopping after {stats['sent']} sent / {stats['failed']} failed", flush=True)
            append_run_log(log_path, "warn", "stop_on_sigterm", sent=stats["sent"], failed=stats["failed"])
            break

        if job_entry["state"] == "sent":
            delay = round(random.uniform(args.min_interval, args.max_interval), 1)
            print(f"[WAIT] {delay}s", flush=True)
            time.sleep(delay)

    # ── Retry failed jobs (1 round, with cooldown) ──
    if not args.dry_run and not _sigterm_received:
        failed_indices = [
            i for i, j in enumerate(all_jobs)
            if j.get("state") == "send_failed"
        ]
        if failed_indices:
            cooldown = 8.0
            print(f"\n[RETRY] {len(failed_indices)} failed job(s) — cooling down {cooldown}s before retry...", flush=True)
            append_run_log(log_path, "info", "retry_round_start", failedCount=len(failed_indices), cooldownSec=cooldown)
            time.sleep(cooldown)

            for idx in failed_indices:
                if _sigterm_received:
                    print("[STOP] SIGTERM during retry round, aborting", flush=True)
                    break

                je = all_jobs[idx]
                job_id = je["jobId"]
                job_url = je["link"]
                title = je.get("title") or ""
                company = je.get("company") or ""

                eval_path = run_dir / "eval" / f"{job_id}.json"
                send_path = send_dir / f"{job_id}.json"

                print(f"[RETRY] {title} @ {company}", flush=True)
                append_run_log(log_path, "info", "retry_attempt", jobId=job_id, title=title, company=company)

                sender = run_sender_worker(
                    job_url,
                    eval_path,
                    send_path,
                    args.cdp_port,
                    screenshot_dir,
                    allow_intent_failed_retry=True,
                    no_retry=False,  # allow full retries on retry round
                    capture_screenshot=args.capture_screenshot,
                )
                send_result = sender.get("sendResult") if isinstance(sender, dict) else {}
                if not isinstance(send_result, dict):
                    send_result = {"status": "failed", "error": "invalid_sender_result"}
                retry_status = send_result.get("status") or "failed"

                if retry_status in {"ok", "sent"}:
                    stats["sent"] += 1
                    stats["failed"] -= 1
                    je["state"] = "sent"
                    je["sentAt"] = now_iso()
                    je["error"] = None
                    applied_jobs.append({"title": title, "company": company, "salary": je.get("salary", ""), "state": "sent"})
                    # Update fail_reasons
                    old_reason = classify_send_failure("failed", str(je.get("error") or ""))
                    if fail_reasons.get(old_reason, 0) > 0:
                        fail_reasons[old_reason] -= 1
                        if fail_reasons[old_reason] <= 0:
                            del fail_reasons[old_reason]
                    print(f"[RETRY-OK] {title} @ {company}", flush=True)
                    append_run_log(log_path, "info", "retry_success", jobId=job_id, title=title, company=company)
                elif retry_status == "already_contacted":
                    stats["failed"] -= 1
                    stats["skipped"] += 1
                    stats["alreadyContacted"] += 1
                    je["state"] = "already_contacted"
                    je["error"] = None
                    print(f"[RETRY-SKIP] {title} @ {company} (already_contacted)", flush=True)
                    append_run_log(log_path, "info", "retry_already_contacted", jobId=job_id)
                else:
                    print(f"[RETRY-FAIL] {title} @ {company} ({send_result.get('error', '')})", flush=True)
                    append_run_log(log_path, "warn", "retry_failed", jobId=job_id, error=str(send_result.get("error") or ""))

                # Brief delay between retries
                if idx != failed_indices[-1]:
                    time.sleep(random.uniform(3.0, 5.0))

    # Build state.json for reconciliation
    state = {
        "runId": run_dir.name,
        "stats": stats,
        "skipReasons": skip_reasons,
        "failReasons": fail_reasons,
        "jobs": all_jobs,
        "finishedAt": now_iso(),
    }
    state_path = run_dir / "state.json"
    dump_json(state_path, state)

    # Build receipt
    if args.dry_run:
        overall = "success" if stats["failed"] == 0 else "failed"
    else:
        processed = stats["sent"] + stats["skipped"]
        if stats["failed"] == 0 and processed >= target_send:
            overall = "success"
        elif processed > 0:
            overall = "partial"
        else:
            overall = "failed"
    receipt = {
        "type": "boss-send",
        "platform": "boss",
        "overallStatus": overall,
        "runDir": str(run_dir),
        "targetSend": target_send,
        "stats": stats,
        "skipReasons": skip_reasons,
        "failReasons": fail_reasons,
        "appliedJobs": applied_jobs,
        "receipt": str(run_dir / "receipt.json"),
        "state": str(state_path),
        "statusDb": str(WORKSPACE / "data/boss_greeting.db"),
        "finishedAt": now_iso(),
    }
    receipt_path = run_dir / "receipt.json"
    dump_json(receipt_path, receipt)
    append_run_log(
        log_path,
        "info",
        "stage3_summary",
        receipt=str(receipt_path),
        overallStatus=overall,
        targetSend=target_send,
        stats=stats,
        skipReasons=skip_reasons,
        failReasons=fail_reasons,
    )

    # Reconcile (skip for dry-run)
    if not args.dry_run and stats["sent"] > 0:
        try:
            run_reconcile_and_assert(run_dir)
            print("[OK] Reconciliation passed", flush=True)
        except Exception as exc:
            print(f"[WARN] Reconciliation: {exc}", flush=True)

    print(f"\n[OK] Send complete: sent={stats['sent']}, failed={stats['failed']}, skipped={stats['skipped']}", flush=True)
    print(f"[OK] Receipt: {receipt_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
