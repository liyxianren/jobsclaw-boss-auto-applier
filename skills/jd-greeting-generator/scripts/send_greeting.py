#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Send a greeting message on BOSS直聘 for a given job URL.

This script HAS external side effects (sends a greeting on BOSS直聘).

Args:
  --job-url <url>         BOSS直聘 job detail page URL
  --message <text>        Greeting message to send
  --session <name>        agent-browser session name (default: boss)
  --headed                Run in headed mode (for login/debug)
  --screenshot-dir <dir>  Directory to save verification screenshots

Output JSON:
  {status, jobUrl, jobTitle, company, recruiter, screenshotPath, error?}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

WORKSPACE = str(Path(__file__).resolve().parents[3])
SKILL_DIR = f"{WORKSPACE}/skills/jd-greeting-generator"
AB_BOSS = f"{SKILL_DIR}/scripts/ab_boss.sh"
DB_PATH = os.environ.get("BOSS_GREETING_DB", f"{WORKSPACE}/data/boss_greeting.db")
SCHEMA_PATH = f"{SKILL_DIR}/scripts/schema.sql"
START_BOSS_CHROME = f"{SKILL_DIR}/scripts/start_boss_chrome.sh"

# ────────────────────────── Logging ──────────────────────────

def log(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


# ────────────────────────── Database ──────────────────────────

def init_db() -> None:
    """Ensure DB and schema exist, run migrations for new columns/indexes."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        schema_sql = Path(SCHEMA_PATH).read_text(encoding="utf-8")
        try:
            conn.executescript(schema_sql)
            conn.commit()
        except sqlite3.OperationalError as e:
            # Existing DB may miss newer columns referenced by new indexes.
            # Continue with explicit migrations below.
            log("WARN", f"Schema bootstrap warning: {e}")

        # Backward-compatible migrations
        for col in (
            "salary TEXT",
            "location TEXT",
            "semantic_fingerprint TEXT",
            "intent_id TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE greetings ADD COLUMN {col}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_greetings_status_url ON greetings(status, job_url)",
            "CREATE INDEX IF NOT EXISTS idx_greetings_semantic_status ON greetings(status, semantic_fingerprint)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_greetings_intent_id ON greetings(intent_id)",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                log("WARN", f"Index migration warning: {e}")
        conn.commit()

        # Backfill semantic_fingerprint for existing records that lack it
        try:
            rows = conn.execute(
                "SELECT id, company, job_title, recruiter FROM greetings WHERE semantic_fingerprint IS NULL"
            ).fetchall()
            backfilled = 0
            for row_id, co, title, rec in rows:
                fp = semantic_fingerprint(co, title, rec)
                if fp:
                    conn.execute("UPDATE greetings SET semantic_fingerprint=? WHERE id=?", (fp, row_id))
                    backfilled += 1
            if backfilled:
                conn.commit()
                log("INFO", f"Backfilled semantic_fingerprint for {backfilled}/{len(rows)} existing records")
        except Exception as e:
            log("WARN", f"Backfill migration warning: {e}")
    finally:
        conn.close()


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def normalize_title(title: Optional[str]) -> str:
    """Normalize job title for semantic dedupe.

    - NFKC (全半角统一)
    - Remove trailing bracket suffix, e.g. "产品经理（AI方向）" -> "产品经理"
    - Remove all spaces
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    s = re.sub(r"[（(][^）)]*[）)]\s*$", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def semantic_fingerprint(company: Optional[str], title: Optional[str], recruiter: Optional[str]) -> Optional[str]:
    c = _normalize_text(company)
    t = normalize_title(title)
    r = _normalize_text(recruiter)
    if not (c or t or r):
        return None
    raw = f"{c}|{t}|{r}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def resolve_run_id(explicit_run_id: Optional[str] = None) -> str:
    if explicit_run_id:
        return str(explicit_run_id)
    return (
        os.environ.get("BOSS_APPLY_RUN_ID")
        or os.environ.get("OPENCLAW_RUN_ID")
        or "standalone"
    )


def build_intent_id(job_url: str, run_id: str, message: str) -> str:
    prefix = re.sub(r"\s+", " ", (message or "").strip())[:32]
    raw = f"{job_url}|{run_id}|{prefix}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def query_existing_delivery_reason(job_url: str, semantic_fp: Optional[str]) -> Optional[str]:
    """Return already-sent reason by url/fingerprint if found."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT id FROM greetings WHERE job_url=? AND status IN ('sent', 'already_contacted') LIMIT 1",
            (job_url,),
        ).fetchone()
        if row:
            return "already_sent_by_url"

        if semantic_fp:
            row = conn.execute(
                """SELECT id FROM greetings
                   WHERE semantic_fingerprint=?
                     AND status IN ('sent', 'already_contacted')
                   LIMIT 1""",
                (semantic_fp,),
            ).fetchone()
            if row:
                return "already_sent_by_fingerprint"
        return None
    finally:
        conn.close()


def intent_terminal_status(intent_id: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT status FROM greetings WHERE intent_id=? AND status IN ('sent', 'failed') LIMIT 1",
            (intent_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def record_greeting(
    job_url: str,
    job_title: Optional[str],
    company: Optional[str],
    recruiter: Optional[str],
    message: str,
    status: str,
    salary: Optional[str] = None,
    location: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    error: Optional[str] = None,
    semantic_fp: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> None:
    """Insert or update greeting record."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO greetings (
                   job_url, job_title, company, salary, location, recruiter,
                   message, status, sent_at, screenshot_path, error,
                   semantic_fingerprint, intent_id
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_url) DO UPDATE SET
                 message=excluded.message,
                 status=excluded.status,
                 sent_at=excluded.sent_at,
                 screenshot_path=excluded.screenshot_path,
                 error=excluded.error,
                 salary=COALESCE(excluded.salary, salary),
                 location=COALESCE(excluded.location, location),
                 company=COALESCE(excluded.company, company),
                 recruiter=COALESCE(excluded.recruiter, recruiter),
                 semantic_fingerprint=COALESCE(excluded.semantic_fingerprint, semantic_fingerprint),
                 intent_id=COALESCE(excluded.intent_id, intent_id)""",
            (
                job_url,
                job_title,
                company,
                salary,
                location,
                recruiter,
                message,
                status,
                time.strftime("%Y-%m-%d %H:%M:%S") if status == "sent" else None,
                screenshot_path,
                error,
                semantic_fp,
                intent_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_jd_cache_eval(
    job_url: str,
    match_score: Optional[str] = None,
    fit: Optional[bool] = None,
    reasoning: Optional[str] = None,
) -> None:
    """Update jd_cache with evaluation results (match_score, fit, reasoning)."""
    if not match_score and reasoning is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """UPDATE jd_cache SET match_score=?, fit=?, reasoning=? WHERE job_url=?""",
            (match_score, 1 if fit else 0, reasoning, job_url),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log("WARN", f"Failed to update jd_cache eval: {e}")


# ────────────────────────── Browser helpers ──────────────────────────

def prepare_env(port: int) -> Dict[str, str]:
    """Prepare environment with CDP port for ab_boss.sh."""
    env = os.environ.copy()
    env["AGENT_BROWSER_CDP_PORT"] = str(port)
    return env


def kill_stale_daemons() -> None:
    """Kill any lingering agent-browser daemon processes from previous runs.
    The daemon continuously polls/refreshes pages, triggering BOSS risk control
    if left alive across pipeline runs."""
    try:
        subprocess.run(
            ["pkill", "-f", "agent-browser.*daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        log("INFO", "Killed stale agent-browser daemons")
        time.sleep(0.5)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass


def close_extra_tabs(port: int) -> None:
    """Close all tabs except one to prevent accumulation across runs."""
    import urllib.request
    list_url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(list_url, timeout=5) as resp:
            tabs = json.loads(resp.read().decode())
        pages = [t for t in tabs if t.get("type") == "page"]
        if len(pages) <= 1:
            return
        # Close all except the first
        for page in pages[1:]:
            tab_id = page.get("id", "")
            close_url = f"http://127.0.0.1:{port}/json/close/{tab_id}"
            try:
                urllib.request.urlopen(close_url, timeout=3)
                log("INFO", f"Closed extra tab: {page.get('url', '')[:60]}")
            except Exception:
                pass
        time.sleep(0.5)
    except Exception as e:
        log("WARN", f"close_extra_tabs failed: {e}")


def cdp_reachable(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3):
            return True
    except (URLError, OSError, TimeoutError):
        return False


def ensure_cdp_ready(port: int) -> bool:
    if cdp_reachable(port):
        return True

    for attempt in range(1, 3):
        env = os.environ.copy()
        env["AGENT_BROWSER_CDP_PORT"] = str(port)
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

        for _ in range(12):
            if cdp_reachable(port):
                return True
            time.sleep(0.5)

    return False


def ab_run(
    cmd: List[str],
    env: Dict[str, str],
    timeout: int = 120,
) -> str:
    """Run ab_boss.sh command and return stdout."""
    full_cmd = ["bash", AB_BOSS] + cmd
    p = subprocess.run(
        full_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(
            f"ab_boss failed ({p.returncode}): {' '.join(cmd)}\n"
            f"STDERR: {(p.stderr or '')[:500]}"
        )
    return (p.stdout or "").strip()


def parse_json_from_mixed_output(raw: str) -> Any:
    """Parse JSON from agent-browser mixed output.

    agent-browser often prepends human-readable lines before the JSON payload:
      ✓ <title>
        <url>
      { ...json... }
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty output")

    # Fast path: pure JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"[\[{]", s)]

    # Parse first decodable JSON object/array from mixed output
    for idx in starts:
        try:
            obj, _ = decoder.raw_decode(s[idx:])
            return obj
        except Exception:
            continue

    # Fallback: sometimes last line is a standalone JSON literal (e.g. "...")
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise ValueError("no JSON payload found in output")


def ab_eval(js: str, env: Dict[str, str], timeout: int = 120) -> Any:
    """Run JavaScript via ab_boss.sh eval and return parsed result.

    BOSS pages may navigate around chat transitions, so eval can fail transiently.
    Retry a few times before surfacing the error.
    """
    raw = ""
    last_err: Optional[Exception] = None
    for i in range(3):
        try:
            raw = ab_run(["eval", js], env=env, timeout=timeout)
            break
        except Exception as e:
            last_err = e
            if i < 2:
                time.sleep(1.2 + i * 0.8)
                continue
            raise

    if not raw and last_err:
        raise last_err

    # agent-browser eval may prepend status lines before JSON payload.
    try:
        return parse_json_from_mixed_output(raw)
    except Exception:
        return raw


def jitter_sleep(lo: float = 2.0, hi: float = 5.0) -> None:
    """Random sleep to avoid detection."""
    t = random.uniform(lo, hi)
    time.sleep(t)


# ────────────────────────── Verify detection ──────────────────────────

DETECT_VERIFY_JS = """(() => {
  const title = document.title || '';
  const url = location.href || '';
  const hasSecurityUrl = /security-check|captcha|verify/i.test(url);
  const hasTitle = /安全验证|人机验证|验证码|Security Check/i.test(title);
  const hasSlider = !!document.querySelector('.geetest_slider, .nc_wrapper, .verify-wrap, [class*="captcha"], iframe[src*="captcha"], iframe[src*="verify"]');
  return { needVerify: hasSlider || hasSecurityUrl || hasTitle, url, title };
})()"""


def detect_verify(env: Dict[str, str]) -> bool:
    """Check if BOSS is showing a verification/captcha page."""
    try:
        result = ab_eval(DETECT_VERIFY_JS, env=env, timeout=30)
        if isinstance(result, dict):
            return bool(result.get("needVerify"))
    except Exception:
        pass
    return False


def wait_for_verify_clear(env: Dict[str, str], timeout_sec: int = 300) -> bool:
    """Wait for user to manually clear verification. Returns True if cleared."""
    log("WARN", f"Verification detected! Please solve it manually within {timeout_sec}s...")
    start = time.time()
    while time.time() - start < timeout_sec:
        time.sleep(2)
        if not detect_verify(env):
            log("INFO", "Verification cleared.")
            return True
    log("ERROR", "Verification timeout.")
    return False


# ────────────────────────── Session Target Validation ──────────────────────────

EXTRACT_CHAT_CONTEXT_JS = """(() => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const url = location.href || '';

  let title = null;
  let company = null;
  let recruiter = null;

  // ── Primary: BOSS v2 chat header (.top-info-content .user-info-wrap) ──
  const baseInfo = document.querySelector('.top-info-content .base-info, .user-info-wrap .base-info');
  if (baseInfo) {
    // Recruiter name lives in .name-text
    const nameEl = baseInfo.querySelector('.name-text');
    if (nameEl) recruiter = clean(nameEl.textContent);

    // Company is a bare <span> sibling of .name-content (no special class).
    // .base-title holds recruiter title (e.g. "人力资源总监"), skip it.
    const spans = baseInfo.querySelectorAll(':scope > span');
    for (const sp of spans) {
      if (sp.classList.contains('base-title')) continue;
      const t = clean(sp.textContent);
      if (t && t !== recruiter) { company = t; break; }
    }
  }

  // ── Fallback: legacy selectors (older BOSS DOM) ──
  if (!recruiter) {
    for (const sel of ['.chat-person-card .name', '.chat-recruit .name', '.chat-header .name', '.person-info .name']) {
      const el = document.querySelector(sel);
      if (el) { recruiter = clean(el.textContent); if (recruiter) break; }
    }
  }
  if (!company) {
    for (const sel of ['.chat-person-card .company', '.chat-recruit .company', '.chat-header .company']) {
      const el = document.querySelector(sel);
      if (el) { company = clean(el.textContent); if (company) break; }
    }
  }

  // ── Job title: try position-list area (right panel on chat page) ──
  const posEl = document.querySelector('.position-list .position-name, .chat-job .job-title, .job-info .job-name');
  if (posEl) title = clean(posEl.textContent);

  // ── Fallback: extract from <title> ──
  if (!title && /chat/i.test(url)) {
    const pageTitle = document.title || '';
    const m = pageTitle.match(/与(.+?)的沟通/);
    if (m) title = m[1];
  }

  // ── Extract from active sidebar item (most reliable for company/recruiter) ──
  if (!company || !recruiter) {
    const activeItem = document.querySelector('.user-list li[role=listitem] .friend-content.selected');
    if (activeItem) {
      if (!recruiter) {
        const n = activeItem.querySelector('.name-text');
        if (n) recruiter = clean(n.textContent);
      }
      if (!company) {
        const spans = activeItem.querySelectorAll('.name-box span');
        if (spans.length > 1) company = clean(spans[1].textContent);
      }
    }
  }

  return { title, company, recruiter, url };
})()"""


def extract_chat_context(env: Dict[str, str]) -> Dict[str, str]:
    """Extract current chat session context from page."""
    try:
        result = ab_eval(EXTRACT_CHAT_CONTEXT_JS, env=env, timeout=15)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {"title": None, "company": None, "recruiter": None, "url": ""}


def validate_target_context(
    expected: Dict[str, Optional[str]],
    actual: Dict[str, Optional[str]],
    retry_count: int,
) -> Tuple[bool, str]:
    """Validate that current chat context matches expected job.

    Returns (is_valid, error_code).
    - Valid if: chat URL jobId matches expected job ID (most reliable)
    - OR: (jobTitle + company match >= 2 fields) OR (title + company match)
    - Allows 1 retry on mismatch
    """
    # URL-based validation: chat URL contains jobId parameter matching expected job
    act_url = (actual.get("url") or "")
    exp_url = (expected.get("jobUrl") or expected.get("link") or "")
    if act_url and exp_url:
        m = re.search(r'/job_detail/([^/.]+)', exp_url)
        if m:
            expected_job_id = m.group(1)
            if f"jobId={expected_job_id}" in act_url:
                return True, ""

    # DOM-based fallback validation
    exp_title = (expected.get("jobTitle") or "").strip().lower()
    exp_company = (expected.get("company") or "").strip().lower()
    exp_recruiter = (expected.get("recruiter") or "").strip().lower()

    act_title = (actual.get("title") or "").strip().lower()
    act_company = (actual.get("company") or "").strip().lower()
    act_recruiter = (actual.get("recruiter") or "").strip().lower()

    # Count matches
    matches = 0
    if exp_title and act_title:
        if exp_title in act_title or act_title in exp_title:
            matches += 1
    if exp_company and act_company:
        if exp_company in act_company or act_company in exp_company:
            matches += 1
    if exp_recruiter and act_recruiter:
        if exp_recruiter in act_recruiter or act_recruiter in exp_recruiter:
            matches += 1

    # Require at least 2 matches OR (title + company match)
    if matches >= 2:
        return True, ""

    title_match = bool(exp_title and act_title and (exp_title in act_title or act_title in exp_title))
    company_match = bool(exp_company and act_company and (exp_company in act_company or act_company in exp_company))
    if title_match and company_match:
        return True, ""

    # Mismatch - allow retry if not already retried
    if retry_count == 0:
        return False, "target_context_mismatch_retry"

    return False, "target_context_mismatch"


# ────────────────────────── Post-send Verification ──────────────────────────

EXTRACT_CHAT_SEND_STATE_JS = """(() => {
  // Snapshot send state before clicking "发送".
  const inputSelectors = [
    '#chat-input',
    '.chat-input[contenteditable="true"]',
    '.chat-input textarea',
    '.chat-input [contenteditable="true"]',
    '.chat-input',
    'textarea[class*="input"]',
    '.message-input textarea',
    '.edit-area textarea',
    '.edit-area [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
  ];
  let inputText = '';
  for (const sel of inputSelectors) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      inputText = (el.value || el.innerText || el.textContent || '').trim();
      break;
    }
  }

  let messageCount = 0;
  const msgSelectors = [
    '.chat-msg-list .msg-item',
    '.message-list .msg-item',
    '[class*="msg-item"]',
    '.chat-content .msg',
  ];
  for (const sel of msgSelectors) {
    const msgs = document.querySelectorAll(sel);
    if (msgs && msgs.length > messageCount) {
      messageCount = msgs.length;
    }
  }

  return { inputText, messageCount };
})()"""


VERIFY_SEND_JS_TMPL = r"""((expectedPrefix, preCount) => {
  const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const ep = normalize(expectedPrefix || '');
  const beforeCount = Number.isFinite(preCount) ? preCount : 0;

  const inputSelectors = [
    '#chat-input',
    '.chat-input[contenteditable="true"]',
    '.chat-input textarea',
    '.chat-input [contenteditable="true"]',
    '.chat-input',
    'textarea[class*="input"]',
    '.message-input textarea',
    '.edit-area textarea',
    '.edit-area [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
  ];
  let inputText = '';
  for (const sel of inputSelectors) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      inputText = normalize(el.value || el.innerText || el.textContent || '');
      break;
    }
  }
  const inputCleared = !inputText;

  const msgSelectors = [
    '.chat-msg-list .msg-item',
    '.message-list .msg-item',
    '[class*="msg-item"]',
    '.chat-content .msg',
  ];
  const seen = new Set();
  const items = [];
  for (const sel of msgSelectors) {
    const nodes = document.querySelectorAll(sel);
    for (const n of nodes) {
      if (!n) continue;
      const key = n.dataset?.id || n.getAttribute?.('data-id') || `${sel}:${items.length}:${(n.textContent || '').slice(0, 24)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(n);
    }
  }
  const messageCount = items.length;
  const messageCountIncreased = messageCount > beforeCount;

  let messagePrefixMatched = false;
  const tail = items.slice(Math.max(0, items.length - 8));
  for (const n of tail) {
    const t = normalize(n.innerText || n.textContent || '');
    if (!t) continue;
    if (ep && t.includes(ep)) {
      messagePrefixMatched = true;
      break;
    }
  }

  const toastSelectors = [
    '.toast-success',
    '[class*="success"]',
  ];
  let toastSuccess = false;
  for (const sel of toastSelectors) {
    const el = document.querySelector(sel);
    if (el && /成功|send.*success/i.test(el.textContent || '')) {
      toastSuccess = true;
      break;
    }
  }

  const verified =
    (messageCountIncreased && messagePrefixMatched) ||
    (inputCleared && messagePrefixMatched) ||
    (toastSuccess && inputCleared);

  return {
    inputCleared,
    inputText,
    beforeCount,
    messageCount,
    messageCountIncreased,
    messagePrefixMatched,
    toastSuccess,
    verified
  };
})(%s, %d)"""


def verify_send_success(env: Dict[str, str], expected_prefix: str, pre_message_count: int) -> Tuple[bool, str]:
    """Verify message was actually sent to current session.
    
    Returns (verified, method).
    """
    verify_js = VERIFY_SEND_JS_TMPL % (
        json.dumps((expected_prefix or "").strip(), ensure_ascii=False),
        int(pre_message_count),
    )
    for _ in range(3):
        try:
            result = ab_eval(verify_js, env=env, timeout=15)
            if isinstance(result, dict):
                method = []
                if result.get("inputCleared"):
                    method.append("input_cleared")
                if result.get("messageCountIncreased"):
                    method.append("message_count_increased")
                if result.get("messagePrefixMatched"):
                    method.append("message_prefix_matched")
                if result.get("toastSuccess"):
                    method.append("toast_success")
                verified = bool(result.get("verified", False))
                if verified:
                    return True, "+".join(method)
        except Exception:
            pass
        time.sleep(0.35)
    return False, "check_failed"



# ────────────────────────── Extract job info ──────────────────────────

EXTRACT_JOB_INFO_JS = """(() => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const firstText = (selectors) => {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (!el) continue;
      const t = clean(el.textContent);
      if (t) return t;
    }
    return null;
  };
  // Title: h1 inside .name (not .name itself which includes salary)
  const jobTitle = firstText([
    ".info-primary .name h1",
    ".job-banner .name h1",
    ".name h1",
    "h1",
  ]);
  // Company: sidebar has the real name; fallback to page title
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
  // Location: dedicated .text-city element
  const location = firstText([
    ".info-primary .text-city",
    ".text-city",
  ]) || "";
  // Recruiter: first text node in .job-boss-info
  let recruiter = null;
  const bossInfoEl = document.querySelector(".job-boss-info");
  if (bossInfoEl) {
    for (const node of bossInfoEl.childNodes) {
      if (node.nodeType === 3) {
        const t = node.textContent.trim();
        if (t) { recruiter = t; break; }
      }
    }
    if (!recruiter) recruiter = clean(bossInfoEl.textContent).split(/\\s/)[0] || null;
  }
  if (!recruiter) recruiter = firstText([".boss-info-attr .name", ".boss-card .name"]);
  return { jobTitle, company, salary, location, recruiter };
})()"""


# ────────────────────────── Page health check ──────────────────────────

ENSURABLE_SENDABLE_PAGE_JS = """(() => {
  const url = location.href || '';
  const hasChatInput = !!document.querySelector('#chat-input, .chat-input[contenteditable="true"], .chat-input textarea, textarea[class*="input"], .message-input textarea, .edit-area textarea');
  const hasSendBtn = !!document.querySelector('button.btn-send, button[type="send"], .btn-sure-v2, .btn-send, [class*="send-message"]');
  const hasChatButton = !!document.querySelector('.btn-startchat, .op-btn-chat, a[ka="job-commu"], button[class*="communicate"], .job-op .btn');
  return { 
    hasChatInput, 
    hasSendBtn, 
    hasChatButton,
    url 
  };
})()"""


def ensure_sendable_page(job_url: str, env: Dict[str, str]) -> Dict[str, Any]:
    """Check if page is in a sendable state before attempting to send.
    
    Returns dict with {ok, reason, url, stage}
    - ok: True if page is sendable
    - reason: error code if not sendable
    - stage: which check failed
    """
    # Extract job_id from job_url
    m = re.search(r"/job_detail/([^/?#]+)", job_url)
    job_key = m.group(1) if m else None
    
    try:
        cur_url = ab_eval("location.href", env=env, timeout=10)
    except Exception:
        cur_url = ""
    
    # Check URL contains job_key
    if job_key and job_key not in cur_url:
        return {
            "ok": False,
            "reason": "preflight_failed",
            "url": cur_url,
            "stage": "url_mismatch",
            "expected": job_url,
        }
    
    # Check page has chat elements
    try:
        page_state = ab_eval(ENSURABLE_SENDABLE_PAGE_JS, env=env, timeout=15)
    except Exception as e:
        return {
            "ok": False,
            "reason": "preflight_failed",
            "url": cur_url,
            "stage": "page_state_check",
            "error": str(e),
        }
    
    if not isinstance(page_state, dict):
        return {
            "ok": False,
            "reason": "preflight_failed",
            "url": cur_url,
            "stage": "page_state_parse",
        }
    
    has_chat_input = page_state.get("hasChatInput", False)
    has_send_btn = page_state.get("hasSendBtn", False)
    has_chat_button = page_state.get("hasChatButton", False)
    
    # Must have either chat input area OR send button OR chat button
    if not (has_chat_input or has_send_btn or has_chat_button):
        return {
            "ok": False,
            "reason": "preflight_failed",
            "url": cur_url,
            "stage": "no_sendable_element",
        }
    
    return {"ok": True, "url": cur_url}


# ────────────────────────── Click "立即沟通" ──────────────────────────

EXTRACT_CHAT_ENTRY_URL_JS = """(() => {
  const out = [];
  const seen = new Set();
  const pushHref = (href, source, text='') => {
    if (!href) return;
    try {
      const abs = new URL(href, location.origin).href;
      if (seen.has(abs)) return;
      seen.add(abs);
      out.push({ href: abs, source, text });
    } catch (_) {}
  };

  const candidates = Array.from(document.querySelectorAll(
    '.btn-startchat, .op-btn-chat, a[ka="job-commu"], .job-op .btn, a[href*="/web/geek/chat"]'
  ));

  for (const el of candidates) {
    const text = (el.innerText || el.textContent || '').trim();
    const href = el.getAttribute('href') || el.getAttribute('data-href') || '';
    if (/立即沟通|继续沟通|打招呼/.test(text) || /\\/web\\/geek\\/chat/.test(href)) {
      pushHref(href, 'candidate', text);
    }
  }

  // Some BOSS pages keep chat URL only on location-ish attributes.
  for (const el of Array.from(document.querySelectorAll('a, button'))) {
    const text = (el.innerText || el.textContent || '').trim();
    if (!/立即沟通|继续沟通|打招呼/.test(text)) continue;
    const href = el.getAttribute('href') || el.getAttribute('data-url') || el.getAttribute('data-href') || '';
    pushHref(href, 'button_like', text);
  }

  return out;
})()"""


def _expected_job_id(job_url: str) -> str:
    m = re.search(r"/job_detail/([^/?#.]+)", job_url or "")
    return m.group(1) if m else ""


def url_contains_expected_job(url: str, job_url: str) -> bool:
    expected_job_id = _expected_job_id(job_url)
    if not expected_job_id:
        return False
    u = str(url or "")
    return "/web/geek/chat" in u and f"jobId={expected_job_id}" in u


def try_open_chat_by_href(job_url: str, env: Dict[str, str]) -> bool:
    """Best-effort fallback: open chat URL extracted from current job page DOM.

    This keeps navigation deterministic when direct clicking is blocked by overlays.
    Safety rule: only accept chat links with matching jobId.
    """
    expected_job_id = _expected_job_id(job_url)
    try:
        urls = ab_eval(EXTRACT_CHAT_ENTRY_URL_JS, env=env, timeout=15)
    except Exception as e:
        log("WARN", f"extract chat entry URL failed: {e}")
        return False

    if not isinstance(urls, list) or not urls:
        return False

    for item in urls:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        if "/web/geek/chat" not in href:
            continue
        if expected_job_id and f"jobId={expected_job_id}" not in href:
            continue
        try:
            log("INFO", f"Fallback: opening extracted chat URL ({item.get('source')})")
            ab_run(["open", href], env=env, timeout=20)
            jitter_sleep(1.0, 2.0)
            cur_url = ab_eval("location.href", env=env, timeout=10)
            cur_url = str(cur_url or "")
            if "/web/geek/chat" in cur_url and (
                not expected_job_id or f"jobId={expected_job_id}" in cur_url
            ):
                log("INFO", "Fallback: opened chat URL successfully")
                return True
        except Exception as e:
            log("WARN", f"Fallback open chat URL failed: {e}")
            continue
    return False


# ────── Sidebar-based chat switching (recovery for wrong-context) ──────

FIND_SIDEBAR_CONVERSATION_JS = r"""(() => {
  // Scan the chat sidebar for a conversation matching target company.
  const targetCompany = (%s).toLowerCase();
  const targetRecruiter = (%s).toLowerCase();
  const items = document.querySelectorAll('.user-list li[role=listitem]');
  for (let i = 0; i < items.length; i++) {
    const el = items[i];
    const nameEl = el.querySelector('.name-text');
    const name = nameEl ? nameEl.textContent.trim() : '';
    const spans = el.querySelectorAll('.name-box span');
    const company = spans.length > 1 ? spans[1].textContent.trim() : '';
    const companyLower = company.toLowerCase();
    const nameLower = name.toLowerCase();
    const companyMatch = targetCompany && companyLower &&
      (companyLower.indexOf(targetCompany) >= 0 || targetCompany.indexOf(companyLower) >= 0);
    const recruiterMatch = targetRecruiter && nameLower &&
      (nameLower.indexOf(targetRecruiter) >= 0 || targetRecruiter.indexOf(nameLower) >= 0);
    if (companyMatch || recruiterMatch) {
      const fc = el.querySelector('.friend-content');
      if (fc) {
        fc.click();
        return { found: true, index: i, name: name, company: company, clicked: true };
      }
      return { found: true, index: i, name: name, company: company, clicked: false };
    }
  }
  return { found: false, scanned: items.length };
})()"""


def try_switch_chat_via_sidebar(
    job_info: Dict[str, Any],
    env: Dict[str, str],
) -> bool:
    """When on the chat page but in the wrong conversation, scan the sidebar
    conversation list for the target company/recruiter and click to switch.

    Returns True if successfully switched to target conversation.
    """
    company = (job_info.get("company") or "").strip()
    recruiter = (job_info.get("recruiter") or "").strip()
    if not company and not recruiter:
        return False

    js = FIND_SIDEBAR_CONVERSATION_JS % (
        json.dumps(company, ensure_ascii=False),
        json.dumps(recruiter, ensure_ascii=False),
    )
    try:
        result = ab_eval(js, env=env, timeout=15)
    except Exception as e:
        log("WARN", f"Sidebar switch failed: {e}")
        return False

    if not isinstance(result, dict) or not result.get("found"):
        log("WARN", f"Sidebar: target not found (scanned {result.get('scanned', '?')} items)")
        return False

    if result.get("clicked"):
        log("INFO", f"Sidebar: switched to {result.get('name')} @ {result.get('company')} (index {result.get('index')})")
        jitter_sleep(1.0, 1.8)
        return True

    log("WARN", f"Sidebar: found {result.get('name')} @ {result.get('company')} but click failed")
    return False


CLICK_CHAT_JS = """(() => {
  // Phase 1: Scan ALL candidate buttons for "继续沟通" (already contacted).
  // Must detect BEFORE clicking anything.
  const selectors = [
    '.btn-startchat',
    '.op-btn-chat',
    '.btn-container .btn',
    'a[ka="job-commu"]',
    'button[class*="communicate"]',
    '.job-op .btn',
  ];
  const allEls = Array.from(document.querySelectorAll('a, button'));

  // Check selectors + all buttons for "继续沟通"
  const checkEls = [];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) checkEls.push(el);
  }
  for (const el of allEls) checkEls.push(el);

  for (const el of checkEls) {
    const text = (el.innerText || el.textContent || '').trim();
    if (/继续沟通/.test(text)) {
      return { ok: false, reason: 'already_contacted', text: text };
    }
  }

  // Phase 2: Click "立即沟通" / "打招呼" (exact match only)
  for (const sel of selectors) {
    const btn = document.querySelector(sel);
    if (btn) {
      const text = (btn.innerText || btn.textContent || '').trim();
      if (/^立即沟通$|^打招呼$/.test(text)) {
        btn.scrollIntoView({block: 'center'});
        btn.click();
        return { ok: true, selector: sel, text: text };
      }
    }
  }

  // Fallback: find any button with exact "立即沟通" or "打招呼"
  for (const btn of allEls) {
    const text = (btn.innerText || btn.textContent || '').trim();
    if (/^立即沟通$|^打招呼$/.test(text)) {
      btn.scrollIntoView({block: 'center'});
      btn.click();
      return { ok: true, selector: 'fallback', text: text };
    }
  }
  return { ok: false, reason: 'chat_button_not_found' };
})()"""

DETECT_CONTINUE_CONTACT_JS = r"""(() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll('a, button'));
  for (const el of nodes) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (/继续沟通/.test(text)) {
      return { alreadyContacted: true, text };
    }
  }
  return { alreadyContacted: false };
})()"""


def detect_continue_contact(env: Dict[str, str]) -> Tuple[bool, str]:
    """Hard gate: if page shows '继续沟通', treat as already contacted."""
    try:
        result = ab_eval(DETECT_CONTINUE_CONTACT_JS, env=env, timeout=10)
        if isinstance(result, dict) and result.get("alreadyContacted"):
            return True, str(result.get("text") or "继续沟通")
    except Exception:
        pass
    return False, ""




# ────────────────────────── Fill message & send ──────────────────────────

FILL_MSG_JS_TMPL = r"""((messageText) => {
  // Find the chat input box
  // Priority: #chat-input (BOSS v2 chat page, contenteditable DIV)
  const inputSelectors = [
    '#chat-input',
    '.chat-input[contenteditable="true"]',
    '.chat-input textarea',
    '.chat-input [contenteditable="true"]',
    '.chat-input',
    'textarea[class*="input"]',
    '.message-input textarea',
    '.edit-area textarea',
    '.edit-area [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
  ];

  let input = null;
  for (const sel of inputSelectors) {
    const el = document.querySelector(sel);
    if (el) {
      // Skip hidden/zero-size elements (e.g., hidden textarea on chat page)
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      input = el;
      break;
    }
  }

  if (!input) {
    return { ok: false, reason: 'input_not_found', url: location.href };
  }

  // Clear and fill
  input.focus();
  if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value'
    )?.set || Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value'
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(input, messageText);
    } else {
      input.value = messageText;
    }
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    // contenteditable (BOSS v2 chat uses DIV#chat-input[contenteditable])
    input.focus();
    input.innerText = messageText;
    // Dispatch multiple events to ensure Vue/React reactivity picks up the change
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
    // Also set textContent as backup (some frameworks read textContent)
    if (!input.innerText && messageText) {
      input.textContent = messageText;
      input.dispatchEvent(new Event('input', {bubbles: true}));
    }
  }

  return { ok: true, inputTag: input.tagName, inputSel: input.className };
})(%s)"""

CLICK_SEND_JS = r"""(() => {
  // 1. Exact match: BOSS直聘 send button
  //    <button type="send" class="btn-v2 btn-sure-v2 btn-send"> 发送 </button>
  const exact = document.querySelector('button.btn-send') ||
                document.querySelector('button[type="send"]') ||
                document.querySelector('.btn-sure-v2');
  if (exact) {
    exact.click();
    return { ok: true, method: 'boss_exact', tag: exact.tagName, cls: exact.className };
  }

  // 2. Broader CSS selectors
  const sendSelectors = [
    '.btn-send',
    'button[class*="send"]',
    '.chat-op .btn',
    '.send-btn',
    '[class*="send-message"]',
  ];
  for (const sel of sendSelectors) {
    const btn = document.querySelector(sel);
    if (btn) {
      btn.click();
      return { ok: true, method: sel };
    }
  }

  // 3. Search ALL visible elements for text "发送"
  const allEls = Array.from(document.querySelectorAll('button, a, div, span'));
  for (const el of allEls) {
    const text = (el.innerText || el.textContent || '').trim();
    if (/发送/.test(text) && el.offsetParent !== null) {
      el.click();
      return { ok: true, method: 'text_match', tag: el.tagName, cls: el.className };
    }
  }

  // 4. Fallback: press Enter on input
  const input = document.querySelector('textarea, [contenteditable="true"]');
  if (input) {
    input.focus();
    ['keydown', 'keypress', 'keyup'].forEach(t => {
      input.dispatchEvent(new KeyboardEvent(t, {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
        bubbles: true, cancelable: true
      }));
    });
    return { ok: true, method: 'enter_key' };
  }

  return { ok: false, reason: 'send_button_not_found' };
})()"""


# ────────────────────────── Browser cleanup ──────────────────────────

def cleanup_browser(cdp_port: int = 18801) -> None:
    """One-time browser cleanup: kill stale daemons and close extra tabs.
    Call this once at pipeline start, not per-job."""
    kill_stale_daemons()
    if not ensure_cdp_ready(cdp_port):
        log("WARN", f"cleanup_browser: cdp_not_reachable:{cdp_port}")
        return
    close_extra_tabs(cdp_port)


# ────────────────────────── Main flow ──────────────────────────

def send_greeting(
    job_url: str,
    message: str,
    cdp_port: int = 18801,
    screenshot_dir: str = f"{WORKSPACE}/.openclaw-runs/boss-greeting/screenshots",
    skip_navigation: bool = False,
    allow_intent_failed_retry: bool = False,
    no_retry: bool = False,
    capture_screenshot: bool = False,
) -> Dict[str, Any]:
    """Open job page, click chat, send greeting, return result.

    Args:
        skip_navigation: If True, skip opening the job URL (assumes page is
            already on the correct URL, e.g. after scrape_jd). Saves 4-8s.
    """

    if not ensure_cdp_ready(cdp_port):
        raise RuntimeError(f"cdp_not_reachable:{cdp_port}")

    env = prepare_env(cdp_port)
    job_info: Dict[str, Any] = {"jobTitle": None, "company": None, "salary": None, "location": None, "recruiter": None, "jobUrl": job_url}
    sem_fp: Optional[str] = None
    idem_id: Optional[str] = None

    try:
        # 1. Quick URL-only dedup (cheap, no browser needed)
        url_reason = query_existing_delivery_reason(job_url, None)
        if url_reason:
            log("INFO", f"Skipping {job_url}: {url_reason}")
            return {
                "status": "skipped",
                "reason": url_reason,
                "jobUrl": job_url,
            }

        # 2. Open job detail page (skip if already on the page)
        if not skip_navigation:
            # 2a. Pre-navigation cleanup: navigate to about:blank first to clear
            # any pending state from previous JD (prevents ERR_ABORTED).
            try:
                ab_run(["open", "about:blank"], env=env, timeout=10)
                jitter_sleep(0.3, 0.6)
            except Exception:
                pass  # Non-critical; if this fails, the actual open may still work.

            log("INFO", f"Opening: {job_url}")
            open_ok = False
            last_open_err: Optional[Exception] = None
            # Even with no_retry enabled, allow one fast retry for transient ERR_ABORTED/CDP hiccups.
            open_attempts = 2 if no_retry else 3
            for _open_attempt in range(open_attempts):
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
                    if recoverable:
                        # Navigate to about:blank before retry to clear browser state.
                        try:
                            ab_run(["open", "about:blank"], env=env, timeout=10)
                            jitter_sleep(0.3, 0.5)
                        except Exception:
                            pass
                        if ensure_cdp_ready(cdp_port):
                            env = prepare_env(cdp_port)
                        jitter_sleep(0.4, 1.0)
                        continue
                    if _open_attempt < open_attempts - 1:
                        jitter_sleep(0.6, 1.2)
                        continue
            if not open_ok:
                raise RuntimeError(f"open_failed: {last_open_err}")
            jitter_sleep(1.0, 1.8)
        else:
            log("INFO", f"Skipping navigation (page already open): {job_url}")

        # 3. Check verification
        if detect_verify(env):
            if not wait_for_verify_clear(env):
                raise RuntimeError("verify_blocked")

        # 3b. Ensure we're still on the intended job detail URL.
        # BOSS may redirect to job list/home after verification or anti-bot checks.
        m = re.search(r"/job_detail/([^/?#]+)", job_url)
        job_key = m.group(1) if m else None
        if job_key:
            # Page loading is a prerequisite — always allow enough retries.
            # BOSS anti-bot may redirect to about:blank after scraping bursts,
            # so even in no_retry mode we need ≥2 retries to recover.
            max_redirect_retries = 2 if no_retry else 3
            for rr in range(max_redirect_retries + 1):
                try:
                    cur_url = ab_eval("location.href", env=env, timeout=30)
                except Exception:
                    cur_url = ""
                cur_url = str(cur_url or "")
                # BOSS may land on either job detail or target chat URL.
                on_target = (
                    (("job_detail" in cur_url) or ("/web/geek/chat" in cur_url))
                    and (job_key in cur_url)
                )
                if on_target:
                    break
                if rr >= max_redirect_retries:
                    raise RuntimeError(f"navigation_mismatch: expected {job_url}, got {cur_url}")
                log(
                    "WARN",
                    f"Navigation mismatch after open/verify. expected contains {job_key}, got {cur_url}. "
                    f"Retrying open ({rr + 1}/{max_redirect_retries})...",
                )
                jitter_sleep(2.0, 3.5)
                ab_run(["open", job_url], env=env)
                jitter_sleep(2.0, 3.0)
                if detect_verify(env):
                    if not wait_for_verify_clear(env):
                        raise RuntimeError("verify_blocked")

        # 4. Extract job info
        info = ab_eval(EXTRACT_JOB_INFO_JS, env=env)
        if isinstance(info, dict):
            job_info.update(info)
        log("INFO", f"Job: {job_info.get('jobTitle')} @ {job_info.get('company')}")

        # 4b. Compute semantic fingerprint and intent ID
        sem_fp = semantic_fingerprint(
            job_info.get("company"),
            job_info.get("jobTitle"),
            job_info.get("recruiter"),
        )
        run_id = resolve_run_id()
        idem_id = build_intent_id(job_url, run_id, message)

        # 4c. Semantic fingerprint dedup (catches same company+title+recruiter with different URL)
        if sem_fp:
            fp_reason = query_existing_delivery_reason(job_url, sem_fp)
            if fp_reason:
                log("INFO", f"Skipping (semantic dedup): {job_info.get('jobTitle')} @ {job_info.get('company')} — {fp_reason}")
                record_greeting(
                    job_url=job_url,
                    job_title=job_info.get("jobTitle"),
                    company=job_info.get("company"),
                    salary=job_info.get("salary"),
                    location=job_info.get("location"),
                    recruiter=job_info.get("recruiter"),
                    message="",
                    status="skipped",
                    error=fp_reason,
                    semantic_fp=sem_fp,
                    intent_id=idem_id,
                )
                return {
                    "status": "skipped",
                    "reason": fp_reason,
                    "jobUrl": job_url,
                    "jobTitle": job_info.get("jobTitle"),
                    "company": job_info.get("company"),
                }

        # 4d. Intent idempotency check (prevents same job+run+message resend)
        terminal = intent_terminal_status(idem_id)
        if terminal:
            if terminal == "failed":
                if allow_intent_failed_retry:
                    log("INFO", f"Intent previously failed but retry enabled: {job_url}")
                else:
                    # Failed intents should be replayable across subsequent runs.
                    log("INFO", f"Intent previously failed, retrying: {job_url}")
            else:
                log("INFO", f"Skipping (intent already {terminal}): {job_url}")
                return {
                    "status": "skipped",
                    "reason": f"intent_already_{terminal}",
                    "jobUrl": job_url,
                    "intentId": idem_id,
                }

        # 5. Detect button state, then click via CDP (real mouse event).
        #
        # BOSS ignores navigation for JS .click() (isTrusted=false) but still
        # fires the API call.  Using `agent-browser click` (Playwright) produces
        # isTrusted=true mouse events that trigger both API + navigation.
        jitter_sleep(0.2, 0.6)

        # 5a. Combined URL-guard + button detection in ONE eval call.
        # This avoids the daemon cleanup race condition between separate
        # ab_eval("location.href") and ab_eval(detect_js) calls.
        detect_js = r"""(() => {
          // Guard: check URL first — if page drifted to about:blank, report early.
          const url = location.href;
          if (url === 'about:blank' || !url || url === 'about:srcdoc') {
            return { action: 'page_drifted', url: url };
          }
          const btns = document.querySelectorAll('.btn-startchat, .op-btn-chat, a[ka="job-commu"]');
          for (const b of btns) {
            const t = (b.innerText || b.textContent || '').trim();
            if (/继续沟通/.test(t)) return { action: 'already_contacted', text: t, url: url };
          }
          const all = Array.from(document.querySelectorAll('a, button'));
          for (const b of all) {
            const t = (b.innerText || b.textContent || '').trim();
            if (/继续沟通/.test(t)) return { action: 'already_contacted', text: t, url: url };
            if (/^立即沟通$|^打招呼$/.test(t)) return { action: 'click', text: t, url: url };
          }
          return { action: 'not_found', url: url };
        })()"""

        # Retry loop: if page drifted or button not found, recover and re-detect.
        _detect_max = 3  # Always 3 attempts (was 2 when no_retry=True, causing single-failure pattern)
        detect_result = None
        for _detect_attempt in range(_detect_max):
            try:
                detect_result = ab_eval(detect_js, env=env)
            except Exception as _det_err:
                detect_result = {"action": "eval_failed", "error": str(_det_err)}

            action = detect_result.get("action") if isinstance(detect_result, dict) else "eval_failed"
            det_url = detect_result.get("url", "") if isinstance(detect_result, dict) else ""

            if action in ("click", "already_contacted"):
                break  # Button found, proceed
            if action == "not_found" and job_key and job_key in str(det_url):
                break  # Page is correct but button genuinely not found

            # Page drifted or eval failed — recover by reopening JD URL.
            log("WARN", f"Detect attempt {_detect_attempt + 1}/{_detect_max}: action={action}, url={str(det_url)[:60]}")
            if _detect_attempt >= _detect_max - 1:
                log("ERROR", f"Button detect failed after {_detect_max} attempts")
                break
            try:
                # Full cleanup: navigate to about:blank first, then wait, then reopen
                ab_run(["open", "about:blank"], env=env, timeout=10)
                jitter_sleep(1.0, 2.0)
                ab_run(["open", job_url], env=env, timeout=30)
                jitter_sleep(4.0, 6.0)  # Longer wait for anti-bot cooldown
                if detect_verify(env):
                    if not wait_for_verify_clear(env):
                        raise RuntimeError("verify_blocked")
            except RuntimeError:
                raise
            except Exception as _rec_err:
                log("WARN", f"Recovery open failed: {_rec_err}")
                if ensure_cdp_ready(cdp_port):
                    env = prepare_env(cdp_port)
                jitter_sleep(2.0, 3.0)

        if isinstance(detect_result, dict) and detect_result.get("action") == "already_contacted":
            log("INFO", f"Already contacted: {job_info.get('jobTitle')} (button: {detect_result.get('text')})")
            record_greeting(
                job_url=job_url,
                job_title=job_info.get("jobTitle"),
                company=job_info.get("company"),
                salary=job_info.get("salary"),
                location=job_info.get("location"),
                recruiter=job_info.get("recruiter"),
                message="",
                status="already_contacted",
                semantic_fp=sem_fp,
                intent_id=idem_id,
            )
            return {
                "status": "skipped",
                "reason": "already_contacted",
                "jobUrl": job_url,
                "jobTitle": job_info.get("jobTitle"),
            }

        entered_chat_via_fallback = False
        if not isinstance(detect_result, dict) or detect_result.get("action") != "click":
            # Fallback 1: open deterministic chat URL extracted from page.
            if try_open_chat_by_href(job_url, env):
                entered_chat_via_fallback = True
                log("INFO", "chat entry fallback: opened by href")
            else:
                # Fallback 2: we may already be in the target chat.
                try:
                    cur_url = str(ab_eval("location.href", env=env, timeout=10) or "")
                except Exception:
                    cur_url = ""
                if url_contains_expected_job(cur_url, job_url):
                    entered_chat_via_fallback = True
                    log("INFO", "chat entry fallback: already on target chat url")
                else:
                    raise RuntimeError(f"click_chat_failed: {detect_result}")
        else:
            # Shortcut: open deterministic chat URL directly when available.
            # This avoids flaky button clicks and wrong-chat drift.
            if try_open_chat_by_href(job_url, env):
                entered_chat_via_fallback = True
                log("INFO", "chat entry shortcut: opened by href")

        # 5c. Preflight check: ensure page is in sendable state
        if not entered_chat_via_fallback:
            log("INFO", "Running preflight page health check...")
            preflight = ensure_sendable_page(job_url, env)
            preflight_recover_tries = 2 if no_retry else 3
            if (not preflight.get("ok")) and preflight.get("stage") == "url_mismatch":
                # Anti-bot redirects may silently drift us to another job/list page.
                # Recover by reopening target job and running one extra preflight cycle.
                log(
                    "WARN",
                    f"Preflight url_mismatch, reopening target before fail-fast: {preflight.get('url')}",
                )
                for rr in range(preflight_recover_tries):
                    try:
                        ab_run(["open", job_url], env=env, timeout=30)
                        jitter_sleep(0.8, 1.5)
                        if detect_verify(env):
                            if not wait_for_verify_clear(env):
                                raise RuntimeError("verify_blocked")
                        preflight = ensure_sendable_page(job_url, env)
                        if preflight.get("ok"):
                            break
                    except Exception as _recover_err:
                        if rr >= preflight_recover_tries - 1:
                            raise RuntimeError(f"preflight_recover_failed: {_recover_err}")
                        jitter_sleep(0.5, 1.0)
            if not preflight.get("ok"):
                raise RuntimeError(f"preflight_failed: stage={preflight.get('stage')}, url={preflight.get('url')}")

        # 5b. Click via CDP (Playwright mouse event) — NOT JS .click()
        # BOSS ignores page navigation for JS .click() (isTrusted=false).
        # Use .info-primary .btn-startchat to avoid "matched 2 elements" error.
        if not entered_chat_via_fallback:
            log("INFO", f"Clicking '{detect_result.get('text')}' via CDP...")
            try:
                ab_run(["click", ".info-primary .btn-startchat"], env=env, timeout=15)
            except Exception as e:
                log("WARN", f"CDP click failed: {e}, trying broader selector...")
                try:
                    ab_run(["click", ".btn-startchat >> nth=0"], env=env, timeout=15)
                except Exception:
                    # Last resort: use JS click (sends greeting but may not navigate)
                    log("WARN", "CDP click unavailable, falling back to JS click...")
                    ab_eval(CLICK_CHAT_JS, env=env)
            log("INFO", f"Clicked: {detect_result.get('text')}")

        # 6. Fast path: detect popup "继续沟通" on current page.
        # After clicking "立即沟通", BOSS shows a popup/dialog with "继续沟通".
        # Clicking it navigates directly to chat — much faster than reloading.
        if not entered_chat_via_fallback:
            jitter_sleep(1.0, 1.8)  # Wait for popup to appear
            POPUP_CONTINUE_JS = """(() => {
                // Look for "继续沟通" buttons that are visible and likely in a popup/dialog/overlay.
                const allBtns = Array.from(document.querySelectorAll(
                    '.dialog-container a, .dialog-wrap a, .greet-boss-dialog a, ' +
                    '.startchat-dialog a, .dialog a, [class*="dialog"] a, ' +
                    '[class*="popup"] a, [class*="modal"] a, [class*="overlay"] a, ' +
                    '.greet-boss-dialog .btn, [class*="dialog"] .btn, [class*="dialog"] button'
                ));
                // Also search all visible links/buttons containing "继续沟通"
                const allLinks = Array.from(document.querySelectorAll('a, button'));
                const candidates = [...new Set([...allBtns, ...allLinks])];
                for (const el of candidates) {
                    const text = (el.textContent || '').trim();
                    if (text.includes('继续沟通') && el.offsetParent !== null) {
                        // Skip the main page button (.info-primary .btn-startchat)
                        if (el.closest('.info-primary')) continue;
                        el.click();
                        return {found: true, text: text, tag: el.tagName, cls: el.className};
                    }
                }
                return {found: false};
            })()"""
            try:
                popup_result = ab_eval(POPUP_CONTINUE_JS, env=env, timeout=8)
                if isinstance(popup_result, dict) and popup_result.get("found"):
                    log("INFO", f"Clicked popup '继续沟通' (fast path): tag={popup_result.get('tag')}, cls={popup_result.get('cls')}")
                    jitter_sleep(1.2, 2.2)  # Wait for chat page navigation
                else:
                    log("INFO", "No popup '继续沟通' found, using standard path.")
            except Exception as e_popup:
                log("WARN", f"Popup detection failed: {e_popup}, using standard path.")
        else:
            jitter_sleep(1.2, 2.2)

        # 6b. Verify we navigated to the chat page.
        # If CDP click worked, we should be on /web/geek/chat now.
        # If not, fallback: reload job page and click "继续沟通" via CDP.
        try:
            post_click_url = ab_eval("location.href", env=env, timeout=15)
        except Exception:
            post_click_url = ""
        expected_job_id = _expected_job_id(job_url)
        if (
            isinstance(post_click_url, str)
            and "/web/geek/chat" in post_click_url
            and expected_job_id
            and f"jobId={expected_job_id}" not in post_click_url
        ):
            log("WARN", "Landed on non-target chat, trying sidebar switch then URL fallback...")
            # Best recovery: switch via sidebar conversation list (SPA, URL won't change)
            if try_switch_chat_via_sidebar(job_info, env):
                pass  # sidebar click switches chat without URL change; validation will follow
            elif try_open_chat_by_href(job_url, env):
                try:
                    post_click_url = ab_eval("location.href", env=env, timeout=10)
                except Exception:
                    post_click_url = ""
        if isinstance(post_click_url, str) and "/web/geek/chat" not in post_click_url:
            log("WARN", "Page did NOT navigate to chat after clicking 立即沟通.")
            # First fallback: extract and open a deterministic chat URL from current DOM.
            if try_open_chat_by_href(job_url, env):
                try:
                    post_click_url = ab_eval("location.href", env=env, timeout=10)
                except Exception:
                    post_click_url = ""
            if isinstance(post_click_url, str) and "/web/geek/chat" in post_click_url:
                pass
            else:
                # Reload the job detail page and detect ACTUAL button state.
                ab_run(["open", job_url], env=env)
                jitter_sleep(1.0, 1.8)
                reload_detect = ab_eval(detect_js, env=env)
                reload_btn_action = reload_detect.get("action") if isinstance(reload_detect, dict) else "not_found"
                reload_btn_text = reload_detect.get("text", "") if isinstance(reload_detect, dict) else ""
                log("INFO", f"After reload, button state: action={reload_btn_action}, text={reload_btn_text}")

                if reload_btn_action == "already_contacted":
                    # "继续沟通" — the first click DID fire the API (chat exists).
                    # Just click to navigate to the existing chat.
                    log("INFO", "Button is '继续沟通' — first click worked, navigating to chat...")
                    try:
                        ab_run(["click", ".info-primary .btn-startchat"], env=env, timeout=15)
                        log("INFO", "Clicked '继续沟通' via CDP, waiting for chat page...")
                        jitter_sleep(1.2, 2.2)
                    except Exception as e2:
                        if try_open_chat_by_href(job_url, env):
                            pass
                        else:
                            log("ERROR", f"chat_navigation_failed: 继续沟通 click failed: {e2}")
                            raise RuntimeError(f"chat_navigation_failed: {e2}")
                elif reload_btn_action == "click":
                    # "立即沟通" still showing — first click FAILED to fire the API.
                    # This means no chat was created yet. Click again.
                    log("WARN", f"Button still '{reload_btn_text}' — first click did NOT work. Retrying...")
                    try:
                        ab_run(["click", ".info-primary .btn-startchat"], env=env, timeout=15)
                        log("INFO", f"Retry-clicked '{reload_btn_text}' via CDP")
                        jitter_sleep(2.0, 3.0)  # Wait longer for chat creation
                        # Check navigation after retry click
                        try:
                            post_retry_url = ab_eval("location.href", env=env, timeout=10)
                        except Exception:
                            post_retry_url = ""
                        if isinstance(post_retry_url, str) and "/web/geek/chat" in post_retry_url:
                            log("INFO", "Retry click navigated to chat successfully")
                        else:
                            # Last resort: reload again, now button should be "继续沟通"
                            log("WARN", "Retry click did not navigate. Reloading for 继续沟通...")
                            ab_run(["open", job_url], env=env)
                            jitter_sleep(1.0, 1.8)
                            ab_run(["click", ".info-primary .btn-startchat"], env=env, timeout=15)
                            jitter_sleep(1.2, 2.2)
                    except Exception as e3:
                        if try_open_chat_by_href(job_url, env):
                            pass
                        else:
                            log("ERROR", f"chat_navigation_failed: 立即沟通 retry failed: {e3}")
                            raise RuntimeError(f"chat_navigation_failed: {e3}")
                else:
                    # Button not found — try href fallback or fail
                    if try_open_chat_by_href(job_url, env):
                        pass
                    else:
                        log("ERROR", f"chat_navigation_failed: button not found after reload: {reload_detect}")
                        raise RuntimeError(f"chat_navigation_failed: button_not_found")

        # 6c. VALIDATE: Check current chat session matches target job
        log("INFO", "Validating chat session target...")
        retry_count = 0
        # Keep one quick context retry even in no_retry mode to recover stale chat drift.
        max_context_retries = 1 if no_retry else 2
        context_valid = False
        last_error_code = ""
        
        while retry_count <= max_context_retries:
            # Extract current chat context
            chat_context = extract_chat_context(env)
            log(f"INFO", f"Chat context: title={chat_context.get('title')}, company={chat_context.get('company')}, url={chat_context.get('url')}")
            
            # Validate against expected job
            is_valid, error_code = validate_target_context(job_info, chat_context, retry_count)
            
            if is_valid:
                context_valid = True
                log("INFO", "Chat session target validated successfully")
                break
            
            last_error_code = error_code
            log(f"WARN", f"Target context mismatch (retry {retry_count}/{max_context_retries}): {error_code}")
            
            if retry_count < max_context_retries:
                # Retry path 0 (best): switch via sidebar conversation list.
                # On the chat page, the left sidebar lists all conversations.
                # Clicking the matching one is the most reliable recovery.
                try:
                    cur_url_check = str(ab_eval("location.href", env=env, timeout=10) or "")
                except Exception:
                    cur_url_check = ""
                if "/web/geek/chat" in cur_url_check and try_switch_chat_via_sidebar(job_info, env):
                    log("INFO", "Recovered via sidebar switch")
                # Retry path 1: direct target chat URL.
                elif try_open_chat_by_href(job_url, env):
                    jitter_sleep(1.0, 1.8)
                else:
                    # Retry path 2: reopen job page and click continue.
                    log("INFO", "Retrying: reopening job page...")
                    ab_run(["open", job_url], env=env)
                    jitter_sleep(1.0, 1.8)

                    # Click "继续沟通" via CDP
                    try:
                        ab_run(["click", ".info-primary .btn-startchat"], env=env, timeout=15)
                        jitter_sleep(1.2, 2.2)
                    except Exception as retry_err:
                        # Last fallback: open extracted chat URL with matching jobId.
                        if try_open_chat_by_href(job_url, env):
                            pass
                        else:
                            log("ERROR", f"Retry click failed: {retry_err}")
                            raise RuntimeError(f"chat_navigation_failed: retry failed: {retry_err}")
            
            retry_count += 1
        
        if not context_valid:
            # Get current URL for error evidence
            try:
                cur_url = ab_eval("location.href", env=env, timeout=10)
            except Exception:
                cur_url = "unknown"
            
            error_msg = f"target_context_mismatch: expected jobTitle={job_info.get('jobTitle')}, company={job_info.get('company')}, actual title={chat_context.get('title')}, company={chat_context.get('company')}, url={cur_url}"
            log("ERROR", error_msg)
            raise RuntimeError(error_msg)

        # 7a. Fill message into input (with retry — chat input may load slowly)
        fill_js = FILL_MSG_JS_TMPL % json.dumps(message, ensure_ascii=False)
        fill_result = ab_eval(fill_js, env=env)
        if not isinstance(fill_result, dict) or not fill_result.get("ok"):
            if no_retry:
                raise RuntimeError(f"fill_failed: {fill_result}")
            # Retry: wait longer for chat window to fully render
            log("WARN", "Input not found, retrying shortly...")
            jitter_sleep(1.5, 2.2)
            fill_result = ab_eval(fill_js, env=env)
            if not isinstance(fill_result, dict) or not fill_result.get("ok"):
                # Last retry with even longer wait
                log("WARN", "Input still not found, final retry...")
                jitter_sleep(2.0, 2.8)
                fill_result = ab_eval(fill_js, env=env)
                if not isinstance(fill_result, dict) or not fill_result.get("ok"):
                    raise RuntimeError(f"fill_failed: {fill_result}")
        log("INFO", f"Message filled into {fill_result.get('inputTag')} ({fill_result.get('inputSel', '')[:50]})")

        # 7a-extra. Verify message was injected correctly
        # Extract first 16 characters of message for verification
        message_prefix = message[:16]
        verify_js = r"""(() => {
          const inputSelectors = [
            '#chat-input',
            '.chat-input[contenteditable="true"]',
            '.chat-input textarea',
            '.chat-input [contenteditable="true"]',
            '.chat-input',
            'textarea[class*="input"]',
            '.message-input textarea',
            '.edit-area textarea',
            '.edit-area [contenteditable="true"]',
            '[contenteditable="true"]',
            'textarea',
          ];
          let input = null;
          for (const sel of inputSelectors) {
            const el = document.querySelector(sel);
            if (el) {
              const rect = el.getBoundingClientRect();
              if (rect.width === 0 && rect.height === 0) continue;
              input = el;
              break;
            }
          }
          if (!input) return { ok: false, reason: 'input_not_found' };
          const currentText = (input.value || input.innerText || input.textContent || '').slice(0, 20);
          return { ok: true, currentText };
        })()"""
        
        # First verification attempt
        verify_result = ab_eval(verify_js, env=env)
        current_text = verify_result.get("currentText", "") if isinstance(verify_result, dict) else ""
        
        if message_prefix not in current_text:
            if no_retry:
                try:
                    cur_url = ab_eval("location.href", env=env, timeout=10)
                except Exception:
                    cur_url = "unknown"
                raise RuntimeError(
                    f"message_injection_failed: expected prefix '{message_prefix}' not found in input. Current URL: {cur_url}"
                )
            # Retry fill once
            log("WARN", f"Message injection verification failed. Retrying fill...")
            jitter_sleep(0.5, 1.0)
            fill_result = ab_eval(fill_js, env=env)
            if not isinstance(fill_result, dict) or not fill_result.get("ok"):
                raise RuntimeError(f"fill_failed_retry: {fill_result}")
            
            # Verify again after retry
            verify_result = ab_eval(verify_js, env=env)
            current_text = verify_result.get("currentText", "") if isinstance(verify_result, dict) else ""
            if message_prefix not in current_text:
                # Get current URL for error reporting
                try:
                    cur_url = ab_eval("location.href", env=env, timeout=10)
                except Exception:
                    cur_url = "unknown"
                raise RuntimeError(f"message_injection_failed: expected prefix '{message_prefix}' not found in input. Current URL: {cur_url}")
        
        log("INFO", f"Message injection verified: '{current_text[:30]}...'")

        # Snapshot pre-send state for strict post-send verification.
        pre_message_count = 0
        try:
            pre_state = ab_eval(EXTRACT_CHAT_SEND_STATE_JS, env=env, timeout=10)
            if isinstance(pre_state, dict):
                pre_message_count = int(pre_state.get("messageCount") or 0)
        except Exception:
            pre_message_count = 0

        # 7b. Wait for UI to enable send button
        jitter_sleep(0.2, 0.5)

        # 7c. Click send button (or press Enter as fallback), with retry
        send_result = ab_eval(CLICK_SEND_JS, env=env)
        if not isinstance(send_result, dict) or not send_result.get("ok"):
            if no_retry:
                raise RuntimeError(f"send_button_not_found: {send_result}")
            # Retry once after waiting for chat UI to fully load
            log("WARN", "Send button not found, retrying shortly...")
            jitter_sleep(1.0, 1.8)
            # Re-fill message (UI may have reset)
            ab_eval(fill_js, env=env)
            jitter_sleep(0.2, 0.5)
            send_result = ab_eval(CLICK_SEND_JS, env=env)
            if not isinstance(send_result, dict) or not send_result.get("ok"):
                raise RuntimeError(f"send_button_not_found: {send_result}")
        log("INFO", f"Message sent via: {send_result.get('method')}")

        # 7d. POST-SEND VERIFICATION: Confirm message was sent to correct session
        log("INFO", "Verifying send success...")
        jitter_sleep(0.6, 1.2)
        verify_prefix = message[:24]
        verified, verify_method = verify_send_success(env, verify_prefix, pre_message_count)
        
        # Soft fallback: if strict DOM verification fails but we're on correct chat
        # and the input box is already cleared, treat as sent.
        if not verified:
            soft_js = r"""((expectedPrefix) => {
              const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const ep = normalize(expectedPrefix || '');
              const inputSelectors = [
                '#chat-input',
                '.chat-input[contenteditable="true"]',
                '.chat-input textarea',
                '.chat-input [contenteditable="true"]',
                '.chat-input',
                'textarea[class*="input"]',
                '.message-input textarea',
                '.edit-area textarea',
                '.edit-area [contenteditable="true"]',
                '[contenteditable="true"]',
                'textarea',
              ];
              let inputText = '';
              for (const sel of inputSelectors) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                inputText = normalize(el.value || el.innerText || el.textContent || '');
                break;
              }
              const url = location.href || '';
              const inputCleared = !inputText;
              const inputChanged = !!ep && !inputText.includes(ep);
              return { url, inputCleared, inputChanged, inputText };
            })(%s)""" % json.dumps(verify_prefix, ensure_ascii=False)
            try:
                soft = ab_eval(soft_js, env=env, timeout=15)
            except Exception:
                soft = {}
            if isinstance(soft, dict):
                soft_url = str(soft.get("url") or "")
                soft_ok = url_contains_expected_job(soft_url, job_url) and (
                    bool(soft.get("inputCleared")) or bool(soft.get("inputChanged"))
                )
                if soft_ok:
                    verified = True
                    verify_method = f"soft_url_input:{'cleared' if soft.get('inputCleared') else 'changed'}"
                    log("WARN", f"Strict verify failed, accepted by soft verify: {verify_method}")
        
        if not verified:
            # Get evidence for error
            try:
                cur_url = ab_eval("location.href", env=env, timeout=10)
            except Exception:
                cur_url = "unknown"
            chat_context = extract_chat_context(env)
            
            error_msg = f"send_unverified: verify_method={verify_method}, expected jobTitle={job_info.get('jobTitle')}, company={job_info.get('company')}, actual title={chat_context.get('title')}, url={cur_url}"
            log("ERROR", error_msg)
            raise RuntimeError(error_msg)
        
        log("INFO", f"Send verified successfully: {verify_method}")

        # 7e. Post-send context re-validation (warning only — message already sent)
        try:
            post_send_ctx = extract_chat_context(env)
            post_valid, post_err = validate_target_context(job_info, post_send_ctx, retry_count=1)
            if not post_valid:
                log("WARN", f"Post-send context drift: {post_err}. "
                    f"Expected: {job_info.get('jobTitle')} @ {job_info.get('company')}, "
                    f"Got: {post_send_ctx.get('title')} @ {post_send_ctx.get('company')}")
        except Exception as drift_err:
            log("WARN", f"Post-send context check failed: {drift_err}")

        # 8. Wait for UI to settle + optional screenshot
        jitter_sleep(0.4, 1.0)
        screenshot_path = None
        if capture_screenshot:
            os.makedirs(screenshot_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            url_hash = hashlib.md5(job_url.encode()).hexdigest()[:8]
            screenshot_path = os.path.join(screenshot_dir, f"boss_{url_hash}_{ts}.png")
            try:
                ab_run(["screenshot", screenshot_path], env=env, timeout=30)
            except Exception as e:
                log("WARN", f"Screenshot failed: {e}")
                screenshot_path = None

        # 9. Record success
        record_greeting(
            job_url=job_url,
            job_title=job_info.get("jobTitle"),
            company=job_info.get("company"),
            salary=job_info.get("salary"),
            location=job_info.get("location"),
            recruiter=job_info.get("recruiter"),
            message=message,
            status="sent",
            screenshot_path=screenshot_path,
            semantic_fp=sem_fp,
            intent_id=idem_id,
        )

        return {
            "status": "ok",
            "jobUrl": job_url,
            "jobTitle": job_info.get("jobTitle"),
            "company": job_info.get("company"),
            "salary": job_info.get("salary"),
            "location": job_info.get("location"),
            "recruiter": job_info.get("recruiter"),
            "screenshotPath": screenshot_path,
        }

    except Exception as e:
        # Optional screenshot on failure for debugging
        screenshot_path = None
        if capture_screenshot:
            try:
                os.makedirs(screenshot_dir, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                url_hash = hashlib.md5(job_url.encode()).hexdigest()[:8]
                screenshot_path = os.path.join(screenshot_dir, f"boss_{url_hash}_failed_{ts}.png")
                ab_run(["screenshot", screenshot_path], env=env, timeout=30)
            except Exception as screenshot_err:
                log("WARN", f"Failed to capture error screenshot: {screenshot_err}")
        
        # Enhanced error with evidence fields
        error_str = str(e)[:2000]
        
        # Try to get current URL for evidence
        current_url = "unknown"
        try:
            if 'env' in dir():
                current_url = ab_eval("location.href", env=env, timeout=5)
        except Exception:
            pass
        
        # Build enhanced error with evidence
        enhanced_error = json.dumps({
            "message": error_str,
            "expected_title": job_info.get("jobTitle"),
            "expected_company": job_info.get("company"),
            "expected_recruiter": job_info.get("recruiter"),
            "current_url": current_url,
            "screenshot_path": screenshot_path,
        }, ensure_ascii=False)
        
        record_greeting(
            job_url=job_url,
            job_title=job_info.get("jobTitle"),
            company=job_info.get("company"),
            salary=job_info.get("salary"),
            location=job_info.get("location"),
            recruiter=job_info.get("recruiter"),
            message=message,
            status="failed",
            error=enhanced_error,
            screenshot_path=screenshot_path,
            semantic_fp=sem_fp,
            intent_id=idem_id,
        )
        
        # Also return enhanced error in result
        return {
            "status": "failed",
            "jobUrl": job_url,
            "jobTitle": job_info.get("jobTitle"),
            "company": job_info.get("company"),
            "error": error_str,
            "expected_title": job_info.get("jobTitle"),
            "expected_company": job_info.get("company"),
            "current_url": current_url,
            "screenshotPath": screenshot_path,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Send greeting on BOSS直聘")
    ap.add_argument("--job-url", required=True, help="BOSS直聘 job detail URL")
    ap.add_argument("--message", default=None, help="Greeting message text (use --message-file to avoid shell escaping issues)")
    ap.add_argument("--message-file", default=None, help="Path to JSON file containing the message (reads .message field)")
    ap.add_argument("--cdp-port", type=int, default=18801, help="CDP port (default: 18801)")
    ap.add_argument("--screenshot-dir", default=f"{WORKSPACE}/.openclaw-runs/boss-greeting/screenshots")
    ap.add_argument("--match-score", default=None, help="Match score (高/中/低) to store in jd_cache")
    ap.add_argument("--reasoning", default=None, help="Match reasoning to store in jd_cache")
    ap.add_argument("--skip-navigation", action="store_true",
                    help="Skip opening job URL (assumes page already on target)")
    ap.add_argument(
        "--allow-intent-failed-retry",
        action="store_true",
        help="Allow resend attempt when existing intent status is failed",
    )
    ap.add_argument(
        "--no-retry",
        action="store_true",
        help="Disable in-process retries for max efficiency",
    )
    ap.add_argument(
        "--capture-screenshot",
        action="store_true",
        help="Capture screenshots on success/failure (default off)",
    )
    args = ap.parse_args()

    # Read message from file or CLI arg
    message = None
    if args.message_file:
        mf = Path(args.message_file)
        if not mf.exists():
            print(json.dumps({"status": "failed", "error": f"message file not found: {mf}"}, ensure_ascii=False))
            sys.exit(2)
        data = json.loads(mf.read_text(encoding="utf-8"))
        message = data.get("message", "") if isinstance(data, dict) else str(data)
    elif args.message:
        message = args.message
    else:
        print(json.dumps({"status": "failed", "error": "must provide --message or --message-file"}, ensure_ascii=False))
        sys.exit(2)

    message = message.strip()
    if not message:
        print(json.dumps({"status": "failed", "error": "empty_message"}, ensure_ascii=False))
        sys.exit(2)

    # Safety valve: message MUST contain the PS signature.
    # If missing, the orchestrator bypassed jd-greeting-generator and generated its own
    # (usually low-quality, template-like) message. Refuse to send.
    PS_MARKER = "PS：本条消息由我基于 OpenClaw 开发的智能求职 Agent 自动完成"
    if PS_MARKER not in message and "OpenClaw" not in message:
        print(json.dumps({
            "status": "failed",
            "error": "message_missing_ps_signature: 消息缺少 PS 签名，可能未经 jd-greeting-generator 生成",
        }, ensure_ascii=False))
        sys.exit(2)

    init_db()

    # Standalone CLI mode: do browser cleanup once
    if not args.skip_navigation:
        cleanup_browser(args.cdp_port)

    # Update jd_cache with eval results if provided
    if args.match_score or args.reasoning:
        update_jd_cache_eval(
            job_url=args.job_url,
            match_score=args.match_score,
            fit=True,  # only fit=true jobs reach send_greeting
            reasoning=args.reasoning,
        )

    result = send_greeting(
        job_url=args.job_url,
        message=message,
        cdp_port=args.cdp_port,
        screenshot_dir=args.screenshot_dir,
        skip_navigation=args.skip_navigation,
        allow_intent_failed_retry=args.allow_intent_failed_retry,
        no_retry=args.no_retry,
        capture_screenshot=args.capture_screenshot,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] in ("ok", "skipped") else 2)


if __name__ == "__main__":
    main()
