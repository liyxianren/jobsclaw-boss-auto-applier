#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Match resume against JD using keyword extraction and cross-matching.

This script does NOT have external side effects (pure analysis).

Args:
  --jd-file <path>      Path to JD JSON (output of scrape_jd.py)
  --resume <path>       Path to candidate resume (markdown)
  --jd-json <json>      Inline JD JSON string (alternative to --jd-file)

Output JSON (stdout):
  {
    "matchScore": "高|中|低",
    "matchPoints": [{"point": "...", "jdReq": "...", "status": "match"}],
    "gaps": ["缺少XX经验"],
    "highlights": ["简历亮点"],
    "suggestedEmphasis": ["建议重点突出的经历"]
  }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

WORKSPACE = str(Path(__file__).resolve().parents[3])
DEFAULT_RESUME = f"{WORKSPACE}/candidate-resume.md"

# ────────────────────────── Tech keyword dictionary ──────────────────────────

# Common tech keywords (Chinese + English) grouped by category
TECH_KEYWORDS: Dict[str, List[str]] = {
    "languages": [
        "Python", "Go", "Golang", "Java", "JavaScript", "TypeScript", "C\\+\\+",
        "C#", "Rust", "PHP", "Ruby", "Swift", "Kotlin", "Scala", "Lua",
        "Shell", "Bash", "SQL", "R语言", "Dart",
    ],
    "ai_ml": [
        "AI", "人工智能", "机器学习", "深度学习", "NLP", "自然语言处理",
        "大模型", "LLM", "GPT", "Claude", "RAG", "向量数据库", "Embedding",
        "Agent", "多Agent", "Prompt", "Fine-?tune", "AIGC", "Transformer",
        "Dify", "LangChain", "OpenAI", "Function Calling", "Workflow",
        "MCP", "OpenClaw", "Coze", "n8n",
    ],
    "frontend": [
        "React", "Vue", "Angular", "Next\\.?js", "Nuxt", "小程序", "微信小程序",
        "Taro", "uni-?app", "Flutter", "React Native", "HTML", "CSS",
        "Webpack", "Vite", "Tailwind",
    ],
    "backend": [
        "Flask", "Django", "FastAPI", "Spring", "Spring Boot", "Node\\.?js",
        "Express", "Koa", "Nest\\.?js", "Gin", "Fiber", "gRPC", "REST",
        "微服务", "中台", "后端", "API",
    ],
    "data": [
        "MySQL", "PostgreSQL", "MongoDB", "Redis", "Elasticsearch", "OpenSearch",
        "Kafka", "RabbitMQ", "数据库", "ETL", "数据分析", "数据仓库",
        "ClickHouse", "大数据", "Hadoop", "Spark", "Flink",
    ],
    "devops": [
        "Docker", "K8s", "Kubernetes", "CI/CD", "Jenkins", "GitHub Actions",
        "AWS", "阿里云", "腾讯云", "Linux", "Nginx", "运维", "DevOps",
    ],
    "product": [
        "产品经理", "产品设计", "需求分析", "用户研究", "竞品分析",
        "PRD", "原型", "Axure", "Figma", "B端", "C端", "SaaS",
        "用户增长", "数据驱动", "商业化", "项目管理",
    ],
    "embedded": [
        "嵌入式", "RISC-?V", "ARM", "MCU", "IoT", "物联网", "固件",
        "RTOS", "STM32", "单片机", "硬件", "芯片",
    ],
    "automation": [
        "自动化", "RPA", "爬虫", "CDP", "Selenium", "Playwright",
        "浏览器自动化", "状态机", "工作流",
    ],
    "soft_skills": [
        "带团队", "团队管理", "项目管理", "沟通", "协作",
        "领导力", "跨部门", "从0到1", "全栈",
    ],
}

# Flatten for quick lookup
ALL_KEYWORDS: List[str] = []
for group in TECH_KEYWORDS.values():
    ALL_KEYWORDS.extend(group)


# ────────────────────────── Keyword extraction ──────────────────────────

def extract_keywords(text: str) -> Set[str]:
    """Extract matching tech keywords from text."""
    found: Set[str] = set()
    text_lower = text.lower()
    for kw in ALL_KEYWORDS:
        pattern = re.compile(kw, re.IGNORECASE)
        if pattern.search(text):
            # Normalize the keyword to its canonical form
            found.add(kw.replace("\\", "").replace(".?", "").replace("-?", ""))
    return found


def extract_years_experience(text: str) -> List[Tuple[str, str]]:
    """Extract experience descriptions with years."""
    results = []
    # Patterns like "3年Go经验", "5年以上Python"
    patterns = [
        r"(\d+)\s*年(?:以上)?\s*([A-Za-z\u4e00-\u9fff]+)\s*(?:经验|开发|工作)",
        r"(\d+)\s*年\s*(?:以上\s*)?(?:的\s*)?(.{2,10}?)经验",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            results.append((m.group(1), m.group(2)))
    return results


def extract_achievements(text: str) -> List[str]:
    """Extract quantified achievements from resume text."""
    achievements = []
    # Look for patterns with numbers that indicate achievements
    patterns = [
        r"[^\n。]*(?:日均|月均|累计|节省|提升|压缩|覆盖|服务)[^\n。]*[\d,]+[^\n。]*",
        r"[^\n。]*[\d,]+\+?\s*(?:个|门|条|人|万|%|课时|项目|章节)[^\n。]*",
        r"[^\n。]*(?:从\s*\d+\s*(?:小时|天|周|月)\s*(?:压缩|缩短|降低))[^\n。]*",
        r"[^\n。]*(?:专利|获奖|第\s*\d+\s*名|前\s*\d+%)[^\n。]*",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            ach = m.group(0).strip().strip("。，,")
            if len(ach) > 10 and ach not in achievements:
                achievements.append(ach)
    return achievements[:8]  # Limit


def extract_companies(text: str) -> List[str]:
    """Extract company names from resume."""
    companies = []
    # Match markdown headers with company names: ### CompanyName｜dates
    for m in re.finditer(r"###\s+(.+?)(?:｜|\|)", text):
        company = m.group(1).strip()
        if company:
            companies.append(company)
    return companies


def extract_projects(text: str) -> List[str]:
    """Extract project names from resume."""
    projects = []
    for m in re.finditer(r"###\s+(.+?)(?:｜|\|)", text):
        name = m.group(1).strip()
        if name:
            projects.append(name)
    return projects


# ────────────────────────── JD parsing ──────────────────────────

def parse_jd_requirements(jd_desc: str) -> List[str]:
    """Extract specific requirement lines from JD description."""
    reqs = []
    for line in jd_desc.split("\n"):
        line = line.strip()
        # Lines starting with numbers or bullet points
        if re.match(r"^[\d\.、\-\*]+\s*", line) and len(line) > 5:
            reqs.append(re.sub(r"^[\d\.、\-\*]+\s*", "", line).strip())
        elif any(kw in line for kw in ["要求", "负责", "熟悉", "精通", "了解", "掌握", "具备"]):
            reqs.append(line)
    return reqs


# ────────────────────────── Core matching ──────────────────────────

def match_resume_jd(
    jd_data: Dict[str, Any],
    resume_text: str,
) -> Dict[str, Any]:
    """Perform keyword-based matching between resume and JD."""

    jd_desc = jd_data.get("description", "")
    jd_title = jd_data.get("jobTitle", "")
    jd_tags = jd_data.get("tags", [])

    # Combine all JD text
    jd_full = f"{jd_title} {jd_desc} {' '.join(jd_tags)}"

    # 1. Extract keywords from both
    jd_keywords = extract_keywords(jd_full)
    resume_keywords = extract_keywords(resume_text)

    # 2. Compute match / gaps
    matched = jd_keywords & resume_keywords
    gaps = jd_keywords - resume_keywords
    extra = resume_keywords - jd_keywords

    # 3. Build match points with context
    match_points = []
    for kw in sorted(matched):
        match_points.append({
            "point": kw,
            "jdReq": kw,
            "status": "match",
        })

    gap_list = sorted(gaps)

    # 4. Extract highlights from resume (quantified achievements)
    highlights = extract_achievements(resume_text)

    # 5. Companies and projects for context
    companies = extract_companies(resume_text)
    projects = extract_projects(resume_text)

    # 6. Suggested emphasis: matched keywords + top highlights
    suggested = []
    # Prioritize matched keywords that appear in JD tags
    for tag in jd_tags:
        tag_lower = tag.lower()
        for kw in matched:
            if kw.lower() in tag_lower or tag_lower in kw.lower():
                if kw not in suggested:
                    suggested.append(kw)
    # Add remaining matched keywords
    for kw in matched:
        if kw not in suggested:
            suggested.append(kw)
    # Add top highlights
    for h in highlights[:2]:
        if h not in suggested:
            suggested.append(h)

    # 7. Score
    if len(matched) >= 5:
        score = "high"
    elif len(matched) >= 2:
        score = "medium"
    else:
        score = "low"

    return {
        "matchScore": score,
        "matchedKeywords": sorted(matched),
        "matchPoints": match_points,
        "gaps": gap_list,
        "highlights": highlights,
        "extraSkills": sorted(extra),
        "companies": companies,
        "projects": projects,
        "suggestedEmphasis": suggested[:6],
        "jdTitle": jd_title,
        "jdCompany": jd_data.get("company", ""),
        "jdSalary": jd_data.get("salary", ""),
        "jdRequirements": parse_jd_requirements(jd_desc),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Match resume against JD")
    ap.add_argument("--jd-file", help="Path to JD JSON file (from scrape_jd.py)")
    ap.add_argument("--jd-json", help="Inline JD JSON string")
    ap.add_argument("--resume", default=DEFAULT_RESUME, help="Path to resume markdown")
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

    # Load resume
    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume not found: {resume_path}", file=sys.stderr)
        sys.exit(1)
    resume_text = resume_path.read_text(encoding="utf-8")

    # Match
    result = match_resume_jd(jd_data, resume_text)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
