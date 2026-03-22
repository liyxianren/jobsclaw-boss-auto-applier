#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple


def dedupe(jobs: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for j in jobs:
        key = (str(j.get("title", "")).strip(), str(j.get("company", "")).strip(), str(j.get("link", "")).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def to_md(jobs: List[Dict], min_count: int) -> str:
    lines = [
        "# BOSS直聘岗位汇总",
        "",
        f"- 抓取条数：{len(jobs)}",
        f"- 目标条数：{min_count}",
        f"- 状态：{'✅ 达标' if len(jobs) >= min_count else '⚠️ 未达标'}",
        "",
        "## 岗位列表",
        "",
    ]

    for i, j in enumerate(jobs, start=1):
        title = j.get("title", "")
        company = j.get("company", "")
        salary = j.get("salary", "")
        city = j.get("city", "")
        exp = j.get("experience", "")
        degree = j.get("degree", "")
        link = j.get("link", "")
        tags = j.get("tags", []) or []

        lines.append(f"### {i}. {title}")
        lines.append(f"- 公司：{company}")
        lines.append(f"- 薪资：{salary}")
        lines.append(f"- 城市：{city}")
        lines.append(f"- 经验：{exp}")
        lines.append(f"- 学历：{degree}")
        lines.append(f"- 标签：{', '.join(tags) if tags else '-'}")
        lines.append(f"- 链接：{link}")
        lines.append("")

    return "\n".join(lines)


def load_jobs(path: Path) -> List[Dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    raise ValueError("input must be JSON array or object with key 'jobs'")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize boss zhipin jobs to JSON+Markdown")
    parser.add_argument("--input", required=True, help="Input JSON path")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--min-count", type=int, default=20, help="Minimum expected job count")
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    jobs = load_jobs(input_path)
    jobs = dedupe(jobs)

    structured = {
        "source": "BOSS直聘",
        "count": len(jobs),
        "minCount": args.min_count,
        "metRequirement": len(jobs) >= args.min_count,
        "jobs": jobs,
    }

    (outdir / "results.json").write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "summary.md").write_text(to_md(jobs, args.min_count), encoding="utf-8")

    print(f"[OK] wrote: {outdir / 'results.json'}")
    print(f"[OK] wrote: {outdir / 'summary.md'}")
    if len(jobs) < args.min_count:
        print("[WARN] job count below target min-count")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
