#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch match and rank jobs against candidate resume.

Bridges boss-zhipin-search output (jobs.json) and jd-greeting-generator input.
Reuses keyword matching logic from match_resume.py.

No external side effects (pure analysis + DB read for dedup).

Usage:
    python3 rank_jobs.py \
        --jobs <jobs.json> \
        --resume <candidate-resume.md> \
        --output <ranked_jobs.json> \
        --db <boss_greeting.db>       # optional: skip already-sent jobs
        --min-score medium            # optional: filter threshold
        --max-count 20                # optional: limit output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

WORKSPACE = str(Path(__file__).resolve().parents[3])
DEFAULT_RESUME = f"{WORKSPACE}/candidate-resume.md"
DEFAULT_DB = f"{WORKSPACE}/data/boss_greeting.db"
DEFAULT_PREFS = f"{WORKSPACE}/candidate-preferences.json"

# Default priority company keywords (for new priority ranking)
DEFAULT_PRIORITY_COMPANIES = [
    "腾讯", "Tencent", "字节", "ByteDance", "抖音", "TikTok",
    "百度", "Baidu", "阿里", "阿里云", "Alibaba", "Aliyun",
    "阿里巴巴", "蚂蚁", "Ant", "美团", "拼多多", "PDD",
    "京东", "JD", "快手", "Kwai", "B站", "Bilibili",
    "网易", "NetEase", "滴滴", "Didi", "小米", "Xiaomi",
]

# ────────────────────────── Tech keyword dictionary ──────────────────────────
# Duplicated from match_resume.py to avoid import path issues.
# If match_resume.py is refactored into a library, this can import from there.

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

ALL_KEYWORDS: List[str] = []
for _group in TECH_KEYWORDS.values():
    ALL_KEYWORDS.extend(_group)

SCORE_ORDER = {"high": 0, "medium": 1, "low": 2}


# ────────────────────────── Keyword extraction ──────────────────────────

def extract_keywords(text: str) -> Set[str]:
    """Extract matching tech keywords from text."""
    found: Set[str] = set()
    for kw in ALL_KEYWORDS:
        pattern = re.compile(kw, re.IGNORECASE)
        if pattern.search(text):
            found.add(kw.replace("\\", "").replace(".?", "").replace("-?", ""))
    return found


# ────────────────────────── Priority Calculation ──────────────────────────

def parse_salary_k(salary_str: str) -> tuple:
    """Extract (min_k, max_k) from salary string like '15-25K', '20-30K·14薪'.
    Returns (None, None) if unparseable."""
    if not salary_str:
        return None, None
    m = re.search(r"(\d{1,3})\s*[-~]\s*(\d{1,3})\s*K", salary_str, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def parse_experience_years(exp_str: str) -> int | None:
    """Parse experience requirement to minimum years."""
    if not exp_str:
        return None
    # Match "1-3年", "3-5年", "5年以上", etc.
    m = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*年", exp_str)
    if m:
        return int(m.group(1))  # Minimum years
    m = re.search(r"(\d+)\s*年以上", exp_str)
    if m:
        return int(m.group(1))
    if "不限" in exp_str or "经验不限" in exp_str:
        return 0
    return None


def calculate_priority(
    job: Dict[str, Any],
    prefs: Dict[str, Any],
    priority_companies: List[str]
) -> Dict[str, Any]:
    """Calculate priority scores for a job based on user preferences.
    
    Priority order: 1) Company (highest weight) > 2) Experience > 3) Salary
    """
    company = job.get("company", "")
    experience = job.get("experience", "")
    salary = job.get("salary", "")

    # 1. Company priority (highest weight)
    priority_company = 0.0
    company_lower = company.lower()
    is_big_tech = False
    for kw in priority_companies:
        if kw.lower() in company_lower:
            priority_company = 100.0
            is_big_tech = True
            break

    # 2. Experience priority
    exp_years = parse_experience_years(experience)
    preferred_max_exp = prefs.get("preferredMaxExperienceYears", 3)
    if exp_years is None:
        priority_experience = 50.0  # Unknown, middle ground
    elif exp_years <= 1:
        priority_experience = 100.0
    elif exp_years <= preferred_max_exp:
        priority_experience = 80.0
    elif exp_years <= 5:
        priority_experience = 40.0
    else:
        priority_experience = 0.0  # Too senior (>=5 years, keep existing hard reject)

    # 3. Salary priority
    salary_range = prefs.get("salaryRange", {})
    pref_min = salary_range.get("min", 0)
    pref_max = salary_range.get("max", 999)
    job_min, job_max = parse_salary_k(salary)

    if job_min is None or job_max is None:
        priority_salary = 50.0  # Unknown
    elif job_min >= pref_max + 5:
        priority_salary = 0.0  # Too high
    elif job_max < pref_min - 3:
        priority_salary = 20.0  # Too low
    elif job_min >= pref_min and job_max <= pref_max:
        priority_salary = 100.0  # Perfect range
    elif job_min < pref_min:
        priority_salary = 60.0  # Slightly below (acceptable)
    else:
        priority_salary = 70.0  # Slightly above (acceptable)

    # Total priority (weighted)
    # Company has highest weight (50%), then experience (30%), then salary (20%)
    priority_total = (
        priority_company * 0.5 +
        priority_experience * 0.3 +
        priority_salary * 0.2
    )

    return {
        "priority_company": priority_company,
        "priority_experience": priority_experience,
        "priority_salary": priority_salary,
        "priority_total": priority_total,
        "is_big_tech": is_big_tech,
    }


# ────────────────────────── DB dedup ──────────────────────────

def load_sent_urls(db_path: str) -> Set[str]:
    """Load already-sent/already-contacted job URLs from the greeting DB."""
    p = Path(db_path)
    if not p.exists():
        return set()
    try:
        conn = sqlite3.connect(str(p))
        rows = conn.execute(
            "SELECT job_url FROM greetings WHERE status IN ('sent', 'pending', 'already_contacted')"
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}
    except Exception as e:
        print(f"[WARN] failed to read DB {db_path}: {e}", file=sys.stderr)
        return set()


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def _normalize_title(title: Optional[str]) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    s = re.sub(r"[（(][^）)]*[）)]\s*$", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def _semantic_fingerprint(company: Optional[str], title: Optional[str], recruiter: Optional[str]) -> Optional[str]:
    c = _normalize_text(company)
    t = _normalize_title(title)
    r = _normalize_text(recruiter)
    if not (c or t or r):
        return None
    raw = f"{c}|{t}|{r}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_sent_fingerprints(db_path: str) -> Set[str]:
    """Load semantic fingerprints of already-sent/contacted jobs."""
    p = Path(db_path)
    if not p.exists():
        return set()
    try:
        conn = sqlite3.connect(str(p))
        rows = conn.execute(
            "SELECT semantic_fingerprint FROM greetings "
            "WHERE status IN ('sent', 'pending', 'already_contacted') "
            "AND semantic_fingerprint IS NOT NULL"
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}
    except Exception as e:
        print(f"[WARN] failed to read fingerprints from DB {db_path}: {e}", file=sys.stderr)
        return set()


# ────────────────────────── Preferences-based hard filter ──────────────────────────

def load_preferences(path: str) -> Dict[str, Any]:
    """Load candidate preferences for hard filtering."""
    p = Path(path)
    if not p.exists():
        print(f"[WARN] preferences file not found: {p}", file=sys.stderr)
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] failed to read preferences: {e}", file=sys.stderr)
        return {}


def hard_filter_job(job: Dict[str, Any], prefs: Dict[str, Any], target_city: str) -> str | None:
    """Return rejection reason string, or None if job passes all filters."""
    title = (job.get("title") or "").strip()
    salary_str = job.get("salary", "") or ""
    city = (job.get("city") or "").strip()
    experience = (job.get("experience") or "").strip()

    reject = prefs.get("hardReject", {})

    # 1. Internship / campus recruitment filter
    reject_keywords = reject.get("keywords", [])
    for kw in reject_keywords:
        if kw in title:
            return f"reject_keyword:{kw}"

    # Daily/monthly pay = internship
    reject_salary_patterns = reject.get("salaryPatterns", [])
    for pat in reject_salary_patterns:
        if pat in salary_str:
            return f"reject_salary_pattern:{pat}"

    # 2. City filter (empty city = unknown, also reject unless no target)
    if target_city:
        if city and target_city not in city and city not in target_city:
            return f"reject_city:{city}"
        # NOTE: if city is empty, we still allow it through — the scraper
        # sometimes fails to extract city. scrape_jd.py will catch it later.

    # 3. Salary range filter
    salary_range = prefs.get("salaryRange", {})
    pref_min = salary_range.get("min", 0)
    pref_max = salary_range.get("max", 999)
    job_min, job_max = parse_salary_k(salary_str)
    if job_min is not None and job_max is not None:
        # Reject if job's min salary >= our max + small buffer
        # e.g. target 15-25K: 30K+ min = too senior, reject
        if job_min >= pref_max + 5:
            return f"reject_salary_too_high:{salary_str}"
        # Reject if job's max salary < our min - small buffer
        if job_max < pref_min - 3:
            return f"reject_salary_too_low:{salary_str}"

    # 4. Experience filter — reject if job requires too many years
    min_exp_reject = reject.get("minExperienceYears", 99)
    exp_match = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*年", experience)
    if exp_match:
        exp_min = int(exp_match.group(1))
        if exp_min >= min_exp_reject:
            return f"reject_experience:{experience}"
    exp_match2 = re.search(r"(\d+)\s*年以上", experience)
    if exp_match2:
        exp_years = int(exp_match2.group(1))
        if exp_years >= min_exp_reject:
            return f"reject_experience:{experience}"

    # 5. Hard-reject programming languages in title
    reject_langs = reject.get("languages", [])
    for lang in reject_langs:
        # Only reject if the language appears prominently in title (as core requirement)
        # e.g. "Java开发" but not "Python/Java均可"
        pattern = re.compile(rf"\b{re.escape(lang)}\b|{re.escape(lang)}开发|{re.escape(lang)}工程师", re.IGNORECASE)
        if pattern.search(title):
            # Check if it's an "either-or" (均可/或) — don't reject those
            if not re.search(r"均可|皆可|优先|或", title):
                return f"reject_language:{lang}"

    return None  # passed all filters


# ────────────────────────── Core matching ──────────────────────────

def match_job(job: Dict[str, Any], resume_keywords: Set[str]) -> Dict[str, Any]:
    """Match a single job card against resume keywords.

    Uses the card-level fields (title, tags, salary, etc.) for matching.
    This is a lightweight match — full JD matching happens later via scrape_jd + LLM.
    """
    # Build text from available card fields
    title = job.get("title", "")
    tags = job.get("tags", [])
    company = job.get("company", "")
    job_text = f"{title} {company} {' '.join(tags)}"

    job_keywords = extract_keywords(job_text)
    matched = job_keywords & resume_keywords
    gaps = job_keywords - resume_keywords

    if len(matched) >= 5:
        score = "high"
    elif len(matched) >= 2:
        score = "medium"
    else:
        score = "low"

    return {
        # Preserve original job fields
        "title": title,
        "company": company,
        "salary": job.get("salary", ""),
        "city": job.get("city", ""),
        "experience": job.get("experience", ""),
        "degree": job.get("degree", ""),
        "tags": tags,
        "benefits": job.get("benefits", []),
        "recruiter": job.get("recruiter", ""),
        "recruiterTitle": job.get("recruiterTitle", ""),
        "link": job.get("link", ""),
        "source": job.get("source", "BOSS直聘"),
        # Match analysis
        "matchScore": score,
        "matchedKeywords": sorted(matched),
        "matchGaps": sorted(gaps),
    }


# ────────────────────────── Main ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Batch match and rank jobs against resume")
    ap.add_argument("--jobs", required=True, help="Path to jobs.json (search output)")
    ap.add_argument("--resume", default=DEFAULT_RESUME, help="Path to candidate resume")
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to boss_greeting.db for dedup")
    ap.add_argument("--output", required=True, help="Output path for ranked_jobs.json")
    ap.add_argument("--max-count", type=int, default=0, help="Max jobs to output (0 = unlimited)")
    ap.add_argument("--city", default="", help="Target city filter (e.g. '深圳'). Reject jobs from other cities.")
    ap.add_argument("--prefs", default=DEFAULT_PREFS, help="Path to candidate-preferences.json")
    args = ap.parse_args()

    # Load jobs
    jobs_path = Path(args.jobs)
    if not jobs_path.exists():
        print(f"[ERROR] jobs file not found: {jobs_path}", file=sys.stderr)
        return 1
    raw = json.loads(jobs_path.read_text(encoding="utf-8"))

    # Handle both array format and wrapped format {jobs: [...]}
    if isinstance(raw, list):
        jobs_list = raw
    elif isinstance(raw, dict):
        jobs_list = raw.get("jobs", [])
    else:
        print("[ERROR] unexpected jobs.json format", file=sys.stderr)
        return 1

    if not jobs_list:
        print("[WARN] no jobs found in input", file=sys.stderr)
        Path(args.output).write_text("[]", encoding="utf-8")
        return 0

    # Load resume
    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"[ERROR] resume not found: {resume_path}", file=sys.stderr)
        return 1
    resume_text = resume_path.read_text(encoding="utf-8")
    resume_keywords = extract_keywords(resume_text)
    print(f"[INFO] resume keywords: {len(resume_keywords)}", flush=True)

    # Load sent URLs and semantic fingerprints for dedup
    sent_urls = load_sent_urls(args.db)
    if sent_urls:
        print(f"[INFO] {len(sent_urls)} already-sent URLs in DB", flush=True)
    sent_fps = load_sent_fingerprints(args.db)
    if sent_fps:
        print(f"[INFO] {len(sent_fps)} semantic fingerprints in DB", flush=True)

    # Load candidate preferences for hard filtering and priority
    prefs = load_preferences(args.prefs)
    if prefs:
        print(f"[INFO] loaded candidate preferences: salary={prefs.get('salaryRange', {})}, city={prefs.get('targetCity', '')}", flush=True)

    # Get priority companies from preferences or use default
    priority_companies = prefs.get("priorityCompanies", DEFAULT_PRIORITY_COMPANIES)

    # Determine target city: CLI arg > preferences > empty
    target_city = args.city.strip() or (prefs.get("targetCity", "") or "").strip()

    # Match and rank with hard filtering
    ranked = []
    skipped_sent = 0
    skipped_hard: Dict[str, int] = {}  # reason → count

    for job in jobs_list:
        link = job.get("link", "")
        # Skip already-sent (URL dedup)
        if link and link in sent_urls:
            skipped_sent += 1
            continue
        # Skip already-sent (semantic fingerprint dedup: same company+title+recruiter)
        fp = _semantic_fingerprint(job.get("company"), job.get("title"), job.get("recruiter"))
        if fp and fp in sent_fps:
            skipped_sent += 1
            continue

        # Hard filter based on preferences (salary, city, internship, experience, language)
        reject_reason = hard_filter_job(job, prefs, target_city) if prefs else None
        if reject_reason:
            bucket = reject_reason.split(":")[0]
            skipped_hard[bucket] = skipped_hard.get(bucket, 0) + 1
            title = job.get("title", "?")[:40]
            print(f"[FILTER] {reject_reason} | {title}", flush=True)
            continue

        # Match (lightweight, card-level only)
        result = match_job(job, resume_keywords)

        # Calculate priority scores
        priority = calculate_priority(job, prefs, priority_companies)
        result["priorityBreakdown"] = {
            "company": priority["priority_company"],
            "experience": priority["priority_experience"],
            "salary": priority["priority_salary"],
            "total": priority["priority_total"],
            "isBigTech": priority["is_big_tech"],
        }

        ranked.append(result)

    if skipped_sent:
        print(f"[INFO] skipped {skipped_sent} already-sent jobs", flush=True)
    if skipped_hard:
        total_filtered = sum(skipped_hard.values())
        print(f"[INFO] hard-filtered {total_filtered} jobs: {dict(skipped_hard)}", flush=True)

    # Sort by priority: company > experience > salary > matchScore
    # Primary: priority_total DESC
    # Secondary: priority_company DESC (big tech first)
    # Tertiary: priority_experience DESC (less experience preferred)
    # Quaternary: priority_salary DESC (salary in range preferred)
    # Finally: matchScore
    def priority_sort_key(x):
        return (
            -x.get("priorityBreakdown", {}).get("total", 0),
            -x.get("priorityBreakdown", {}).get("company", 0),
            -x.get("priorityBreakdown", {}).get("experience", 0),
            -x.get("priorityBreakdown", {}).get("salary", 0),
            SCORE_ORDER.get(x.get("matchScore", "low"), 2),
        )

    ranked.sort(key=priority_sort_key)

    # Limit
    if args.max_count > 0:
        ranked = ranked[: args.max_count]

    # Write output
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")

    # Summary
    score_counts = {"high": 0, "medium": 0, "low": 0}
    big_tech_count = 0
    for r in ranked:
        score_counts[r["matchScore"]] = score_counts.get(r["matchScore"], 0) + 1
        if r.get("priorityBreakdown", {}).get("isBigTech"):
            big_tech_count += 1

    print(f"[OK] {len(ranked)} jobs ranked (high={score_counts['high']}, "
          f"medium={score_counts['medium']}, low={score_counts['low']})", flush=True)
    print(f"[OK] big-tech jobs: {big_tech_count}", flush=True)
    print(f"[OK] output: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
