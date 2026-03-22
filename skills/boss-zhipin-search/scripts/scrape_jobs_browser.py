#!/usr/bin/env python3
"""Scrape BOSS jobs via browser automation (raw CDP websocket)."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

BASE_URL = "https://www.zhipin.com/web/geek/jobs"


# ========== BOSS URL code → 人类可读映射 ==========
SALARY_CODE_MAP = {
    "401": "3K以下", "402": "3-5K", "403": "5-10K", "404": "10-15K",
    "405": "15-20K", "406": "20-30K", "407": "30-50K", "408": "50K以上",
}
EXPERIENCE_CODE_MAP = {
    "101": "经验不限", "102": "应届毕业生", "103": "1年以内",
    "104": "1-3年", "105": "3-5年", "106": "5-10年", "107": "10年以上",
}
DEGREE_CODE_MAP = {
    "201": "学历不限", "202": "中专", "203": "大专",
    "204": "本科", "205": "硕士", "206": "博士",
}
JOBTYPE_CODE_MAP = {
    "1901": "全职", "1903": "实习",
}


def _resolve_filter_code(value: str, code_map: dict) -> str:
    """将 BOSS URL code 转换为人类可读格式。支持逗号分隔的多值。"""
    if not value:
        return value
    parts = [p.strip() for p in value.split(",")]
    resolved = [code_map.get(p, p) for p in parts]
    return ",".join(resolved)


# ========== 脚本级兜底过滤辅助函数 ==========

def match_experience(exp_text: str, expected: str) -> bool:
    """
    判断岗位经验是否匹配预期。
    - exp_text: 岗位卡片/详情中提取的经验文本（如 "3-5年"）
    - expected: 用户期望的经验值（如 "1-3年"）
    """
    if not exp_text or not expected:
        return True  # 未提供时默认匹配（避免误杀）
    
    exp_text = normalize_text(exp_text).lower()
    expected = normalize_text(expected).lower()
    
    # 精确匹配
    if exp_text == expected:
        return True
    
    # "经验不限" 匹配任何期望
    if "不限" in exp_text or "应届" in exp_text or "实习" in exp_text:
        return True
    
    # 解析经验范围
    def parse_exp(text: str) -> Tuple[int, int]:
        # 支持 "1-3年", "3-5年", "5-10年", "10年以上", "1年以内"
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*年", text)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r"(\d+)\s*年以上", text)
        if m:
            return (int(m.group(1)), 100)
        m = re.search(r"(\d+)\s*年以内", text)
        if m:
            return (0, int(m.group(1)))
        return (0, 100)  # 默认范围
    
    exp_range = parse_exp(exp_text)
    expected_range = parse_exp(expected)
    
    # 范围重叠即视为匹配
    return exp_range[1] >= expected_range[0] and exp_range[0] <= expected_range[1]


def match_degree(degree_text: str, expected: str) -> bool:
    """
    判断学历是否匹配。
    - degree_text: 岗位要求的学历
    - expected: 期望学历（如 "本科"）
    """
    if not degree_text or not expected:
        return True
    
    degree_text = normalize_text(degree_text).lower()
    expected = normalize_text(expected).lower()
    
    # 精确匹配
    if degree_text == expected:
        return True
    
    # "学历不限" 匹配任何期望
    if "不限" in degree_text or "学历不限" in degree_text:
        return True
    
    # 学历层级（从低到高）
    level_order = ["中专", "高中", "大专", "本科", "硕士", "博士"]
    
    try:
        actual_idx = next(i for i, d in enumerate(level_order) if d in degree_text)
        expect_idx = next(i for i, d in enumerate(level_order) if d in expected)
        # 实际学历 >= 期望学历 即为匹配
        return actual_idx >= expect_idx
    except StopIteration:
        return True  # 无法解析时默认通过


def match_job_type(meta_text: str, tags: List[str], expected: str) -> bool:
    """
    判断求职类型是否匹配。
    - meta_text: 岗位元信息文本
    - tags: 岗位标签列表
    - expected: 期望类型（如 "全职"）
    """
    if not expected:
        return True
    
    expected = normalize_text(expected).lower()
    all_text = normalize_text(meta_text).lower() + " " + " ".join(normalize_text(t) for t in tags)
    
    # 全职/实习/兼职匹配
    if expected in ["全职", "full-time"]:
        return "实习" not in all_text and "兼职" not in all_text
    elif expected in ["实习", "intern"]:
        return "实习" in all_text
    elif expected in ["兼职", "part-time"]:
        return "兼职" in all_text
    
    return True


def salary_in_range(card_salary: str, filter_salary: str) -> bool:
    """
    判断薪资是否在期望范围内。
    - card_salary: 岗位薪资文本（如 "15-30K"）
    - filter_salary: 用户期望薪资（如 "15-25K"）
    """
    if not card_salary or not filter_salary:
        return True  # 未提供时默认通过
    
    card_salary = normalize_text(card_salary).upper().replace(" ", "")
    filter_salary = normalize_text(filter_salary).upper().replace(" ", "")
    
    def parse_salary(text: str) -> Tuple[Optional[int], Optional[int]]:
        # 支持 "15-30K", "15K-30K", "30K以上" 等格式
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*[kK]", text)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r"(\d+)\s*[kK]\s*以上", text)
        if m:
            return (int(m.group(1)), 999)
        # 尝试只匹配一个数字
        m = re.search(r"(\d+)\s*[kK]", text)
        if m:
            return (int(m.group(1)), int(m.group(1)))
        return (None, None)
    
    card_low, card_high = parse_salary(card_salary)
    filter_low, filter_high = parse_salary(filter_salary)
    
    if card_low is None or filter_low is None:
        log("WARN", f"无法解析薪资: card={card_salary}, filter={filter_salary}")
        return True  # 无法解析时记录 warn 但不误杀
    
    # 薪资范围有重叠即为匹配
    return card_high >= filter_low and card_low <= filter_high


CARD_SELECTORS = [
    ".job-list-box .job-card-wrapper",
    ".search-job-result .job-card-box",
    ".search-job-result li",
    ".job-list li",
    ".job-card-box",
    "li[data-job-id]",
]
FILTER_SCOPE_SELECTORS = [
    ".search-condition-wrapper",
    ".condition-box",
    ".job-search-wrapper",
    ".condition-filter-select",
    ".city-area",
    ".filter-wrap",
]
PAGINATION_SCOPE_SELECTORS = [
    ".options-pages",
    ".job-list-pagination",
    ".pagination-area",
    ".page",
]
VERIFY_TEXT_PATTERNS = [
    "安全验证",
    "验证码",
    "请完成验证",
    "行为验证",
    "拖动滑块",
    "人机验证",
    "访问过于频繁",
    "异常访问",
    "验证后继续",
    "登录后继续",
]
# Strict pattern: only allow /job_detail/ URLs (not /gongsi/job/ or company pages)
DETAIL_URL_PATTERN = re.compile(r"/job_detail/.*\.html", re.IGNORECASE)
EXPERIENCE_PATTERN = re.compile(r"(应届|实习|无经验|经验不限|\d+\s*-\s*\d+年|\d+年以内|\d+年以上)")
DEGREE_PATTERN = re.compile(r"(学历不限|不限|中专|高中|大专|本科|硕士|博士)")
def log(level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)


def jitter_sleep(min_ms: int, max_ms: int, reason: str = "") -> float:
    wait_ms = random.randint(min_ms, max_ms)
    if reason:
        log("INFO", f"{reason}，随机等待 {wait_ms}ms")
    time.sleep(wait_ms / 1000.0)
    return wait_ms / 1000.0


def parse_json_loose(text: str) -> Optional[Any]:
    content = (text or "").strip()
    if not content:
        return None
    candidates: List[str] = [content]
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in reversed(lines):
        if line not in candidates:
            candidates.append(line)
    for chunk in candidates:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    match = re.search(r"(\{.*\}|\[.*\])", content, re.S)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def extract_json_value(payload: Any) -> Any:
    """Normalize agent-browser JSON output into a direct value.

    agent-browser typically returns:
    - {success, data: {url: ...}, error}
    - {success, data: {result: <value>}, error}  (for eval)

    We want to consistently extract the inner value.
    """

    if not isinstance(payload, dict):
        return payload

    # Common wrapper: {success:true, data:{result: ...}, error:null}
    data = payload.get("data")
    if isinstance(data, dict) and "result" in data:
        return data.get("result")

    # Older/alternative wrapper: {result: ...}
    if "result" in payload:
        result = payload["result"]
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    if "value" in payload and len(payload) <= 3:
        return payload["value"]

    if "data" in payload and len(payload) <= 3:
        return payload["data"]

    return payload


def prepare_agent_browser_env() -> Dict[str, str]:
    """Legacy helper – kept for compatibility but no longer needed for CDP."""
    return os.environ.copy()


class Browser:
    """Browser automation via raw CDP websocket (browser-level connection).

    Connects to Chrome through the *browser* websocket and uses
    ``Target.attachToTarget`` to get a session for the first page.
    This avoids Playwright / agent-browser lifecycle issues that close
    the page on disconnect.
    """

    def __init__(self, cdp_port: int, headed: bool = True, env: Dict[str, str] | None = None) -> None:
        self.cdp_port = cdp_port
        self.headed = headed
        self._msg_id = 0
        self._ws = None  # type: Any
        self._session_id = None  # type: Optional[str]
        self._connect()

    def _connect(self) -> None:
        import websocket as _ws_mod
        from urllib.request import urlopen as _urlopen

        # Get browser-level websocket URL
        version = json.loads(_urlopen(
            f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=5
        ).read())
        browser_ws = version["webSocketDebuggerUrl"]

        # Find first page target
        tabs = json.loads(_urlopen(
            f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=5
        ).read())
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            raise RuntimeError("Chrome CDP 没有可用的 page tab")
        target_id = page_tabs[0]["id"]

        # Connect to browser websocket
        self._ws = _ws_mod.create_connection(browser_ws, timeout=30)

        # Attach to page target via session (does NOT close page on disconnect)
        result = self._cdp_raw("Target.attachToTarget", {
            "targetId": target_id, "flatten": True,
        })
        self._session_id = result.get("sessionId")
        if not self._session_id:
            raise RuntimeError("无法 attach 到页面 target")
        log("INFO", f"已通过 CDP session 连接到 tab (target={target_id[:12]}...)")

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── low-level CDP messaging ──

    def _cdp_raw(self, method: str, params: Optional[Dict] = None) -> Dict:
        """Send CDP command on browser-level (no session)."""
        self._msg_id += 1
        msg: Dict[str, Any] = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error [{method}]: {resp['error']}")
                return resp.get("result", {})

    def _cdp(self, method: str, params: Optional[Dict] = None) -> Dict:
        """Send CDP command on page session."""
        self._msg_id += 1
        msg: Dict[str, Any] = {
            "id": self._msg_id,
            "method": method,
            "sessionId": self._session_id,
        }
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error [{method}]: {resp['error']}")
                return resp.get("result", {})

    def _js(self, expression: str) -> Any:
        """Evaluate JS and return the value (or None on error)."""
        r = self._cdp("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        })
        result = r.get("result", {})
        if result.get("type") == "undefined":
            return None
        return result.get("value")

    # ── public interface (compatible with old Browser API) ──

    def eval(self, js_code: str, *, allow_fail: bool = False, timeout_sec: int = 120) -> Tuple[bool, Any]:
        """Evaluate JS in page context. Returns (ok, value)."""
        try:
            value = self._js(js_code)
            return True, value
        except Exception as e:
            if not allow_fail:
                log("WARN", f"eval 失败: {e}")
            return False, None

    def run(
        self,
        cmd: List[str],
        *,
        json_output: bool = False,
        allow_fail: bool = False,
        timeout_sec: int = 120,
    ) -> Tuple[int, str, str]:
        """Execute a browser command. Supports: open, fill, press, click, wait, tab, eval."""
        try:
            action = cmd[0] if cmd else ""

            if action == "open":
                url = cmd[1] if len(cmd) > 1 else ""
                self._do_open(url)
                return 0, "", ""

            elif action == "fill":
                selector = cmd[1] if len(cmd) > 1 else ""
                value = cmd[2] if len(cmd) > 2 else ""
                self._do_fill(selector, value)
                return 0, "", ""

            elif action == "press":
                key = cmd[1] if len(cmd) > 1 else "Enter"
                self._do_press(key)
                return 0, "", ""

            elif action == "click":
                selector = cmd[1] if len(cmd) > 1 else ""
                self._do_click(selector)
                return 0, "", ""

            elif action == "wait":
                ms = int(cmd[1]) if len(cmd) > 1 else 1000
                time.sleep(ms / 1000.0)
                return 0, "", ""

            elif action == "tab":
                # No-op: we're already attached to the page
                return 0, "", ""

            elif action == "eval":
                js_code = cmd[1] if len(cmd) > 1 else ""
                value = self._js(js_code)
                return 0, json.dumps(value, ensure_ascii=False) if value is not None else "", ""

            else:
                if not allow_fail:
                    log("WARN", f"未知的 browser 命令: {action}")
                return 1, "", f"unknown command: {action}"

        except Exception as e:
            if not allow_fail:
                log("WARN", f"browser 命令失败 [{' '.join(cmd)}]: {e}")
            return 1, "", str(e)

    # ── command implementations ──

    def _do_open(self, url: str) -> None:
        """Navigate via JS (avoids CDP Page.navigate which can trigger anti-bot)."""
        self._js(f'window.location.href = "{url}"')
        # Wait for navigation to start
        time.sleep(0.5)
        # Poll until page is loaded (up to 30s)
        for _ in range(60):
            try:
                state = self._js("document.readyState")
                if state in ("complete", "interactive"):
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _do_fill(self, selector: str, value: str) -> None:
        """Fill input using CDP Input.insertText for isTrusted=true events."""
        # Focus the element
        escaped_sel = selector.replace("'", "\\'")
        self._js(f"""
            (function() {{
                var el = document.querySelector('{escaped_sel}');
                if (!el) throw new Error('Element not found: {escaped_sel}');
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
            }})()
        """)
        time.sleep(0.1)
        # Use Input.insertText for trusted input events
        self._cdp("Input.insertText", {"text": value})

    def _do_press(self, key: str) -> None:
        """Press a key using CDP Input.dispatchKeyEvent (isTrusted=true)."""
        key_map = {
            "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
            "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9, "nativeVirtualKeyCode": 9},
            "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
        }
        kp = key_map.get(key, {"key": key, "code": key, "windowsVirtualKeyCode": 0, "nativeVirtualKeyCode": 0})
        self._cdp("Input.dispatchKeyEvent", {"type": "keyDown", **kp})
        self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", **kp})

    def _do_click(self, selector: str) -> None:
        """Click an element via JS."""
        escaped_sel = selector.replace("'", "\\'")
        self._js(f"""
            (function() {{
                var el = document.querySelector('{escaped_sel}');
                if (el) el.click();
            }})()
        """)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_link(value: str) -> str:
    link = normalize_text(value)
    if not link:
        return ""
    try:
        parts = urlsplit(link)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return link


def unique_list(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        item = normalize_text(v)
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def to_int(value: Any, default: int) -> int:
    try:
        iv = int(value)
        return iv if iv > 0 else default
    except Exception:
        return default


def parse_salary_candidates(salary: str) -> List[str]:
    salary = normalize_text(salary).upper().replace(" ", "")
    m = re.fullmatch(r"(\d{1,3})-(\d{1,3})K", salary)
    if not m:
        return [salary] if salary else []
    low = int(m.group(1))
    high = int(m.group(2))
    buckets = [
        ("2K以下", 0, 2),
        ("2-5K", 2, 5),
        ("5-10K", 5, 10),
        ("10-15K", 10, 15),
        ("15-25K", 15, 25),
        ("25-50K", 25, 50),
        ("50K以上", 50, 999),
    ]
    overlaps: List[Tuple[int, str]] = []
    for label, l, r in buckets:
        overlap = max(0, min(high, r) - max(low, l))
        if overlap > 0 or (label == "50K以上" and high >= 50):
            overlaps.append((overlap, label))
    overlaps.sort(reverse=True, key=lambda x: x[0])
    ranked = [label for _, label in overlaps]
    return unique_list([f"{low}-{high}K"] + ranked)


def assumptions_for_filters(filters: Dict[str, Any], min_count: int, max_jobs: int, page_limit: int) -> List[str]:
    salary = _resolve_filter_code(normalize_text(filters.get("salary", "")), SALARY_CODE_MAP)
    return [
        "仅通过浏览器渲染后 DOM 抽取数据，不调用 BOSS 接口。",
        f"固定 URL 搜索，所有筛选参数已在 URL 中（salary={salary}），不做 UI 点击筛选。",
        "批量从 DOM 提取岗位卡片数据，不逐条点击。PUA 反爬字体已解码。",
        f"默认停止条件: 抓到 max_jobs({max_jobs}) 或达到 page_limit({page_limit})；min_count={min_count} 仅用于达标判定。",
    ]


def js_click_text(candidates: Sequence[str], exact: bool, scopes: Sequence[str]) -> str:
    return f"""
(() => {{
  const words = {json.dumps(list(candidates), ensure_ascii=False)};
  const exact = {str(bool(exact)).lower()};
  const scopes = {json.dumps(list(scopes), ensure_ascii=False)};
  const clean = (s) => (s || "").replace(/\\s+/g, "").trim();
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || style.pointerEvents === "none") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }};
  const roots = [];
  for (const s of scopes) {{
    for (const el of document.querySelectorAll(s)) {{
      roots.push(el);
    }}
  }}
  if (!roots.length) roots.push(document);
  const selectors = "button,a,label,li,span,div,p,[role='button']";
  for (const root of roots) {{
    const nodes = Array.from(root.querySelectorAll(selectors));
    for (const word of words) {{
      const w = clean(word);
      if (!w) continue;
      for (const node of nodes) {{
        if (!visible(node)) continue;
        const txt = clean(node.textContent || node.getAttribute("aria-label") || "");
        if (!txt) continue;
        const matched = exact ? txt === w : (txt === w || txt.includes(w));
        if (!matched) continue;
        node.scrollIntoView({{behavior: "instant", block: "center"}});
        node.click();
        return {{clicked: true, word, matched: txt, tag: node.tagName}};
      }}
    }}
  }}
  return {{clicked: false}};
}})()
""".strip()


def click_text(
    browser: Browser,
    candidates: Sequence[str],
    *,
    exact: bool = False,
    scopes: Sequence[str] = (),
) -> bool:
    items = unique_list(candidates)
    if not items:
        return False
    ok, result = browser.eval(js_click_text(items, exact=exact, scopes=scopes), allow_fail=True)
    if not ok or not isinstance(result, dict):
        return False
    return bool(result.get("clicked"))


def detect_verify(browser: Browser) -> Tuple[bool, Dict[str, Any]]:
    script = f"""
(() => {{
  const txt = (document.body ? document.body.innerText : "").slice(0, 5000);
  const url = location.href || "";
  const title = document.title || "";
  const textHit = {json.dumps(VERIFY_TEXT_PATTERNS, ensure_ascii=False)}.some(k => txt.includes(k));
  const iframeHit = Array.from(document.querySelectorAll("iframe")).some((f) => /captcha|verify|geetest|challenge|security/i.test((f.src || "") + " " + (f.id || "") + " " + (f.className || "")));
  const boxHit = !!document.querySelector("[class*='verify'],[id*='verify'],[class*='captcha'],[id*='captcha']");
  return {{url, title, textHit, iframeHit, boxHit, snippet: txt.slice(0, 240)}};
}})()
""".strip()
    ok, result = browser.eval(script, allow_fail=True)
    if not ok or not isinstance(result, dict):
        return False, {}
    url = normalize_text(result.get("url", "")).lower()
    url_hit = any(flag in url for flag in ("captcha", "verify", "challenge", "security"))
    matched = bool(url_hit or result.get("textHit") or result.get("iframeHit") or result.get("boxHit"))
    return matched, result


def wait_for_verify_clear(
    browser: Browser,
    timeout_sec: int,
    check_min_sec: int,
    check_max_sec: int,
) -> bool:
    matched, detail = detect_verify(browser)
    if not matched:
        return True
    log("WARN", "检测到验证码/安全验证。请在弹出的浏览器窗口中手动完成验证/登录，完成后脚本会自动继续。")
    if detail:
        log("WARN", f"验证线索: url={detail.get('url', '')} title={detail.get('title', '')}")
    started = time.time()
    while time.time() - started < timeout_sec:
        jitter_sleep(check_min_sec * 1000, check_max_sec * 1000, reason="等待手动验证")
        matched, _ = detect_verify(browser)
        if not matched:
            log("INFO", "检测到验证已解除，继续执行。")
            return True
    log("ERROR", f"等待手动验证超时({timeout_sec}s)，将优雅退出并保留已抓取数据。")
    return False


def get_current_url(browser: Browser) -> str:
    ok, result = browser.eval("(() => location.href || '')()", allow_fail=True)
    if ok:
        return normalize_text(result)
    return ""


def maybe_wait_after_navigation(browser: Browser, min_ms: int = 2600, max_ms: int = 4800) -> None:
    jitter_sleep(min_ms, max_ms, reason="等待页面稳定")
    browser.run(["wait", "1600"], allow_fail=True)


def fill_keyword(browser: Browser, keyword: str) -> bool:
    selectors = [
        "input[name='query']",
        "input[placeholder*='搜索']",
        "input[placeholder*='职位']",
        "input[placeholder*='关键字']",
        "input[type='search']",
        "input[type='text']",
    ]
    for sel in selectors:
        code, _, _ = browser.run(["fill", sel, keyword], allow_fail=True)
        if code == 0:
            return True
    script = f"""
(() => {{
  const kw = {json.dumps(keyword, ensure_ascii=False)};
  const inputs = Array.from(document.querySelectorAll("input[type='text'],input[type='search']"));
  const visible = inputs.find((x) => {{
    const r = x.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }});
  if (!visible) return {{ok: false}};
  visible.focus();
  visible.value = kw;
  visible.dispatchEvent(new Event("input", {{bubbles: true}}));
  visible.dispatchEvent(new Event("change", {{bubbles: true}}));
  return {{ok: true}};
}})()
""".strip()
    ok, result = browser.eval(script, allow_fail=True)
    return bool(ok and isinstance(result, dict) and result.get("ok"))


def trigger_search(browser: Browser) -> bool:
    search_button_selectors = [
        "button[type='submit']",
        ".search-btn",
        ".btn-search",
        ".search-button",
    ]
    for sel in search_button_selectors:
        code, _, _ = browser.run(["click", sel], allow_fail=True)
        if code == 0:
            return True
    code, _, _ = browser.run(["press", "Enter"], allow_fail=True)
    return code == 0


def apply_one_filter(browser: Browser, label: str, panel_texts: Sequence[str], value_texts: Sequence[str]) -> None:
    values = unique_list(value_texts)
    if not values:
        return
    log("INFO", f"尝试设置筛选项[{label}] -> {values[0]}")
    _ = click_text(browser, panel_texts, exact=False, scopes=FILTER_SCOPE_SELECTORS)
    jitter_sleep(1600, 2800)
    clicked = click_text(browser, values, exact=False, scopes=FILTER_SCOPE_SELECTORS)
    if not clicked:
        clicked = click_text(browser, values, exact=False, scopes=())
    if not clicked:
        log("WARN", f"筛选[{label}]未命中可点击项，已降级跳过。")
        return
    maybe_wait_after_navigation(browser, 1800, 3200)


def apply_filters(browser: Browser, filters: Dict[str, Any]) -> None:
    city = normalize_text(filters.get("city"))
    salary = normalize_text(filters.get("salary"))
    experience = normalize_text(filters.get("experience"))
    degree = normalize_text(filters.get("degree"))
    job_type = normalize_text(filters.get("jobType"))

    if city:
        apply_one_filter(browser, "city", ["城市", "地区", "全国"], [city])
    if salary:
        apply_one_filter(browser, "salary", ["薪资", "月薪", "薪酬"], parse_salary_candidates(salary))
    if experience:
        apply_one_filter(browser, "experience", ["经验", "工作经验"], [experience])
    if degree:
        apply_one_filter(browser, "degree", ["学历"], [degree])
    if job_type:
        apply_one_filter(browser, "jobType", ["类型", "职位类型", "全职"], [job_type])


def js_get_cards(limit: int) -> str:
    return f"""
(() => {{
  const selectors = {json.dumps(CARD_SELECTORS, ensure_ascii=False)};
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();

  // ── BOSS 反爬字体解码 ──
  // BOSS 使用 kanzhun-mix 自定义字体，数字用 PUA Unicode 字符渲染。
  // 用 canvas 比对 PUA 字符与 0-9 数字的渲染像素来建立映射。
  var puaMap = {{}};
  try {{
    var cv = document.createElement("canvas");
    cv.width = 30; cv.height = 30;
    var cx = cv.getContext("2d");
    cx.font = "20px kanzhun-mix, kanzhun-Regular";
    cx.textBaseline = "middle";
    function renderPx(ch) {{
      cx.clearRect(0, 0, 30, 30);
      cx.fillText(ch, 2, 15);
      var d = cx.getImageData(0, 0, 30, 30).data;
      var px = [];
      for (var i = 3; i < d.length; i += 4) {{ if (d[i] > 10) px.push(i); }}
      return px;
    }}
    function sim(a, b) {{
      var sa = new Set(a), sb = new Set(b), inter = 0;
      sa.forEach(function(v) {{ if (sb.has(v)) inter++; }});
      var u = sa.size + sb.size - inter;
      return u > 0 ? inter / u : 0;
    }}
    var digitPx = {{}};
    for (var d = 0; d <= 9; d++) digitPx[d] = renderPx(String(d));
    // Scan PUA range used by BOSS (0xE030-0xE039 typically)
    for (var cp = 0xE020; cp <= 0xE050; cp++) {{
      var px = renderPx(String.fromCharCode(cp));
      if (px.length < 3) continue;  // empty glyph
      var best = -1, bestS = 0;
      for (var dd = 0; dd <= 9; dd++) {{
        var s = sim(px, digitPx[dd]);
        if (s > bestS) {{ bestS = s; best = dd; }}
      }}
      if (bestS > 0.5) puaMap[cp] = best;
    }}
  }} catch(e) {{}}

  function decodePUA(text) {{
    var out = "";
    for (var i = 0; i < text.length; i++) {{
      var c = text.charCodeAt(i);
      if (puaMap[c] !== undefined) out += String(puaMap[c]);
      else out += text[i];
    }}
    return out;
  }}

  // ── 卡片提取 ──
  let cards = [];
  for (const sel of selectors) {{
    const nodes = Array.from(document.querySelectorAll(sel));
    if (nodes.length) {{ cards = nodes; break; }}
  }}
  if (!cards.length) {{
    const anchors = Array.from(document.querySelectorAll("a[href*='job_detail'],a[href*='/job/']"));
    cards = anchors.map(a => a.closest("li,div,article") || a);
  }}
  const uniq = [];
  const seen = new Set();
  for (const node of cards) {{
    if (!node || seen.has(node)) continue;
    seen.add(node); uniq.push(node);
  }}

  const out = uniq.slice(0, {int(limit)}).map((card, index) => {{
    const titleEl = card.querySelector(".job-name") || card.querySelector(".job-title,h3,h4,[class*='job-name'],[class*='title']");
    const companyEl = card.querySelector(".boss-name,.company-name") || card.querySelector("[class*='company-name']");
    const salaryEl = card.querySelector(".job-salary") || card.querySelector(".salary,[class*='salary']");
    const linkEl = card.querySelector("a[href*='job_detail'],a[href*='/job/'],a[href]");

    // 提取 tag-list 的每个 li 单独作为 token（避免拼接）
    const tagLis = Array.from(card.querySelectorAll(".tag-list li"));
    const metaTokens = tagLis.map(li => clean(li.textContent));

    // 位置信息从 .company-location 提取
    const locEl = card.querySelector(".company-location");
    const location = clean(locEl ? locEl.textContent : "");
    if (location) metaTokens.push(location);

    let titleText = clean(titleEl ? titleEl.textContent : "");

    // 解码薪资 PUA 字体
    let salaryRaw = salaryEl ? salaryEl.textContent : "";
    let salaryText = clean(decodePUA(salaryRaw));

    // 从 title 中去掉薪资文本
    if (salaryText && titleText.includes(salaryText)) {{
      titleText = clean(titleText.replace(salaryText, ""));
    }}
    // 同时清理 PUA 残留（title 也可能包含 PUA 字符）
    titleText = clean(decodePUA(titleText));
    titleText = titleText.replace(/\\s*\\d{{1,3}}-\\d{{1,3}}K(·\\d+薪)?\\s*$/, "").trim();

    return {{
      index,
      title: titleText,
      company: clean(companyEl ? companyEl.textContent : ""),
      salary: salaryText,
      meta: metaTokens.join("|"),
      link: linkEl ? linkEl.href : ""
    }};
  }});
  return {{count: out.length, cards: out}};
}})()
""".strip()


def get_cards(browser: Browser, limit: int = 120) -> List[Dict[str, Any]]:
    ok, result = browser.eval(js_get_cards(limit), allow_fail=True)
    if not ok or not isinstance(result, dict):
        return []
    cards = result.get("cards")
    if not isinstance(cards, list):
        return []
    out: List[Dict[str, Any]] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "index": int(c.get("index", 0)),
                "title": normalize_text(c.get("title", "")),
                "company": normalize_text(c.get("company", "")),
                "salary": normalize_text(c.get("salary", "")),
                "meta": normalize_text(c.get("meta", "")),
                "link": normalize_link(str(c.get("link", ""))),
            }
        )
    return out


def click_card_by_index(browser: Browser, index: int) -> bool:
    script = f"""
((targetIndex) => {{
  const selectors = {json.dumps(CARD_SELECTORS, ensure_ascii=False)};
  let cards = [];
  for (const sel of selectors) {{
    const nodes = Array.from(document.querySelectorAll(sel));
    if (nodes.length) {{
      cards = nodes;
      break;
    }}
  }}
  if (!cards.length) {{
    const anchors = Array.from(document.querySelectorAll("a[href*='job_detail'],a[href*='/job/']"));
    cards = anchors.map(a => a.closest("li,div,article") || a);
  }}
  if (targetIndex < 0 || targetIndex >= cards.length) return {{clicked: false, reason: "index_oob", count: cards.length}};
  const card = cards[targetIndex];
  card.scrollIntoView({{behavior: "instant", block: "center"}});
  const target = card.querySelector("a[href*='job_detail'],a[href*='/job/'],a[href]") || card;
  target.click();
  return {{clicked: true, count: cards.length}};
}})({int(index)})
""".strip()
    ok, result = browser.eval(script, allow_fail=True)
    return bool(ok and isinstance(result, dict) and result.get("clicked"))


def scroll_for_more(browser: Browser) -> None:
    script = """
(() => {
  const selectors = [".job-list-box", ".search-job-result", ".job-list", ".job-list-container"];
  for (const sel of selectors) {
    const node = document.querySelector(sel);
    if (!node) continue;
    const style = window.getComputedStyle(node);
    const canScroll = /(auto|scroll)/.test(style.overflowY || "") || node.scrollHeight > node.clientHeight + 8;
    if (canScroll) {
      node.scrollTop += Math.max(600, Math.floor(node.clientHeight * 0.85));
      return {ok: true, target: sel, mode: "container"};
    }
  }
  window.scrollBy({top: Math.max(700, Math.floor(window.innerHeight * 0.85)), behavior: "instant"});
  return {ok: true, target: "window", mode: "window"};
})()
""".strip()
    _ = browser.eval(script, allow_fail=True)
    jitter_sleep(1400, 3000, reason="滚动列表触发更多岗位")
    browser.run(["wait", "1000"], allow_fail=True)


def parse_meta_tokens(values: Iterable[str]) -> List[str]:
    tokens: List[str] = []
    for value in values:
        text = normalize_text(value)
        if not text:
            continue
        for part in re.split(r"[|/·•\n]", text):
            item = normalize_text(part)
            if item:
                tokens.append(item)
    return unique_list(tokens)


def parse_city_experience_degree(tokens: Iterable[str], preferred_city: str = "") -> Tuple[str, str, str]:
    city = normalize_text(preferred_city)
    exp = ""
    degree = ""
    city_known = [
        "北京",
        "上海",
        "广州",
        "深圳",
        "杭州",
        "成都",
        "武汉",
        "西安",
        "南京",
        "苏州",
        "天津",
        "重庆",
        "长沙",
        "郑州",
        "青岛",
    ]

    for tk in tokens:
        t = normalize_text(tk)
        if not t:
            continue
        if not exp:
            m = EXPERIENCE_PATTERN.search(t)
            if m:
                exp = m.group(0)
        if not degree:
            m = DEGREE_PATTERN.search(t)
            if m:
                degree = m.group(0)
        if not city:
            if any(name in t for name in city_known):
                city = t
            elif re.search(r"(市|区|县)$", t) and not re.search(r"\d", t):
                city = t
    return city, exp, degree


def js_extract_detail() -> str:
    return """
(() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const firstText = (selectors) => {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (!el) continue;
      const t = clean(el.textContent);
      if (t) return t;
    }
    return "";
  };
  const manyText = (selectors, limit = 20) => {
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

  const title = firstText([
    ".job-detail-header .name",
    ".job-title",
    "h1",
    ".name",
  ]);
  const company = firstText([
    ".job-detail-header .company-name",
    ".company-name a",
    ".company-info h2",
    ".job-card-wrapper.active .company-name",
  ]);
  const salary = firstText([
    ".job-detail-header .salary",
    ".salary",
    ".job-salary",
  ]);
  const meta = firstText([
    ".job-detail-header .text-desc",
    ".job-primary .tag-list",
    ".job-limit",
    ".job-card-wrapper.active .job-area",
  ]);
  const recruiter = firstText([
    ".boss-info-attr .name",
    ".job-boss-info .name",
    ".boss-card .name",
    "[class*='recruiter'] .name",
  ]);
  const recruiterTitle = firstText([
    ".boss-info-attr .title",
    ".job-boss-info .title",
    ".boss-card .desc",
    "[class*='recruiter'] .title",
  ]);

  const tags = manyText([
    ".job-detail .job-tags span",
    ".job-detail .job-labels span",
    ".job-card-wrapper.active .tag-list li",
    ".job-card-wrapper.active .tag-list span",
    ".job-sec .tag-list li",
  ], 24);
  const benefits = manyText([
    ".job-detail .job-bene-tag span",
    ".job-detail .job-benefit-tag span",
    ".welfare-list li",
    ".job-sec .welfare-list span",
  ], 24);
  const infoTokens = manyText([
    ".job-detail-header .tag-list li",
    ".job-detail-header .tag-list span",
    ".job-card-wrapper.active .tag-list li",
    ".job-card-wrapper.active .tag-list span",
  ], 16);
  let link = location.href || "";
  if (!/job_detail|\/job\//i.test(link)) {
    const active = document.querySelector(".job-card-wrapper.active,.job-card-box.active,.active-job");
    const anchor = active ? active.querySelector("a[href*='job_detail'],a[href*='/job/'],a[href]") : null;
    if (anchor && anchor.href) link = anchor.href;
  }
  return {
    title,
    company,
    salary,
    meta,
    tags,
    benefits,
    infoTokens,
    recruiter,
    recruiterTitle,
    link
  };
})()
""".strip()


def extract_detail(browser: Browser) -> Dict[str, Any]:
    ok, result = browser.eval(js_extract_detail(), allow_fail=True)
    if not ok or not isinstance(result, dict):
        return {}
    return {
        "title": normalize_text(result.get("title", "")),
        "company": normalize_text(result.get("company", "")),
        "salary": normalize_text(result.get("salary", "")),
        "meta": normalize_text(result.get("meta", "")),
        "tags": unique_list(result.get("tags", []) if isinstance(result.get("tags"), list) else []),
        "benefits": unique_list(result.get("benefits", []) if isinstance(result.get("benefits"), list) else []),
        "infoTokens": unique_list(result.get("infoTokens", []) if isinstance(result.get("infoTokens"), list) else []),
        "recruiter": normalize_text(result.get("recruiter", "")),
        "recruiterTitle": normalize_text(result.get("recruiterTitle", "")),
        "link": normalize_link(str(result.get("link", ""))),
    }


def canonical_key(job: Dict[str, Any]) -> str:
    link = normalize_link(job.get("link", ""))
    # Only use link as key if it's a real job detail URL (not a search page URL)
    if link and DETAIL_URL_PATTERN.search(link):
        return f"link::{link}"
    return "basic::" + "||".join(
        [
            normalize_text(job.get("title", "")).lower(),
            normalize_text(job.get("company", "")).lower(),
            normalize_text(job.get("salary", "")).lower(),
        ]
    )


def merge_job(card: Dict[str, Any], detail: Dict[str, Any], fallback_city: str) -> Dict[str, Any]:
    title = normalize_text(detail.get("title") or card.get("title"))
    company = normalize_text(detail.get("company") or card.get("company"))
    salary = normalize_text(detail.get("salary") or card.get("salary"))
    # Prefer card link (extracted from <a href="...job_detail...">) over detail link.
    # In BOSS's split-panel UI, detail link is often the search page URL, not the job URL.
    card_link = normalize_link(card.get("link") or "")
    detail_link = normalize_link(detail.get("link") or "")
    link = card_link if DETAIL_URL_PATTERN.search(card_link) else (detail_link or card_link)

    tags = unique_list(detail.get("tags", []))
    benefits = unique_list(detail.get("benefits", []))
    tokens = parse_meta_tokens(
        [detail.get("meta", ""), card.get("meta", "")]
        + (detail.get("infoTokens", []) if isinstance(detail.get("infoTokens"), list) else [])
        + tags
    )
    city, experience, degree = parse_city_experience_degree(tokens, preferred_city=fallback_city)

    return {
        "title": title,
        "company": company,
        "salary": salary,
        "city": city or fallback_city,
        "experience": experience,
        "degree": degree,
        "tags": tags,
        "benefits": benefits,
        "recruiter": normalize_text(detail.get("recruiter", "")),
        "recruiterTitle": normalize_text(detail.get("recruiterTitle", "")),
        "link": link,
        "source": "BOSS直聘",
    }


def should_add_job(job: Dict[str, Any], seen: set) -> bool:
    # Strict filter: reject jobs without valid job_detail links
    link = normalize_link(job.get("link", ""))
    if link and not DETAIL_URL_PATTERN.search(link):
        return False
    if not normalize_text(job.get("title")) and not normalize_text(job.get("link")):
        return False
    key = canonical_key(job)
    if key in seen:
        return False
    seen.add(key)
    return True


CITY_CODES: Dict[str, str] = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "成都": "101270100", "武汉": "101200100", "西安": "101110100",
    "南京": "101190100", "苏州": "101190400", "天津": "101030100", "重庆": "101040100",
    "长沙": "101250100", "郑州": "101180100", "青岛": "101120200", "东莞": "101281600",
    "佛山": "101280800", "合肥": "101220100", "厦门": "101230200", "福州": "101230100",
    "济南": "101120100", "昆明": "101290100", "大连": "101070200", "哈尔滨": "101050100",
    "沈阳": "101070100", "珠海": "101280700", "全国": "100010000",
}


# ── 固定搜索 URL 前缀（只有 query= 后的关键词变化）──
FIXED_SEARCH_PREFIX = (
    "https://www.zhipin.com/web/geek/jobs"
    "?city=101280600"
    "&jobType=1901"
    "&salary=405"
    "&experience=104"
    "&degree=203"
    "&scale=303,304,305,306"
)


def build_search_url(keyword: str, filters: Dict[str, Any]) -> str:
    """Build a BOSS直聘 search URL.

    URL 前缀是固定的，只有 query=<keyword> 会变。
    filters 参数保留但不再用于构建 URL（仅用于日志/兼容）。
    """
    from urllib.parse import quote
    return f"{FIXED_SEARCH_PREFIX}&query={quote(keyword)}"


def build_page_url(base_search_url: str, page: int) -> str:
    """Build search URL for a given page number using &page=N parameter."""
    from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

    parts = urlsplit(base_search_url)
    params = parse_qs(parts.query, keep_blank_values=True)
    # Remove existing page param, then set the new one
    params.pop("page", None)
    if page > 1:
        params["page"] = [str(page)]
    new_query = urlencode(params, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))


def _ensure_search_keyword(browser: Browser, keyword: str, full_url: str = "") -> bool:
    """Verify the BOSS SPA actually searched for the keyword.

    BOSS SPA often strips URL query params on initial load, showing default
    recommendations instead of search results. This function checks the search
    input value and manually triggers a search if the keyword is missing.

    Uses Playwright fill + press (trusted events) instead of JS synthetic events,
    because BOSS ignores untrusted (isTrusted=false) keyboard events.

    Returns True if keyword search is confirmed active.
    """
    check_js = """(() => {
  const input = document.querySelector("input.ipt-search, input[name='query'], input[ka='header-home-search']");
  return input ? input.value : "";
})()"""
    ok, current_val = browser.eval(check_js, allow_fail=True)
    current_val = normalize_text(current_val) if ok else ""
    current_val = current_val.strip('"').strip("'")

    if keyword.lower() in current_val.lower():
        log("INFO", f"搜索关键词确认: 输入框={current_val}")
        return True

    log("WARN", f"BOSS SPA 未加载搜索关键词: 输入框='{current_val}'，期望包含='{keyword}'")
    log("INFO", "尝试通过 Playwright fill+press 填入关键词并触发搜索...")

    # Try multiple selectors for the BOSS search input
    selectors = [
        "input[placeholder*='搜索']",
        "input.ipt-search",
        "input.input",
        "input[ka='header-home-search']",
        "input[name='query']",
    ]

    filled = False
    for sel in selectors:
        # Use Playwright fill (trusted event, triggers Vue reactivity)
        code, stdout, stderr = browser.run(
            ["fill", sel, keyword], allow_fail=True, timeout_sec=10,
        )
        if code == 0:
            log("INFO", f"Playwright fill 成功: selector={sel}, keyword={keyword}")
            filled = True
            break
        log("DEBUG", f"fill selector '{sel}' 失败，尝试下一个")

    if not filled:
        log("WARN", "所有 fill selector 均失败，无法填入关键词")
        return False

    # Use Playwright press Enter (trusted event) to trigger search
    code, _, _ = browser.run(["press", "Enter"], allow_fail=True, timeout_sec=10)
    if code != 0:
        log("WARN", "Playwright press Enter 失败")
        return False

    log("INFO", "已按下 Enter 触发搜索，等待页面加载...")
    maybe_wait_after_navigation(browser, 5000, 7000)

    # Verify the URL now contains the keyword (more reliable than checking input value)
    ok_url, cur_url = browser.eval("location.href", allow_fail=True)
    cur_url = normalize_text(cur_url) if ok_url else ""
    cur_url = cur_url.strip('"').strip("'")
    if "query=" in cur_url:
        log("INFO", f"搜索已触发，URL 包含 query 参数: {cur_url}")
        return True

    # Fallback: check input value
    ok2, new_val = browser.eval(check_js, allow_fail=True)
    new_val = normalize_text(new_val) if ok2 else ""
    new_val = new_val.strip('"').strip("'")
    if keyword.lower() in new_val.lower():
        log("INFO", f"搜索已触发: 输入框={new_val}")
        return True

    log("WARN", f"搜索触发后未确认关键词: URL={cur_url}, 输入框='{new_val}'")
    return False


def _check_search_redirect(browser: Browser, expected_path: str = "/web/geek/jobs") -> bool:
    """Check if browser is still on the expected search page (not redirected to homepage).

    Returns True if still on search page, False if redirected.
    """
    ok, cur_url = browser.eval("location.href", allow_fail=True)
    cur_url = normalize_text(cur_url) if ok else ""
    # Strip quotes from eval result
    cur_url = cur_url.strip('"').strip("'")
    if expected_path in cur_url:
        return True
    log("WARN", f"重定向检测：当前 URL={cur_url}，不包含 {expected_path}")
    return False


def _retry_open_with_redirect_check(
    browser: Browser,
    url: str,
    *,
    max_retries: int = 2,
    expected_path: str = "/web/geek/jobs",
    label: str = "搜索页",
) -> bool:
    """Open a URL, check for redirect, retry with increasing delays if redirected.

    Returns True if successfully landed on the expected page.
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait_sec = 6 + attempt * 4  # 10s, 14s, ...
            log("INFO", f"第 {attempt + 1} 次重试打开{label}，先等待 {wait_sec}s")
            time.sleep(wait_sec)
            code, _, _ = browser.run(["open", url], allow_fail=True, timeout_sec=60)
            if code != 0:
                log("WARN", f"重试打开{label}命令失败")
                continue
        maybe_wait_after_navigation(browser, 2800, 4800)
        if _check_search_redirect(browser, expected_path):
            if attempt > 0:
                log("INFO", f"第 {attempt + 1} 次重试成功，已回到{label}")
            return True
        log("WARN", f"BOSS 反爬重定向：打开{label}后被跳转首页 (attempt {attempt + 1}/{max_retries + 1})")
    return False


def navigate_to_page(browser: Browser, base_search_url: str, page: int) -> bool:
    """Navigate to a specific search results page via URL parameter."""
    url = build_page_url(base_search_url, page)
    log("INFO", f"URL 翻页 → page={page}: {url}")
    code, _, _ = browser.run(["open", url], allow_fail=True, timeout_sec=60)
    if code != 0:
        return False
    return _retry_open_with_redirect_check(
        browser, url, expected_path="/web/geek/jobs", label=f"搜索第{page}页",
    )


def process_current_page(
    browser: Browser,
    jobs: List[Dict[str, Any]],
    seen_keys: set,
    dropped_invalid_links: List[str],
    *,
    max_jobs: int,
    verify_timeout_sec: int,
    verify_check_min_sec: int,
    verify_check_max_sec: int,
    fallback_city: str,
    filters: Dict[str, Any] = None,
    filter_stats: Dict[str, int] = None,
) -> str:
    """Fast extraction: read all card data from the DOM in one shot, no per-card clicking."""

    # Initialize filter stats if not provided
    if filter_stats is None:
        filter_stats = {
            "filteredByExperience": 0,
            "filteredByDegree": 0,
            "filteredByJobType": 0,
            "filteredBySalary": 0,
        }
    
    # Extract filter values — resolve BOSS URL codes to human-readable
    exp_filter = _resolve_filter_code(normalize_text(filters.get("experience")), EXPERIENCE_CODE_MAP) if filters else ""
    degree_filter = _resolve_filter_code(normalize_text(filters.get("degree")), DEGREE_CODE_MAP) if filters else ""
    jobtype_filter = _resolve_filter_code(normalize_text(filters.get("jobType")), JOBTYPE_CODE_MAP) if filters else ""
    salary_filter = _resolve_filter_code(normalize_text(filters.get("salary")), SALARY_CODE_MAP) if filters else ""

    cards = get_cards(browser, limit=200)
    if not cards:
        log("WARN", "当前页未识别到岗位卡片。")
        return "ok"

    log("INFO", f"当前页识别到 {len(cards)} 个岗位卡片，批量提取中...")

    for card in cards:
        if len(jobs) >= max_jobs:
            return "ok"

        title = normalize_text(card.get("title", ""))
        company = normalize_text(card.get("company", ""))
        salary = normalize_text(card.get("salary", ""))
        card_link = normalize_link(card.get("link", ""))
        meta = normalize_text(card.get("meta", ""))

        tokens = parse_meta_tokens([meta])
        city, experience, degree = parse_city_experience_degree(tokens, preferred_city=fallback_city)

        # Link filter: track non-job_detail links as invalid
        if card_link and not DETAIL_URL_PATTERN.search(card_link):
            if len(dropped_invalid_links) < 5:
                dropped_invalid_links.append(card_link)
            log("INFO", f"跳过无效链接: {card_link}")
            continue

        # City filter: skip jobs from other cities (BOSS sometimes mixes in "推荐" from elsewhere)
        if fallback_city and city and fallback_city not in city and city not in fallback_city:
            log("INFO", f"跳过非目标城市: {title} | {city} (目标: {fallback_city})")
            continue

        # ========== 脚本级兜底过滤 ==========
        # 即使网页筛选失败，也在这里做最后的过滤判定
        if exp_filter and not match_experience(experience, exp_filter):
            filter_stats["filteredByExperience"] += 1
            log("INFO", f"跳过（经验不匹配）: {title} | 期望:{exp_filter} 实际:{experience}")
            continue
        
        if degree_filter and not match_degree(degree, degree_filter):
            filter_stats["filteredByDegree"] += 1
            log("INFO", f"跳过（学历不匹配）: {title} | 期望:{degree_filter} 实际:{degree}")
            continue
        
        if jobtype_filter and not match_job_type(meta, [], jobtype_filter):
            filter_stats["filteredByJobType"] += 1
            log("INFO", f"跳过（求职类型不匹配）: {title} | 期望:{jobtype_filter}")
            continue
        
        if salary_filter and not salary_in_range(salary, salary_filter):
            filter_stats["filteredBySalary"] += 1
            log("INFO", f"跳过（薪资不匹配）: {title} | 期望:{salary_filter} 实际:{salary}")
            continue
        # ========== 兜底过滤结束 ==========

        job = {
            "title": title,
            "company": company,
            "salary": salary,
            "city": city or fallback_city,
            "experience": experience,
            "degree": degree,
            "tags": [],
            "benefits": [],
            "recruiter": "",
            "recruiterTitle": "",
            "link": card_link,
            "source": "BOSS直聘",
        }

        if should_add_job(job, seen_keys):
            jobs.append(job)
            log("INFO", f"已抓取 {len(jobs)} 条: {title} | {company} | {salary}")

    return "ok"


# ── 默认搜索关键词（用户未指定岗位时使用）──
DEFAULT_KEYWORDS = ["AI产品经理", "Agent", "AI开发", "大模型应用"]


def ensure_keyword(filters: Dict[str, Any]) -> str:
    keyword = normalize_text(filters.get("keyword"))
    if not keyword:
        # 尝试从 candidate-preferences.json 加载
        prefs_path = Path(__file__).resolve().parents[2] / "candidate-preferences.json"
        if not prefs_path.exists():
            prefs_path = Path.home() / ".openclaw" / "workspace" / "candidate-preferences.json"
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                kw_list = prefs.get("searchKeywords", [])
                if kw_list:
                    keyword = normalize_text(kw_list[0])
                    log("INFO", f"keyword 未指定，使用 candidate-preferences 默认: {keyword}")
            except Exception:
                pass
        if not keyword:
            keyword = DEFAULT_KEYWORDS[0]
            log("INFO", f"keyword 未指定，使用硬编码默认: {keyword}")
    return keyword


def load_filters(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("input JSON must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape BOSS jobs via agent-browser DOM automation")
    parser.add_argument("--input", required=True, help="Path to filters JSON")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--cdp-port", type=int, default=18801, help="CDP port of BOSS-dedicated Chrome (default: 18801)")
    parser.add_argument("--headed", dest="headed", action="store_true", help="Run with visible browser window")
    parser.add_argument("--no-headed", dest="headed", action="store_false", help="Run headless")
    parser.set_defaults(headed=True)
    parser.add_argument("--min-count", type=int, default=None, help="Target minimum job count (fallback to filters.minCount)")
    parser.add_argument("--max-jobs", type=int, default=None, help="Hard cap of jobs to collect")
    parser.add_argument("--page-limit", type=int, default=None, help="Maximum pages to traverse")
    parser.add_argument("--verify-timeout-sec", type=int, default=15 * 60, help="Wait timeout for manual verify/login")
    parser.add_argument("--verify-check-min-sec", type=int, default=2, help="Min polling interval during verify wait")
    parser.add_argument("--verify-check-max-sec", type=int, default=5, help="Max polling interval during verify wait")
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    jobs_path = outdir / "jobs.json"

    if not input_path.exists():
        log("ERROR", f"input 不存在: {input_path}")
        return 2

    try:
        filters = load_filters(input_path)
        keyword = ensure_keyword(filters)
    except Exception as exc:
        log("ERROR", f"读取输入失败: {exc}")
        return 2

    min_count = to_int(args.min_count if args.min_count is not None else filters.get("minCount"), 20)
    if args.max_jobs is None:
        max_jobs = min_count
    else:
        max_jobs = to_int(args.max_jobs, min_count)
        if max_jobs < min_count:
            log("WARN", f"max_jobs({max_jobs}) < min_count({min_count})，将按 max_jobs 停止并在汇总中标记未达标。")
    page_limit = to_int(args.page_limit if args.page_limit is not None else filters.get("pageLimit"), 1)

    assumptions = assumptions_for_filters(filters, min_count=min_count, max_jobs=max_jobs, page_limit=page_limit)
    log("INFO", "抓取 assumptions:")
    for idx, item in enumerate(assumptions, start=1):
        log("INFO", f"{idx}. {item}")

    browser = Browser(cdp_port=args.cdp_port, headed=bool(args.headed))

    jobs: List[Dict[str, Any]] = []
    seen_keys: set = set()
    dropped_invalid_links: List[str] = []  # Track non-job_detail links
    filter_stats: Dict[str, int] = {
        "filteredByExperience": 0,
        "filteredByDegree": 0,
        "filteredByJobType": 0,
        "filteredBySalary": 0,
    }
    status = "ok"
    exit_code = 0

    # Build search URL directly from filters (faster and more reliable than clicking UI filters).
    base_search_url = build_search_url(keyword, filters)
    city_fallback = normalize_text(filters.get("city", ""))
    log("INFO", f"搜索 URL: {base_search_url}")

    # ── 两步导航策略（绕过 BOSS 反爬）──
    # 直接打开完整筛选 URL 会被 BOSS 反爬 redirect 到首页并丢失所有参数。
    # 解法：先进首页 → 延迟 → Playwright fill 关键词 → 验证搜索框 → Enter → 等 3 秒 → 跳转完整筛选 URL。
    homepage_url = f"{BASE_URL}?city=101280600"
    log("INFO", f"Step 1: 先打开首页建立会话 → {homepage_url}")
    code, _, _ = browser.run(["open", homepage_url], allow_fail=True, timeout_sec=60)
    if code != 0:
        log("ERROR", "无法打开 BOSS 首页。")
        status = "open_failed"
        exit_code = 2
    else:
        maybe_wait_after_navigation(browser, 2800, 4200)

        # Step 2: Playwright fill 模拟真实用户输入关键词（含验证+重试）
        log("INFO", f"Step 2: Playwright fill 关键词 → {keyword}")
        fill_selectors = [
            "input[placeholder*='搜索']", "input.ipt-search",
            "input[ka='header-home-search']", "input[name='query']",
        ]
        fill_verified = False
        for attempt in range(3):
            # 延迟至少 1 秒，模拟真实用户节奏
            jitter_sleep(1800, 3200, reason=f"搜索前延迟 (attempt {attempt + 1})")

            fill_ok = False
            used_sel = ""
            for sel in fill_selectors:
                fill_code, _, _ = browser.run(["fill", sel, keyword], allow_fail=True, timeout_sec=10)
                if fill_code == 0:
                    log("INFO", f"fill 成功: selector={sel}")
                    fill_ok = True
                    used_sel = sel
                    break

            if not fill_ok:
                log("WARN", f"所有 fill selector 均失败 (attempt {attempt + 1})")
                continue

            # 验证搜索框内容是否正确
            time.sleep(0.5)
            verify_js = (
                "(function() {"
                "  var sels = ['input[placeholder*=\"搜索\"]', 'input.ipt-search',"
                "    'input[ka=\"header-home-search\"]', 'input[name=\"query\"]'];"
                "  for (var i = 0; i < sels.length; i++) {"
                "    var el = document.querySelector(sels[i]);"
                "    if (el && el.value) return el.value;"
                "  }"
                "  return '';"
                "})()"
            )
            ok_val, input_value = browser.eval(verify_js, allow_fail=True)
            input_value = normalize_text(str(input_value or "")).strip('"').strip("'") if ok_val else ""

            if input_value == keyword:
                log("INFO", f"搜索框验证通过: '{input_value}' == '{keyword}'")
                fill_verified = True
                break
            else:
                log("WARN", f"搜索框内容不一致: 期望='{keyword}', 实际='{input_value}' (attempt {attempt + 1})")
                # 清空搜索框后重试
                if used_sel:
                    browser.run(
                        ["eval", f"document.querySelector('{used_sel}').value = ''"],
                        allow_fail=True, timeout_sec=5,
                    )

        if not fill_verified:
            log("WARN", "搜索框验证失败，降级直接打开完整 URL")

        if fill_verified:
            press_code, _, _ = browser.run(["press", "Enter"], allow_fail=True, timeout_sec=10)
            if press_code == 0:
                log("INFO", "Enter 已触发搜索，等待 4 秒让页面稳定...")
                time.sleep(4)
            else:
                log("WARN", "press Enter 失败，降级直接打开完整 URL")

        # Step 3: 跳转完整筛选 URL（此时 BOSS 已有搜索 session，不会 strip 参数）
        log("INFO", f"Step 3: 跳转完整筛选 URL → {base_search_url}")
        code2, _, _ = browser.run(["open", base_search_url], allow_fail=True, timeout_sec=60)
        if code2 != 0:
            log("ERROR", "无法打开完整筛选 URL。")
            status = "open_failed"
            exit_code = 2
        elif not _retry_open_with_redirect_check(
            browser, base_search_url,
            expected_path="/web/geek/jobs", label="搜索页",
        ):
            log("ERROR", "BOSS 反复重定向到首页，无法加载搜索结果。")
            status = "redirect_to_homepage"
            exit_code = 3
        elif not wait_for_verify_clear(
            browser,
            timeout_sec=args.verify_timeout_sec,
            check_min_sec=args.verify_check_min_sec,
            check_max_sec=args.verify_check_max_sec,
        ):
            status = "verify_timeout"
        else:
            # 验证 URL 中 query 参数与关键词一致
            ok_url, cur_url = browser.eval("location.href", allow_fail=True)
            cur_url = normalize_text(cur_url).strip('"').strip("'") if ok_url else ""
            from urllib.parse import urlsplit, parse_qs
            qs = parse_qs(urlsplit(cur_url).query)
            url_query = qs.get("query", [""])[0]
            if url_query == keyword:
                log("INFO", f"URL query 参数验证通过: query={url_query}")
            else:
                log("WARN", f"URL query 参数不一致: 期望='{keyword}', 实际='{url_query}'")

            log("INFO", "搜索页已就绪，开始抓取岗位卡片")
            for page in range(1, page_limit + 1):
                log("INFO", f"开始抓取第 {page}/{page_limit} 页")
                page_status = process_current_page(
                    browser,
                    jobs,
                    seen_keys,
                    dropped_invalid_links,
                    max_jobs=max_jobs,
                    verify_timeout_sec=args.verify_timeout_sec,
                    verify_check_min_sec=args.verify_check_min_sec,
                    verify_check_max_sec=args.verify_check_max_sec,
                    fallback_city=city_fallback,
                    filters=filters,
                    filter_stats=filter_stats,
                )
                if page_status == "verify_timeout":
                    status = "verify_timeout"
                    break
                if len(jobs) >= max_jobs:
                    break
                if page >= page_limit:
                    break
                jitter_sleep(4500, 7500, reason="翻页前等待")
                next_ok = navigate_to_page(browser, base_search_url, page + 1)
                if not next_ok:
                    log("WARN", f"URL 翻页到第 {page + 1} 页失败（可能被反爬重定向），停止翻页。")
                    break
                if not wait_for_verify_clear(
                    browser,
                    timeout_sec=args.verify_timeout_sec,
                    check_min_sec=args.verify_check_min_sec,
                    check_max_sec=args.verify_check_max_sec,
                ):
                    status = "verify_timeout"
                    break

    payload = {
        "source": "BOSS直聘",
        "status": status,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "minCount": min_count,
        "maxJobs": max_jobs,
        "pageLimit": page_limit,
        "assumptions": assumptions,
        "count": len(jobs),
        "jobs": jobs,
        "droppedInvalidLinkCount": len(dropped_invalid_links),
        "droppedInvalidLinkSamples": dropped_invalid_links[:5],
        "filteredByExperience": filter_stats.get("filteredByExperience", 0),
        "filteredByDegree": filter_stats.get("filteredByDegree", 0),
        "filteredByJobType": filter_stats.get("filteredByJobType", 0),
        "filteredBySalary": filter_stats.get("filteredBySalary", 0),
    }
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("OK", f"raw jobs 已写入: {jobs_path}")
    if len(jobs) < min_count:
        log("WARN", f"抓取条数 {len(jobs)} 未达到 min_count={min_count}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
