#!/usr/bin/env python3
"""Run end-to-end boss-zhipin-search pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List
from urllib.request import urlopen
from urllib.error import URLError


def run(cmd: List[str]) -> int:
    print(f"[RUN] {' '.join(cmd)}", flush=True)
    cp = subprocess.run(cmd, text=True, capture_output=True)
    if cp.stdout:
        print(cp.stdout, end="")
    if cp.stderr:
        print(cp.stderr, end="", file=sys.stderr)
    return cp.returncode


def _cdp_has_pages(cdp_port: int) -> bool:
    """Check if Chrome CDP has at least one page tab (not just the process)."""
    try:
        import json as _json
        resp = urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=3)
        tabs = _json.loads(resp.read())
        return any(t.get("type") == "page" for t in tabs)
    except Exception:
        return False


def ensure_chrome_cdp(cdp_port: int) -> bool:
    """Always kill and restart Chrome CDP for clean state (avoid stale tab issues)."""
    cdp_url = f"http://127.0.0.1:{cdp_port}/json/version"

    # Find start_boss_chrome.sh
    start_script = Path(__file__).resolve().parent.parent.parent / "jd-greeting-generator" / "scripts" / "start_boss_chrome.sh"
    if not start_script.exists():
        print(f"[ERROR] start script not found: {start_script}", file=sys.stderr)
        return False

    # Always force-relaunch: kill existing Chrome on this port, then restart fresh.
    # This prevents stale state from a previously opened browser causing issues.
    print(f"[INFO] Force-restarting Chrome on :{cdp_port} for clean state...", flush=True)
    env = dict(__import__("os").environ)
    env["AGENT_BROWSER_CDP_PORT"] = str(cdp_port)
    env["AGENT_BROWSER_HEADLESS"] = "0"
    env["AGENT_BROWSER_FORCE_RELAUNCH"] = "1"
    # Open homepage only — scrape script handles keyword search + filter navigation
    env["AGENT_BROWSER_BOSS_START_URL"] = "https://www.zhipin.com/web/geek/jobs?city=101280600"

    rc = subprocess.run(["bash", str(start_script)], text=True, capture_output=True, env=env)
    if rc.stdout:
        print(rc.stdout, end="")
    if rc.stderr:
        print(rc.stderr, end="", file=sys.stderr)

    # Wait for CDP + page to be ready
    for _ in range(20):
        time.sleep(1)
        try:
            urlopen(cdp_url, timeout=3)
            if _cdp_has_pages(cdp_port):
                print(f"[OK] Chrome CDP started on :{cdp_port} (headed, page attached)", flush=True)
                return True
        except (URLError, OSError):
            pass

    print(f"[ERROR] Chrome CDP failed to start on :{cdp_port} after 20s", file=sys.stderr)
    return False


def read_min_count(path: Path, fallback: int = 20) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and int(data.get("minCount", fallback)) > 0:
            return int(data.get("minCount", fallback))
    except Exception:
        pass
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="validate -> scrape(browser) -> summarize")
    parser.add_argument("--input", required=True, help="filters json path")
    parser.add_argument("--outdir", required=True, help="output directory")
    parser.add_argument("--cdp-port", type=int, default=18801, help="CDP port of BOSS-dedicated Chrome (default: 18801)")
    parser.add_argument("--headed", dest="headed", action="store_true", help="run browser in headed mode")
    parser.add_argument("--no-headed", dest="headed", action="store_false", help="run browser in headless mode")
    parser.set_defaults(headed=True)
    parser.add_argument("--min-count", type=int, default=None, help="override minCount for scrape+summary")
    parser.add_argument("--max-jobs", type=int, default=None, help="max jobs for scrape")
    parser.add_argument("--page-limit", type=int, default=None, help="max pages for scrape")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    validate_script = script_dir / "validate_filters.py"
    scrape_script = script_dir / "scrape_jobs_browser.py"
    summarize_script = script_dir / "summarize_jobs.py"

    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rc = run([sys.executable, str(validate_script), "--input", str(input_path)])
    if rc != 0:
        return rc

    # Auto-start Chrome if not running
    if not ensure_chrome_cdp(args.cdp_port):
        return 2

    scrape_cmd = [
        sys.executable,
        str(scrape_script),
        "--input",
        str(input_path),
        "--outdir",
        str(outdir),
        "--cdp-port",
        str(args.cdp_port),
    ]
    scrape_cmd.append("--headed" if args.headed else "--no-headed")
    if args.min_count is not None:
        scrape_cmd.extend(["--min-count", str(args.min_count)])
    if args.max_jobs is not None:
        scrape_cmd.extend(["--max-jobs", str(args.max_jobs)])
    if args.page_limit is not None:
        scrape_cmd.extend(["--page-limit", str(args.page_limit)])

    scrape_rc = run(scrape_cmd)

    min_count = args.min_count if args.min_count is not None else read_min_count(input_path, fallback=20)
    jobs_path = outdir / "jobs.json"
    if not jobs_path.exists():
        print(f"[ERROR] scrape output not found: {jobs_path}", file=sys.stderr)
        return scrape_rc if scrape_rc != 0 else 2
    if scrape_rc != 0:
        print("[WARN] scrape exited non-zero, summarize will still run on existing jobs.json", flush=True)

    rc = run(
        [
            sys.executable,
            str(summarize_script),
            "--input",
            str(jobs_path),
            "--outdir",
            str(outdir),
            "--min-count",
            str(min_count),
        ]
    )
    if rc != 0:
        return rc

    print(f"[OK] pipeline completed: {outdir}", flush=True)
    print(f"[OK] raw jobs: {jobs_path}", flush=True)
    print(f"[OK] summary json: {outdir / 'results.json'}", flush=True)
    print(f"[OK] summary md: {outdir / 'summary.md'}", flush=True)
    return scrape_rc


if __name__ == "__main__":
    raise SystemExit(main())
