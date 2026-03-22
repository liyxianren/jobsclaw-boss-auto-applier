#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Candidate Pool Management CLI.

Manages a persistent pool of job candidates to avoid losing jobs due to maxApply*3 truncation.
Allows for priority-based consumption across multiple rounds.

Usage:
    # Ingest jobs into pool
    python3 candidate_pool.py ingest --jobs jobs.json --db pool.db --keyword "Agent" --city "深圳"

    # Pick candidates from pool (sorted by priority)
    python3 candidate_pool.py pick --db pool.db --city "深圳" --limit 10

    # Mark job status
    python3 candidate_pool.py mark --db pool.db --job-url "<url>" --status sent
    python3 candidate_pool.py mark --db pool.db --job-url "<url>" --status failed --error "Connection timeout"
    python3 candidate_pool.py mark --db pool.db --job-url "<url>" --status retry_pending --retry-after-min 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default priority company keywords
DEFAULT_PRIORITY_COMPANIES = [
    "腾讯", "Tencent", "字节", "ByteDance", "抖音", "TikTok",
    "百度", "Baidu", "阿里", "阿里云", "Alibaba", "Aliyun",
    "阿里巴巴", "蚂蚁", "Ant", "美团", "拼多多", "PDD",
    "京东", "JD", "快手", "Kwai", "B站", "Bilibili",
    "网易", "NetEase", "滴滴", "Didi", "小米", "Xiaomi",
]

DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "data/candidate_pool.db")
DEFAULT_PREFS = str(Path(__file__).resolve().parents[3] / "candidate-preferences.json")


# ────────────────────────── DB Management ──────────────────────────

def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Get DB connection, creating tables if needed."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create candidate_pool table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_pool (
            job_url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            city TEXT,
            experience TEXT,
            degree TEXT,
            source_keyword TEXT,
            status TEXT DEFAULT 'pending',
            priority_company REAL DEFAULT 0,
            priority_experience REAL DEFAULT 0,
            priority_salary REAL DEFAULT 0,
            priority_total REAL DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            retry_after TEXT,
            last_error TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            last_selected_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON candidate_pool(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_city ON candidate_pool(city)
    """)
    conn.commit()


def load_preferences(path: str) -> Dict[str, Any]:
    """Load candidate preferences."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_salary_k(salary_str: str) -> tuple:
    """Extract (min_k, max_k) from salary string."""
    import re
    if not salary_str:
        return None, None
    m = re.search(r"(\d{1,3})\s*[-~]\s*(\d{1,3})\s*K", salary_str, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def parse_experience_years(exp_str: str) -> Optional[int]:
    """Parse experience requirement to minimum years."""
    import re
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
) -> Dict[str, float]:
    """Calculate priority scores for a job."""
    company = job.get("company", "")
    experience = job.get("experience", "")
    salary = job.get("salary", "")

    # 1. Company priority (highest weight)
    priority_company = 0.0
    company_lower = company.lower()
    for kw in priority_companies:
        if kw.lower() in company_lower:
            priority_company = 100.0
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
        priority_experience = 0.0  # Too senior

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
    # Company has highest weight, then experience, then salary
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
    }


# ────────────────────────── Commands ──────────────────────────

def cmd_ingest(args) -> int:
    """Ingest jobs into candidate pool."""
    jobs_path = Path(args.jobs)
    if not jobs_path.exists():
        print(f"[ERROR] jobs file not found: {jobs_path}", file=sys.stderr)
        return 1

    raw = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs_list = raw.get("jobs", []) if isinstance(raw, dict) else raw

    if not jobs_list:
        print("[WARN] no jobs to ingest", file=sys.stderr)
        return 0

    # Load preferences for priority calculation
    prefs = load_preferences(args.prefs or DEFAULT_PREFS)
    priority_companies = prefs.get("priorityCompanies", DEFAULT_PRIORITY_COMPANIES)

    conn = get_db_connection(args.db)
    now = datetime.now().isoformat()

    ingested = 0
    updated = 0

    for job in jobs_list:
        link = job.get("link", "") or job.get("job_url", "") or job.get("jobUrl", "")
        if not link:
            continue

        title = job.get("title", "")
        company = job.get("company", "")
        salary = job.get("salary", "")
        city = job.get("city", "")
        experience = job.get("experience", "")
        degree = job.get("degree", "")

        # Calculate priority
        priority = calculate_priority(job, prefs, priority_companies)

        # Check if exists
        existing = conn.execute(
            "SELECT job_url FROM candidate_pool WHERE job_url = ?", (link,)
        ).fetchone()

        if existing:
            # Update existing record
            conn.execute("""
                UPDATE candidate_pool SET
                    title = ?, company = ?, salary = ?, city = ?, experience = ?, degree = ?,
                    source_keyword = ?, last_seen_at = ?, priority_company = ?, priority_experience = ?,
                    priority_salary = ?, priority_total = ?, updated_at = ?
                WHERE job_url = ?
            """, (title, company, salary, city, experience, degree,
                  args.keyword, now,
                  priority["priority_company"], priority["priority_experience"],
                  priority["priority_salary"], priority["priority_total"],
                  now, link))
            updated += 1
        else:
            # Insert new record
            conn.execute("""
                INSERT INTO candidate_pool (
                    job_url, title, company, salary, city, experience, degree,
                    source_keyword, status, priority_company, priority_experience,
                    priority_salary, priority_total,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (link, title, company, salary, city, experience, degree,
                  args.keyword,
                  priority["priority_company"], priority["priority_experience"],
                  priority["priority_salary"], priority["priority_total"],
                  now, now, now, now))
            ingested += 1

    conn.commit()
    conn.close()

    print(f"[OK] ingested={ingested}, updated={updated}, total={len(jobs_list)}")
    return 0


def cmd_pick(args) -> int:
    """Pick candidates from pool, sorted by priority."""
    conn = get_db_connection(args.db)
    now = datetime.now().isoformat()

    # Build query
    query = """
        SELECT * FROM candidate_pool
        WHERE status IN ('pending', 'retry_pending')
    """
    params = []

    if args.city:
        query += " AND city LIKE ?"
        params.append(f"%{args.city}%")

    if not args.exclude_sent:
        # Also include jobs that were previously selected but not marked as sent
        pass

    query += " ORDER BY priority_total DESC, priority_company DESC, priority_experience DESC"

    if args.limit:
        query += f" LIMIT {args.limit}"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("[]")
        return 0

    # Convert to list of dicts
    results = []
    for row in rows:
        results.append({
            "job_url": row["job_url"],
            "title": row["title"],
            "company": row["company"],
            "salary": row["salary"],
            "city": row["city"],
            "experience": row["experience"],
            "degree": row["degree"],
            "source_keyword": row["source_keyword"],
            "status": row["status"],
            "priority_company": row["priority_company"],
            "priority_experience": row["priority_experience"],
            "priority_salary": row["priority_salary"],
            "priority_total": row["priority_total"],
            "retry_count": row["retry_count"],
            "retry_after": row["retry_after"],
            "last_error": row["last_error"],
        })

    # Update last_selected_at for picked jobs
    conn = get_db_connection(args.db)
    for r in results:
        conn.execute(
            "UPDATE candidate_pool SET last_selected_at = ? WHERE job_url = ?",
            (now, r["job_url"])
        )
    conn.commit()
    conn.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_mark(args) -> int:
    """Mark job status in pool."""
    conn = get_db_connection(args.db)
    now = datetime.now().isoformat()

    # Build update fields
    updates = ["status = ?", "updated_at = ?"]
    params = [args.status, now]

    if args.error:
        updates.append("last_error = ?")
        params.append(args.error)

    if args.retry_after_min:
        retry_after = (datetime.now() + timedelta(minutes=args.retry_after_min)).isoformat()
        updates.append("retry_after = ?")
        params.append(retry_after)
        # Increment retry count
        conn.execute(
            "UPDATE candidate_pool SET retry_count = retry_count + 1 WHERE job_url = ?",
            (args.job_url,)
        )

    params.append(args.job_url)

    query = f"UPDATE candidate_pool SET {', '.join(updates)} WHERE job_url = ?"
    conn.execute(query, params)
    conn.commit()

    # Verify update
    row = conn.execute(
        "SELECT * FROM candidate_pool WHERE job_url = ?",
        (args.job_url,)
    ).fetchone()
    conn.close()

    if row:
        print(f"[OK] marked {args.job_url} as {args.status}")
        return 0
    else:
        print(f"[ERROR] job not found: {args.job_url}", file=sys.stderr)
        return 1


def cmd_list(args) -> int:
    """List jobs in pool with optional filters."""
    conn = get_db_connection(args.db)

    query = "SELECT * FROM candidate_pool"
    conditions = []
    params = []

    if args.status:
        conditions.append("status = ?")
        params.append(args.status)

    if args.city:
        conditions.append("city LIKE ?")
        params.append(f"%{args.city}%")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY priority_total DESC"

    if args.limit:
        query += f" LIMIT {args.limit}"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for row in rows:
        results.append({
            "job_url": row["job_url"],
            "title": row["title"],
            "company": row["company"],
            "salary": row["salary"],
            "city": row["city"],
            "status": row["status"],
            "priority_total": row["priority_total"],
        })

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    """Show pool statistics."""
    conn = get_db_connection(args.db)

    # Status counts
    status_counts = conn.execute("""
        SELECT status, COUNT(*) as cnt FROM candidate_pool GROUP BY status
    """).fetchall()

    # Priority distribution
    priority_dist = conn.execute("""
        SELECT
            CASE
                WHEN priority_total >= 80 THEN 'high'
                WHEN priority_total >= 50 THEN 'medium'
                ELSE 'low'
            END as priority_bucket,
            COUNT(*) as cnt
        FROM candidate_pool
        GROUP BY priority_bucket
    """).fetchall()

    # Total
    total = conn.execute("SELECT COUNT(*) FROM candidate_pool").fetchone()[0]

    conn.close()

    result = {
        "total": total,
        "by_status": {row["status"]: row["cnt"] for row in status_counts},
        "by_priority": {row["priority_bucket"]: row["cnt"] for row in priority_dist},
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ────────────────────────── Main ──────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate Pool Management")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    ing = subparsers.add_parser("ingest", help="Ingest jobs into pool")
    ing.add_argument("--jobs", required=True, help="Path to jobs.json")
    ing.add_argument("--db", default=DEFAULT_DB, help="Path to pool DB")
    ing.add_argument("--keyword", required=True, help="Source keyword")
    ing.add_argument("--city", default="", help="Target city")
    ing.add_argument("--prefs", default=DEFAULT_PREFS, help="Path to preferences")

    # pick
    pick = subparsers.add_parser("pick", help="Pick candidates from pool")
    pick.add_argument("--db", default=DEFAULT_DB, help="Path to pool DB")
    pick.add_argument("--city", default="", help="Filter by city")
    pick.add_argument("--limit", type=int, default=0, help="Max candidates to pick")
    pick.add_argument("--exclude-sent", action="store_true", help="Exclude already sent")

    # mark
    mark = subparsers.add_parser("mark", help="Mark job status")
    mark.add_argument("--db", default=DEFAULT_DB, help="Path to pool DB")
    mark.add_argument("--job-url", required=True, help="Job URL to mark")
    mark.add_argument("--status", required=True, choices=["pending", "sent", "failed", "retry_pending", "skipped"], help="New status")
    mark.add_argument("--error", default="", help="Error message if failed")
    mark.add_argument("--retry-after-min", type=int, help="Retry after N minutes")

    # list
    lst = subparsers.add_parser("list", help="List jobs in pool")
    lst.add_argument("--db", default=DEFAULT_DB, help="Path to pool DB")
    lst.add_argument("--status", help="Filter by status")
    lst.add_argument("--city", help="Filter by city")
    lst.add_argument("--limit", type=int, default=0, help="Limit results")

    # stats
    stats = subparsers.add_parser("stats", help="Show pool statistics")
    stats.add_argument("--db", default=DEFAULT_DB, help="Path to pool DB")

    args = parser.parse_args()

    commands = {
        "ingest": cmd_ingest,
        "pick": cmd_pick,
        "mark": cmd_mark,
        "list": cmd_list,
        "stats": cmd_stats,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
