#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sender Worker（仅发送，不评估）。

输入：
  --job-url <url>
  --eval-json <path>
  --send-out <path>
  --cdp-port <int>
  --screenshot-dir <path>

行为：仅调用 send_greeting.py
输出：原样记录 send_greeting 返回 + stderr 摘要
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE = Path(__file__).resolve().parents[3]
SEND_SCRIPT = WORKSPACE / "skills/jd-greeting-generator/scripts/send_greeting.py"


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


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_stderr_summary(stderr_text: str, max_lines: int = 30, max_chars: int = 4000) -> str:
    if not stderr_text:
        return ""
    lines = [ln.rstrip() for ln in stderr_text.splitlines() if ln.strip()]
    tail = lines[-max_lines:]
    summary = "\n".join(tail)
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Sender worker (send-only)")
    ap.add_argument("--job-url", required=True)
    ap.add_argument("--eval-json", required=True)
    ap.add_argument("--send-out", required=True)
    ap.add_argument("--cdp-port", type=int, required=True)
    ap.add_argument("--screenshot-dir", required=True)
    ap.add_argument(
        "--allow-intent-failed-retry",
        action="store_true",
        help="Pass through to send_greeting.py to allow retry when intent is failed",
    )
    ap.add_argument(
        "--no-retry",
        action="store_true",
        help="Disable send_greeting in-process retries",
    )
    ap.add_argument(
        "--capture-screenshot",
        action="store_true",
        help="Enable screenshots in send_greeting (default off)",
    )
    args = ap.parse_args()

    eval_path = Path(args.eval_json)
    send_out = Path(args.send_out)

    try:
        eval_data = load_json(eval_path)
        message = (eval_data.get("message") or eval_data.get("messageDraft") or "").strip()

        # Append PS signature required by send_greeting.py
        PS_SIGNATURE = "\nPS：本条消息由我基于 OpenClaw 开发的智能求职 Agent 自动完成"
        if message and "OpenClaw" not in message:
            message = message + PS_SIGNATURE

        match_score = eval_data.get("matchScore")
        reasoning = eval_data.get("reasoning")

        if not message:
            result = {
                "sendResult": {
                    "status": "failed",
                    "error": "empty_message_draft",
                    "jobUrl": args.job_url,
                },
                "stderrSummary": "",
                "exitCode": 2,
            }
            dump_json(send_out, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"message": message}, tmp, ensure_ascii=False)
            tmp_message_path = Path(tmp.name)

        try:
            cmd: List[str] = [
                sys.executable,
                str(SEND_SCRIPT),
                "--job-url",
                args.job_url,
                "--message-file",
                str(tmp_message_path),
                "--cdp-port",
                str(args.cdp_port),
                "--screenshot-dir",
                args.screenshot_dir,
            ]
            if match_score:
                cmd.extend(["--match-score", str(match_score)])
            if reasoning:
                cmd.extend(["--reasoning", str(reasoning)])
            if args.allow_intent_failed_retry:
                cmd.append("--allow-intent-failed-retry")
            if args.no_retry:
                cmd.append("--no-retry")
            if args.capture_screenshot:
                cmd.append("--capture-screenshot")

            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout = cp.stdout or ""
            stderr = cp.stderr or ""

            try:
                send_result = extract_json_from_mixed_output(stdout)
            except Exception:
                send_result = {
                    "status": "failed",
                    "error": "invalid_json_from_send_greeting",
                    "rawStdout": stdout[-2000:],
                    "jobUrl": args.job_url,
                }

            result = {
                "sendResult": send_result,
                "stderrSummary": build_stderr_summary(stderr),
                "exitCode": cp.returncode,
            }
            dump_json(send_out, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if isinstance(send_result, dict) and send_result.get("status") in {"ok", "sent", "skipped"} else 2
        finally:
            try:
                tmp_message_path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as exc:
        result = {
            "sendResult": {
                "status": "failed",
                "error": f"sender_exception: {exc}",
                "jobUrl": args.job_url,
            },
            "stderrSummary": "",
            "exitCode": 2,
        }
        dump_json(send_out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
