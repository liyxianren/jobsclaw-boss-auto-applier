#!/usr/bin/env python3
"""Validate boss-zhipin-search input filters.

Accepts BOTH formats:
  - BOSS URL codes (recommended):  salary="405", experience="104", degree="203"
  - Human-readable (legacy):       salary="15-30K", experience="1-3年", degree="本科"
"""
import argparse
import json
import re
import sys
from pathlib import Path

ALLOWED_KEYS = {
    "keyword", "city", "salary", "experience", "degree",
    "jobType", "scale", "pageLimit", "minCount",
}

# ── BOSS URL code mappings ──
SALARY_CODES = {"401", "402", "403", "404", "405", "406", "407", "408", "409"}
EXPERIENCE_CODES = {"101", "102", "103", "104", "105", "106", "107", "108"}
DEGREE_CODES = {"201", "202", "203", "204", "205", "206", "207", "209"}
JOB_TYPE_CODES = {"1901", "1903", "1902"}

# ── Human-readable allowed values (legacy) ──
EXPERIENCE_READABLE = {
    "应届", "实习", "无经验", "经验不限",
    "1年以内", "1-3年", "3-5年", "5-10年", "10年以上",
}
DEGREE_READABLE = {
    "学历不限", "不限", "初中", "中专", "高中", "大专", "本科", "硕士", "博士",
}
JOB_TYPE_READABLE = {
    "全职", "实习", "兼职", "full-time", "part-time", "intern",
}


def is_code_list(value: str, valid_codes: set) -> bool:
    """Check if value is a comma-separated list of valid BOSS URL codes."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return all(p in valid_codes for p in parts) if parts else False


def is_salary(value: str) -> bool:
    v = value.strip()
    # URL code format: single code like "405" (禁止 "405,406")
    if is_code_list(v, SALARY_CODES):
        return True
    # Human-readable format: "15-30K"
    return bool(re.fullmatch(r"\d{1,3}\s*-\s*\d{1,3}[kK]", v))


def is_experience(value: str) -> bool:
    v = value.strip()
    if is_code_list(v, EXPERIENCE_CODES):
        return True
    if v in EXPERIENCE_READABLE:
        return True
    return bool(re.match(r"\d+-\d+年$", v))


def is_degree(value: str) -> bool:
    v = value.strip()
    if is_code_list(v, DEGREE_CODES):
        return True
    return v in DEGREE_READABLE


def is_job_type(value: str) -> bool:
    v = value.strip()
    if is_code_list(v, JOB_TYPE_CODES):
        return True
    return any(v.lower() == j.lower() for j in JOB_TYPE_READABLE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate boss-zhipin-search input filters")
    parser.add_argument("--input", required=True, help="Path to input JSON")
    args = parser.parse_args()

    p = Path(args.input)
    if not p.exists():
        print(f"[ERROR] input file not found: {p}")
        return 2

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] invalid json: {e}")
        return 2

    if not isinstance(data, dict):
        print("[ERROR] input must be a JSON object")
        return 2

    unknown = sorted(set(data.keys()) - ALLOWED_KEYS)
    if unknown:
        print(f"[WARN] unknown fields: {', '.join(unknown)}")

    if not data.get("keyword"):
        print("[ERROR] keyword is required")
        return 1

    # salary 校验
    salary = data.get("salary")
    if salary and not is_salary(str(salary)):
        print(f"[ERROR] salary='{salary}' invalid. Use BOSS URL code (e.g. '405') or range (e.g. '15-30K')")
        return 1

    # experience 校验
    experience = data.get("experience")
    if experience and not is_experience(str(experience)):
        print(f"[WARN] experience='{experience}' may be invalid. Use URL code (e.g. '104') or text (e.g. '1-3年')")

    # degree 校验
    degree = data.get("degree")
    if degree and not is_degree(str(degree)):
        print(f"[WARN] degree='{degree}' may be invalid. Use URL code (e.g. '203') or text (e.g. '本科')")

    # jobType 校验
    job_type = data.get("jobType")
    if job_type and not is_job_type(str(job_type)):
        print(f"[WARN] jobType='{job_type}' may be invalid. Use URL code (e.g. '1901') or text (e.g. '全职')")

    for k in ("pageLimit", "minCount"):
        if k in data:
            try:
                v = int(data[k])
            except Exception:
                print(f"[ERROR] {k} must be an integer")
                return 1
            if v <= 0:
                print(f"[ERROR] {k} must be > 0")
                return 1

    print("[OK] filters validated")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
