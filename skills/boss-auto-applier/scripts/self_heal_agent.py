#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-heal agent for sender failures (script-only, no LLM).

Input failure context -> choose repair actions -> execute -> return JSON.
This script is intentionally deterministic so orchestrators can call it safely.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

WORKSPACE = Path(__file__).resolve().parents[3]
START_CHROME = WORKSPACE / "skills/jd-greeting-generator/scripts/start_boss_chrome.sh"
AB_BOSS = WORKSPACE / "skills/jd-greeting-generator/scripts/ab_boss.sh"


def sh(cmd: List[str], *, env: Dict[str, str], timeout: int = 90) -> Tuple[bool, str, int]:
    cp = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        env=env,
    )
    out = (cp.stdout or "").strip()
    return cp.returncode == 0, out[-2000:], cp.returncode


def classify_failure(status: str, error_text: str, stderr_summary: str) -> str:
    text = f"{status}\n{error_text}\n{stderr_summary}".lower()
    if "verify_blocked" in text or "verification" in text or "captcha" in text:
        return "verification_blocked"
    if "cdp endpoint is not reachable" in text or "no page found" in text:
        return "cdp_unreachable"
    if "err_aborted" in text or "open_failed" in text or "navigation_mismatch" in text:
        return "navigation_transient"
    if "url_mismatch" in text or "preflight_failed" in text:
        return "url_drift"
    if "chat_navigation_failed" in text or "click_chat_failed" in text or "target_context_mismatch" in text:
        return "chat_context_drift"
    return "unknown"


def mk_env(cdp_port: int, force_relaunch: bool = False) -> Dict[str, str]:
    env = dict(os.environ)
    env["AGENT_BROWSER_CDP_PORT"] = str(cdp_port)
    if force_relaunch:
        env["AGENT_BROWSER_FORCE_RELAUNCH"] = "1"
    return env


def act_ensure_chrome(cdp_port: int) -> Tuple[bool, str]:
    ok, out, rc = sh(["bash", str(START_CHROME)], env=mk_env(cdp_port), timeout=120)
    return ok, f"ensure_chrome rc={rc}: {out}"


def act_force_relaunch(cdp_port: int) -> Tuple[bool, str]:
    ok, out, rc = sh(["bash", str(START_CHROME)], env=mk_env(cdp_port, force_relaunch=True), timeout=120)
    return ok, f"force_relaunch rc={rc}: {out}"


def act_open_job(job_url: str, cdp_port: int) -> Tuple[bool, str]:
    ok, out, rc = sh(["bash", str(AB_BOSS), "open", job_url], env=mk_env(cdp_port), timeout=90)
    return ok, f"open_job rc={rc}: {out}"


def action(name: str, fn) -> Dict[str, Any]:
    ok, detail = fn()
    return {"name": name, "ok": ok, "detail": detail}


def run_repair(job_url: str, cdp_port: int, failure_kind: str) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    retry_recommended = False

    if failure_kind == "verification_blocked":
        return {
            "applied": False,
            "failureKind": failure_kind,
            "retryRecommended": False,
            "actions": [],
            "note": "manual_verification_required",
        }

    if failure_kind == "cdp_unreachable":
        actions.append(action("force_relaunch_chrome", lambda: act_force_relaunch(cdp_port)))
        actions.append(action("open_target_job", lambda: act_open_job(job_url, cdp_port)))
        retry_recommended = any(a["ok"] for a in actions)
    elif failure_kind in {"navigation_transient", "url_drift", "chat_context_drift"}:
        actions.append(action("ensure_chrome_ready", lambda: act_ensure_chrome(cdp_port)))
        actions.append(action("open_target_job", lambda: act_open_job(job_url, cdp_port)))
        if not all(a["ok"] for a in actions):
            actions.append(action("force_relaunch_chrome", lambda: act_force_relaunch(cdp_port)))
            actions.append(action("open_target_job_after_relaunch", lambda: act_open_job(job_url, cdp_port)))
        retry_recommended = any(a["ok"] for a in actions)
    else:
        actions.append(action("ensure_chrome_ready", lambda: act_ensure_chrome(cdp_port)))
        retry_recommended = any(a["ok"] for a in actions)

    return {
        "applied": bool(actions),
        "failureKind": failure_kind,
        "retryRecommended": retry_recommended,
        "actions": actions,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Self-heal agent for send failures")
    ap.add_argument("--job-url", required=True)
    ap.add_argument("--status", default="failed")
    ap.add_argument("--error-text", default="")
    ap.add_argument("--stderr-summary", default="")
    ap.add_argument("--cdp-port", type=int, default=18801)
    args = ap.parse_args()

    failure_kind = classify_failure(args.status, args.error_text, args.stderr_summary)
    result = run_repair(args.job_url, args.cdp_port, failure_kind)
    result["jobUrl"] = args.job_url
    result["status"] = args.status
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

