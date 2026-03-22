#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Scrape full JD (job description) from a BOSS直聘 job detail page via CDP.

This script uses the browser (via ab_boss.sh + agent-browser) to open
a BOSS直聘 job URL and extract the full job description including
work responsibilities and requirements.

Args:
  --job-url <url>         BOSS直聘 job detail page URL
  --cdp-port <port>       CDP port (default: 18801)

Output JSON (stdout):
  {
    "jobTitle": "高级Go开发",
    "company": "XX科技",
    "salary": "25-40K",
    "experience": "3-5年",
    "recruiter": "李女士",
    "recruiterTitle": "HRBP",
    "description": "工作内容：\\n1. ...\\n\\n任职要求：\\n1. ...",
    "tags": ["Go", "微服务"],
    "benefits": ["五险一金"],
    "link": "https://..."
  }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict
from urllib.error import URLError
from urllib.request import urlopen

WORKSPACE = str(Path(__file__).resolve().parents[3])
SKILL_DIR = f"{WORKSPACE}/skills/jd-greeting-generator"
AB_BOSS = f"{SKILL_DIR}/scripts/ab_boss.sh"
DB_PATH = os.environ.get("BOSS_GREETING_DB", f"{WORKSPACE}/data/boss_greeting.db")
SCHEMA_PATH = f"{SKILL_DIR}/scripts/schema.sql"
START_BOSS_CHROME = f"{SKILL_DIR}/scripts/start_boss_chrome.sh"

# Reuse browser helpers from send_greeting
sys.path.insert(0, f"{SKILL_DIR}/scripts")
from send_greeting import (
    ab_run,
    ab_eval,
    detect_verify,
    wait_for_verify_clear,
    jitter_sleep,
    log,
    prepare_env,
)

# ────────────────────────── JS: Extract full JD ──────────────────────────

EXTRACT_FULL_JD_JS = """(() => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const firstText = (selectors) => {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (!el) continue;
      const t = clean(el.textContent);
      if (t) return t;
    }
    return "";
  };
  const manyText = (selectors, limit = 30) => {
    const out = [];
    const seen = new Set();
    for (const s of selectors) {
      for (const el of document.querySelectorAll(s)) {
        const t = clean(el.textContent);
        if (!t || seen.has(t)) continue;
        seen.add(t);
        out.push(t);
        if (out.length >= limit) return out;
      }
    }
    return out;
  };

  // ── Basic info (updated for current BOSS DOM, 2026-03) ──
  const jobTitle = firstText([
    ".info-primary .name h1",
    ".job-banner .name h1",
    ".name h1",
    "h1",
  ]);
  // Company: sidebar has the real company name; fallback to page title parse
  let company = firstText([
    ".sider-company .company-info a",
    ".sider-company .company-info",
  ]);
  if (!company) {
    const titleMatch = document.title.match(/「.+?招聘」_(.+?)招聘-BOSS直聘/);
    if (titleMatch) company = titleMatch[1];
  }
  const salary = firstText([
    ".info-primary .salary",
    ".name .salary",
    ".salary",
  ]);
  // Location/Experience/Education are separate elements under .info-primary p
  const city = firstText([
    ".info-primary .text-city",
    ".text-city",
  ]);
  const experienceText = firstText([
    ".info-primary .text-experiece",
    ".text-experiece",
  ]);
  const educationText = firstText([
    ".info-primary .text-degree",
    ".text-degree",
  ]);
  const meta = [city, experienceText, educationText].filter(Boolean).join(" ");

  // Recruiter: name is the first text child of .job-boss-info
  let recruiter = "";
  const bossInfoEl = document.querySelector(".job-boss-info");
  if (bossInfoEl) {
    // Get direct text content (name), excluding child elements
    for (const node of bossInfoEl.childNodes) {
      if (node.nodeType === 3) {
        const t = node.textContent.trim();
        if (t) { recruiter = t; break; }
      }
    }
    if (!recruiter) recruiter = clean(bossInfoEl.textContent).split(/\\s/)[0] || "";
  }
  if (!recruiter) recruiter = firstText([".boss-info-attr .name", ".boss-card .name"]);

  const recruiterTitle = firstText([
    ".boss-info-attr",
  ]);
  // boss-info-attr contains "公司名·职位", extract just the role part
  const recruiterTitleClean = recruiterTitle ? recruiterTitle.replace(company, "").replace(/^[·\\.\\s]+|[·\\.\\s]+$/g, "") : "";

  // ── Tags & Benefits ──
  const tags = manyText([
    ".job-detail .job-tags span",
    ".job-detail .job-labels span",
    ".job-sec .tag-list li",
  ], 24);
  const benefits = manyText([
    ".job-detail .job-bene-tag span",
    ".job-detail .job-benefit-tag span",
    ".welfare-list li",
    ".job-sec .welfare-list span",
  ], 24);

  // ── JD Full Text (the critical addition) ──
  // BOSS直聘 job detail page has multiple .job-sec-text blocks
  // typically: 工作内容, 任职要求, etc.
  const descSelectors = [
    '.job-sec-text',
    '.job-detail-section .text',
    '.job-description .text',
    '.job-detail .text-main',
    '.detail-content .text',
    '.job-detail-body .job-sec .text',
  ];
  const descParts = [];
  const seenDesc = new Set();
  for (const sel of descSelectors) {
    for (const el of document.querySelectorAll(sel)) {
      // Use innerText to preserve line breaks
      const t = (el.innerText || el.textContent || "").trim();
      if (!t || seenDesc.has(t)) continue;
      seenDesc.add(t);
      descParts.push(t);
    }
  }
  const description = descParts.join("\\n\\n");

  // ── Experience / Education already extracted above ──
  const experience = experienceText || "";
  const education = educationText || "";

  const link = location.href || "";
  const pageTitle = document.title || "";
  const hasSecurityUrl = /security-check|captcha|verify/i.test(link);
  const hasTitleVerify = /安全验证|人机验证|验证码|Security Check/i.test(pageTitle);
  const hasSlider = !!document.querySelector('.geetest_slider, .nc_wrapper, .verify-wrap, [class*="captcha"], iframe[src*="captcha"], iframe[src*="verify"]');
  const needVerify = hasSlider || hasSecurityUrl || hasTitleVerify;

  return {
    jobTitle,
    company,
    salary,
    city,
    experience,
    education,
    meta,
    recruiter,
    recruiterTitle: recruiterTitleClean,
    description,
    tags,
    benefits,
    link,
    _needVerify: needVerify,
    _pageTitle: pageTitle,
  };
})()"""


# ────────────────────────── JD Cache ──────────────────────────

def cache_jd(result: Dict[str, Any]) -> None:
    """Write scraped JD data to jd_cache table for dashboard display."""
    job_url = result.get("link") or result.get("job_url", "")
    if not job_url or result.get("status") != "ok":
        return
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.execute(
            """INSERT OR REPLACE INTO jd_cache
               (job_url, job_title, company, salary, location, experience, education,
                description, tags, benefits, recruiter, recruiter_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_url,
                result.get("jobTitle"),
                result.get("company"),
                result.get("salary"),
                result.get("meta", ""),
                result.get("experience"),
                result.get("education"),
                result.get("description"),
                json.dumps(result.get("tags", []), ensure_ascii=False),
                json.dumps(result.get("benefits", []), ensure_ascii=False),
                result.get("recruiter"),
                result.get("recruiterTitle"),
            ),
        )
        conn.commit()
        conn.close()
        log("INFO", f"Cached JD to DB: {job_url}")
    except Exception as e:
        log("WARN", f"Failed to cache JD: {e}")


# ────────────────────────── Main ──────────────────────────

def extract_job_id(url: str) -> str:
    m = re.search(r"/job_detail/([a-zA-Z0-9_\-]+)\.html", url or "")
    return m.group(1) if m else ""


def normalize_url(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'")


def cdp_reachable(cdp_port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=3):
            return True
    except (URLError, OSError, TimeoutError):
        return False


def ensure_cdp_ready(cdp_port: int) -> bool:
    if cdp_reachable(cdp_port):
        return True

    for attempt in range(1, 3):
        env = os.environ.copy()
        env["AGENT_BROWSER_CDP_PORT"] = str(cdp_port)
        # Use force relaunch to recover from stale/half-dead Chrome states.
        env["AGENT_BROWSER_FORCE_RELAUNCH"] = "1"
        try:
            cp = subprocess.run(
                ["bash", START_BOSS_CHROME],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            if cp.stdout:
                last = cp.stdout.strip().splitlines()[-1]
                log("INFO", f"start_boss_chrome attempt {attempt}: {last}")
        except Exception as e:
            log("WARN", f"start_boss_chrome attempt {attempt} failed: {e}")
            time.sleep(1.0)
            continue

        for _ in range(10):
            if cdp_reachable(cdp_port):
                return True
            time.sleep(0.5)

        time.sleep(1.0)

    return False


def scrape_jd(job_url: str, cdp_port: int = 18801) -> Dict[str, Any]:
    """Open a BOSS直聘 job page and extract full JD info."""
    if not ensure_cdp_ready(cdp_port):
        return {
            "status": "failed",
            "error": f"cdp_not_reachable:{cdp_port}",
            "link": job_url,
        }

    env = prepare_env(cdp_port)
    target_job_id = extract_job_id(job_url)

    try:
        # 1. Open job detail page
        log("INFO", f"Opening: {job_url}")
        open_ok = False
        last_open_err: Exception | None = None
        for _open_attempt in range(2):
            try:
                ab_run(["open", job_url], env=env)
                open_ok = True
                break
            except Exception as e:
                last_open_err = e
                err_text = str(e)
                recoverable = (
                    "CDP endpoint is not reachable" in err_text
                    or "No page found" in err_text
                    or "Execution context was destroyed" in err_text
                    or "ERR_ABORTED" in err_text
                )
                if recoverable and ensure_cdp_ready(cdp_port):
                    env = prepare_env(cdp_port)
                    jitter_sleep(0.8, 1.6)
                    continue
                break
        if not open_ok:
            raise RuntimeError(f"open_failed: {last_open_err}")

        # 2. Extract full JD with integrated redirect/verify checks
        result: Dict[str, Any] | Any = None
        extract_last_err: Exception | None = None
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                result = ab_eval(EXTRACT_FULL_JD_JS, env=env, timeout=30)
                if not isinstance(result, dict):
                    raise RuntimeError(f"extraction_failed: got {type(result)}")

                result_link = normalize_url(result.get("link"))
                need_verify = bool(result.get("_needVerify"))
                on_target_detail = "job_detail" in result_link and (
                    not target_job_id or f"/job_detail/{target_job_id}.html" in result_link
                )

                if need_verify:
                    if attempt >= max_attempts:
                        raise RuntimeError("verification_timeout")
                    log("WARN", "Verification page detected during JD scrape; waiting for clear")
                    if not wait_for_verify_clear(env):
                        raise RuntimeError("verification_timeout")
                    jitter_sleep(0.6, 1.2)
                    continue

                if not on_target_detail:
                    if attempt >= max_attempts:
                        raise RuntimeError(f"redirect_to_homepage: {result_link}")
                    log(
                        "WARN",
                        f"Target mismatch/redirect → {result_link or '<empty>'}, "
                        f"reopen target ({attempt}/{max_attempts-1})",
                    )
                    jitter_sleep(1.2, 2.2)
                    ab_run(["open", job_url], env=env)
                    jitter_sleep(0.6, 1.3)
                    continue

                # 允许少数岗位标签为空，但正文不应为空
                desc_len = len((result.get("description") or "").strip())
                if desc_len == 0:
                    if attempt >= max_attempts:
                        raise RuntimeError("empty_description")
                    log("WARN", f"Extract attempt {attempt}/{max_attempts} got empty description; retry")
                    jitter_sleep(0.7, 1.4)
                    continue
                break
            except Exception as e:
                extract_last_err = e
                if attempt >= max_attempts:
                    raise
                log("WARN", f"Extract attempt {attempt}/{max_attempts} failed: {e}; reopen target and retry")
                ab_run(["open", job_url], env=env)
                jitter_sleep(0.9, 1.8)
                # Retry early if verify page appears mid-navigation
                if detect_verify(env):
                    if not wait_for_verify_clear(env):
                        raise RuntimeError("verification_timeout")

        if not isinstance(result, dict):
            raise RuntimeError(f"extraction_failed_final: {extract_last_err or type(result)}")

        # Ensure link is set
        if not result.get("link"):
            result["link"] = job_url
        result.pop("_needVerify", None)
        result.pop("_pageTitle", None)

        log("INFO", f"Scraped: {result.get('jobTitle')} @ {result.get('company')}")
        desc_len = len(result.get("description", ""))
        log("INFO", f"Description length: {desc_len} chars")

        if desc_len == 0:
            log("WARN", "Description is empty! DOM selectors may need updating.")

        full_result = {"status": "ok", **result}
        cache_jd(full_result)
        return full_result

    except Exception as e:
        log("ERROR", f"Scrape failed: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "link": job_url,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape full JD from BOSS直聘")
    ap.add_argument("--job-url", required=True, help="BOSS直聘 job detail URL")
    ap.add_argument("--cdp-port", type=int, default=18801, help="CDP port (default: 18801)")
    args = ap.parse_args()

    result = scrape_jd(job_url=args.job_url, cdp_port=args.cdp_port)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("status") == "ok" else 2)


if __name__ == "__main__":
    main()
