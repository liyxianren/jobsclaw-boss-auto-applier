#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Generate a BOSS直聘 greeting message based on JD + resume match analysis.

This script does NOT have external side effects (pure text generation).

Args:
  --jd-file <path>      Path to JD JSON (from scrape_jd.py)
  --match-file <path>   Path to match JSON (from match_resume.py)
  --resume <path>       Path to resume markdown
  --style <str>         Style: "professional" | "casual" | "technical" (default: professional)
  --jd-json <json>      Inline JD JSON string
  --match-json <json>   Inline match JSON string

Output: greeting message (stdout, plain text)
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE = str(Path(__file__).resolve().parents[3])
DEFAULT_RESUME = f"{WORKSPACE}/candidate-resume.md"


# ────────────────────────── Resume info extraction ──────────────────────────

def extract_resume_basics(resume_text: str) -> Dict[str, str]:
    """Extract basic info from resume markdown."""
    info: Dict[str, str] = {}

    # Name (first heading or first line)
    name_match = re.search(r"^#\s+(.+)", resume_text, re.MULTILINE)
    if name_match:
        info["name"] = name_match.group(1).strip()

    # Target position
    target_match = re.search(r"目标岗位[：:]\s*(.+)", resume_text)
    if target_match:
        info["targetRole"] = target_match.group(1).strip()

    # Current / latest company
    company_matches = re.findall(r"###\s+(.+?)(?:｜|\|)", resume_text)
    if company_matches:
        info["latestCompany"] = company_matches[0].strip()
        info["companies"] = [c.strip() for c in company_matches]

    # Years of experience (from work section only, not education)
    work_section = resume_text
    work_marker = re.search(r"工作经历|工作经验|职业经历", resume_text)
    if work_marker:
        work_start = work_marker.start()
        work_section = resume_text[work_start:]
        # Stop at next ## section that's not work-related
        next_sec = re.search(r"\n##\s+(?!.*工作)", work_section[10:])
        if next_sec:
            work_section = work_section[:next_sec.start() + 10]
    year_spans = re.findall(r"(\d{4})\.?\d*\s*[-–~]\s*(?:(\d{4})\.?\d*|至今|present)", work_section, re.IGNORECASE)
    if year_spans:
        start_year = min(int(y[0]) for y in year_spans)
        end_years = [int(y[1]) if y[1] else 2026 for y in year_spans]
        max_end = max(end_years)
        info["yearsExp"] = str(max_end - start_year)

    # Latest role title (strip markdown bold markers)
    role_matches = re.findall(r"职位[：:]\s*(.+)", resume_text)
    if role_matches:
        role = role_matches[0].strip().strip("*").strip()
        info["latestRole"] = role

    return info


def extract_key_achievements(resume_text: str) -> List[str]:
    """Extract top quantified achievements from resume."""
    achievements = []
    # Look for lines with numbers and impact keywords
    patterns = [
        r"(?:交付|完成|服务|覆盖)\s*(\d+[\+]?\s*(?:个|门|条|人|万))[^\n]*",
        r"(?:节省|压缩|缩短|提升|降低)[^\n]*?(?:\d+[%元万小时天周月])[^\n]*",
        r"日均[^\n]*?(?:\d+[\+]?\s*(?:万|条|级))[^\n]*",
        r"(?:从\s*\d+\s*(?:小时|天)\s*(?:压缩|缩短)[^\n]*?\d+[^\n]*)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, resume_text):
            ach = m.group(0).strip().strip("。，,；;")
            if 8 < len(ach) < 80 and ach not in achievements:
                achievements.append(ach)
    return achievements[:5]


def _trim_at_boundary(text: str, max_len: int = 60) -> str:
    """Trim text at a natural Chinese/English boundary."""
    # Strip markdown formatting
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.strip().strip("-").strip()
    if len(text) <= max_len:
        return text
    # Try to cut at a clause separator
    for sep in ["，", "；", "、", ",", "。", " "]:
        idx = text.rfind(sep, 0, max_len)
        if idx > max_len // 2:
            return text[:idx]
    return text[:max_len]


# ────────────────────────── Message generation ──────────────────────────

# Template components (mix and match for variety)
OPENINGS = {
    "professional": [
        "看到贵司{role}岗位，我的背景比较契合",
        "对贵司{role}方向很感兴趣",
        "我目前在做{current_field}方向，和贵司{role}岗位很对口",
    ],
    "casual": [
        "看到贵司在招{role}",
        "这个{role}岗位很吸引我",
        "对{company}的{role}岗位很感兴趣",
    ],
    "technical": [
        "看到贵司{role}岗位的技术栈和我的经验很匹配",
        "在{tech_stack}方面有比较深的实战经验，和贵司{role}方向一致",
    ],
}

MATCH_TEMPLATES = [
    "我在{company}做过{project_desc}，{achievement}",
    "之前在{company}主导{project_desc}，{achievement}",
    "有{years}年{field}经验，在{company}{project_desc}",
]

CLOSINGS = {
    "professional": [
        "方便的话聊聊？",
        "期待有机会进一步交流。",
        "希望能和您详细聊聊。",
    ],
    "casual": [
        "方便聊聊吗？",
        "有空交流一下？",
        "感兴趣的话咱们聊聊？",
    ],
    "technical": [
        "方便的话可以深入聊聊技术细节。",
        "期待交流。",
    ],
}


def build_message(
    jd_data: Dict[str, Any],
    match_data: Dict[str, Any],
    resume_text: str,
    style: str = "professional",
) -> str:
    """Generate greeting message from JD + match + resume data."""

    resume_info = extract_resume_basics(resume_text)
    achievements = extract_key_achievements(resume_text)

    jd_title = jd_data.get("jobTitle", "")
    jd_company = jd_data.get("company", "")
    match_points = match_data.get("matchedKeywords", [])
    highlights = match_data.get("highlights", [])
    suggested = match_data.get("suggestedEmphasis", [])
    companies = resume_info.get("companies", [])
    years_exp = resume_info.get("yearsExp", "")
    latest_company = resume_info.get("latestCompany", "")
    latest_role = resume_info.get("latestRole", "")

    # Determine key tech areas to mention
    tech_keywords = match_points[:3] if match_points else []
    tech_str = "+".join(tech_keywords) if tech_keywords else ""

    # Build opening
    openings = OPENINGS.get(style, OPENINGS["professional"])
    opening_vars = {
        "role": jd_title or "该",
        "company": jd_company,
        "current_field": latest_role or "AI",
        "tech_stack": tech_str or "相关技术",
    }
    opening = random.choice(openings)
    for k, v in opening_vars.items():
        opening = opening.replace(f"{{{k}}}", v)

    # Build match/experience section (the meat of the message)
    body_parts = []

    # Mention specific experience
    if latest_company and achievements:
        # Pick 1-2 most relevant achievements
        relevant_ach = achievements[:2]
        for ach in relevant_ach:
            # Trim at natural boundary (sentence/clause separator)
            ach = _trim_at_boundary(ach, max_len=60)
            body_parts.append(ach)

    # Add tech match context
    if tech_keywords and years_exp:
        tech_mention = f"有{years_exp}年{'+'.join(tech_keywords[:2])}相关经验"
        body_parts.insert(0, tech_mention)
    elif tech_keywords:
        tech_mention = f"在{'+'.join(tech_keywords[:3])}方面有实战经验"
        body_parts.insert(0, tech_mention)

    # If we have highlighted items from match but no achievements
    if not body_parts and highlights:
        for h in highlights[:2]:
            h = _trim_at_boundary(h, max_len=60)
            body_parts.append(h)

    # Mention company for credibility
    if latest_company and not any(latest_company in p for p in body_parts):
        body_parts.insert(0, f"目前在{latest_company}")

    # Build closing
    closings = CLOSINGS.get(style, CLOSINGS["professional"])
    closing = random.choice(closings)

    # Assemble message
    # Opening. Body. Closing.
    body_text = "，".join(body_parts) if body_parts else ""

    if body_text:
        message = f"{opening}。{body_text}。{closing}"
    else:
        message = f"{opening}。{closing}"

    # Clean up
    message = message.replace("。。", "。").replace("，。", "。")
    message = re.sub(r"\s+", "", message)  # Remove stray whitespace in Chinese text

    # Validate length (100-300 chars)
    if len(message) < 80:
        # Too short, add more context
        extra = []
        if jd_company:
            extra.append(f"对{jd_company}的业务方向很认可")
        if suggested:
            extra.append(f"在{suggested[0]}方面有深入实践")
        if extra:
            insert_text = "，".join(extra)
            message = f"{opening}。{insert_text}。{body_text}。{closing}" if body_text else f"{opening}。{insert_text}。{closing}"
            message = message.replace("。。", "。").replace("，。", "。")
            message = re.sub(r"\s+", "", message)

    if len(message) > 300:
        # Too long, trim body
        # Keep opening + first body part + closing
        short_body = body_parts[0] if body_parts else ""
        message = f"{opening}。{short_body}。{closing}" if short_body else f"{opening}。{closing}"
        message = message.replace("。。", "。").replace("，。", "。")
        message = re.sub(r"\s+", "", message)

    return message


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate BOSS直聘 greeting message")
    ap.add_argument("--jd-file", help="Path to JD JSON file")
    ap.add_argument("--match-file", help="Path to match JSON file")
    ap.add_argument("--resume", default=DEFAULT_RESUME, help="Path to resume markdown")
    ap.add_argument("--jd-json", help="Inline JD JSON string")
    ap.add_argument("--match-json", help="Inline match JSON string")
    ap.add_argument("--style", default="professional",
                    choices=["professional", "casual", "technical"],
                    help="Message style (default: professional)")
    ap.add_argument("--json", action="store_true", help="Output JSON format")
    args = ap.parse_args()

    # Load JD
    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            jd_data = json.load(f)
    elif args.jd_json:
        jd_data = json.loads(args.jd_json)
    else:
        print("Error: provide --jd-file or --jd-json", file=sys.stderr)
        sys.exit(1)

    # Load match
    if args.match_file:
        with open(args.match_file, "r", encoding="utf-8") as f:
            match_data = json.load(f)
    elif args.match_json:
        match_data = json.loads(args.match_json)
    else:
        print("Error: provide --match-file or --match-json", file=sys.stderr)
        sys.exit(1)

    # Load resume
    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume not found: {resume_path}", file=sys.stderr)
        sys.exit(1)
    resume_text = resume_path.read_text(encoding="utf-8")

    # Generate
    message = build_message(jd_data, match_data, resume_text, style=args.style)
    if args.json:
        print(json.dumps({"message": message}, ensure_ascii=False))
    else:
        print(message)


if __name__ == "__main__":
    main()
