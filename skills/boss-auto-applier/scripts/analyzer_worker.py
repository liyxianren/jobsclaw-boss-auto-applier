#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyzer Worker（仅分析，不发送，不写 DB）。

输入：
  --jd-json <path>
  --resume <path>
  --prefs <path>
  --out <path>

输出 JSON：
  {
    "fit": bool,
    "matchScore": "高|中|低",
    "skipReason": str|None,
    "reasoning": str,
    "messageDraft": str
  }
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE = Path(__file__).resolve().parents[3]
SCRIPTS = {
    "match": WORKSPACE / "skills/jd-greeting-generator/scripts/match_resume.py",
    "gen_msg": WORKSPACE / "skills/jd-greeting-generator/scripts/generate_message.py",
}


def sh(cmd: List[str], *, timeout: Optional[int] = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=check)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def parse_salary_k(s: str) -> Optional[Tuple[int, int]]:
    if not s:
        return None
    m = re.search(r"(\d{1,3})\s*-\s*(\d{1,3})\s*K", s, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def min_required_years(exp: str) -> Optional[int]:
    if not exp:
        return None
    s = str(exp).strip()
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*年", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*年\s*以上", s)
    if m:
        return int(m.group(1))
    if "不限" in s:
        return None
    return None


def contains_any(text: str, keywords: List[str], *, case_sensitive: bool = True) -> Optional[str]:
    t = text or ""
    for k in keywords:
        if not k:
            continue
        if case_sensitive:
            if k in t:
                return k
        else:
            # 大小写不敏感模式：用单词边界匹配，避免 "Java" 匹配 "JavaScript"、"Go" 匹配 "Google"
            if re.search(r'(?<![a-zA-Z])' + re.escape(k) + r'(?![a-zA-Z])', t, re.IGNORECASE):
                return k
    return None


def normalize_match_score(ms: Optional[str]) -> str:
    if not ms:
        return "低"
    ms = str(ms).strip().lower()
    if ms in {"high", "高"}:
        return "高"
    if ms in {"medium", "中"}:
        return "中"
    return "低"


def collect_text_fields(jd: Dict[str, Any]) -> str:
    parts = [
        jd.get("jobTitle", ""),
        jd.get("company", ""),
        jd.get("salary", ""),
        jd.get("experience", ""),
        jd.get("degree", ""),
        jd.get("description", ""),
        " ".join(jd.get("tags") or []),
    ]
    return "\n".join([p for p in parts if p])


def compute_match_points(jd_text: str, pref: Dict[str, Any]) -> Tuple[float, List[str]]:
    cand = pref.get("profile", {})
    core = cand.get("coreTech", []) + cand.get("strengths", [])
    core_text = " ".join(core)

    def has(term: str) -> bool:
        return term in jd_text

    points = 0.0
    evidence: List[str] = []

    strong_terms = [
        ("Agent", ["Agent", "智能体", "agent"]),
        ("RAG", ["RAG", "检索增强", "知识库", "向量检索", "Embedding"]),
        ("OpenClaw/编排", ["OpenClaw", "编排", "工作流", "状态机", "workflow"]),
        ("Python后端", ["Python", "Flask", "FastAPI", "后端", "接口"]),
        ("浏览器自动化", ["CDP", "Playwright", "Selenium", "浏览器自动化"]),
        ("平台集成", ["API", "集成", "对接", "多平台"]),
    ]

    for _label, terms in strong_terms:
        if any(has(t) for t in terms) and any(t in core_text for t in ["Agent", "智能体", "RAG", "OpenClaw", "状态机", "Python", "Flask", "CDP", "浏览器"]):
            points += 1.0
            for t in terms:
                if has(t):
                    evidence.append(t)
                    break

    if any(has(t) for t in ["LangChain", "langchain"]) and any(x in core_text for x in ["Dify", "Coze", "Agent", "智能体"]):
        points += 0.5
        evidence.append("LangChain")

    evidence = list(dict.fromkeys(evidence))[:6]
    return points, evidence


def evaluate_fit(jd: Dict[str, Any], match_data: Dict[str, Any], pref: Dict[str, Any]) -> Dict[str, Any]:
    jd_text = collect_text_fields(jd)
    title = jd.get("jobTitle") or ""
    hard = pref.get("hardReject", {})

    salary = jd.get("salary") or ""
    salary_k = parse_salary_k(salary)
    if salary_k and salary_k[0] >= 30:
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"薪资范围{salary}（下限≥30K）超出目标，且岗位通常对应更高年限/层级；不投。关键词：{salary}，{title or '岗位'}",
            "skipReason": "salary_too_high",
        }

    if contains_any(title + "\n" + jd_text, hard.get("keywords", [])):
        kw = contains_any(title + "\n" + jd_text, hard.get("keywords", [])) or "实习"
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"岗位包含「{kw}」等实习/校招信号，不符合本轮投递策略。关键词：{kw}，{title or '岗位'}",
            "skipReason": "intern_or_campus",
        }

    if contains_any(salary + "\n" + jd_text, hard.get("salaryPatterns", [])):
        kw = contains_any(salary + "\n" + jd_text, hard.get("salaryPatterns", [])) or "元/天"
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"薪资呈现「{kw}」的日薪/月薪制，不符合预期。关键词：{kw}，{salary or '薪资未知'}",
            "skipReason": "salary_pattern",
        }

    reject_langs = hard.get("languages", [])
    # 先检查标题（大小写不敏感 + 单词边界），标题含语言名直接拒绝
    title_lang = contains_any(title, reject_langs, case_sensitive=False)
    if title_lang:
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"岗位标题包含「{title_lang}」技术栈，与候选人以 Python/Agent/RAG 为主不匹配。关键词：{title_lang}，{title or '岗位'}",
            "skipReason": "language_mismatch",
        }
    # 再检查 JD 全文
    lang_kw = contains_any(jd_text, reject_langs, case_sensitive=False)
    if lang_kw:
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"JD 明确偏「{lang_kw}」技术栈（如{lang_kw}开发/工程师），与候选人以 Python/Agent/RAG 为主不匹配。关键词：{lang_kw}，{title or '岗位'}",
            "skipReason": "language_mismatch",
        }

    exp_min = min_required_years(jd.get("experience") or "")
    if exp_min is not None and exp_min >= 3:
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"经验要求为「{jd.get('experience')}」，最低≥{exp_min}年，与候选人约1年经验差距过大。关键词：{jd.get('experience')}，{title or '岗位'}",
            "skipReason": "experience_gap",
        }

    if re.search(r"算法|研究员|模型训练|预训练|微调|RLHF|论文|CV算法|NLP算法", title + "\n" + jd_text):
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"岗位更偏算法/训练/研究（如「算法/模型训练/微调」等），与候选人定位的AI应用开发/Agent落地不一致。关键词：算法，{title or '岗位'}",
            "skipReason": "direction_mismatch",
        }

    deg_field = (jd.get("degree") or "").strip()
    if re.search(r"硕士|研究生", deg_field) or re.search(r"985/211|211|985", deg_field):
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"学历要求为「{deg_field}」，候选人学历为本科（专升本），大概率系统筛掉。关键词：{deg_field}，{title or '岗位'}",
            "skipReason": "degree_hard",
        }

    mismatch_terms = ["嵌入式", "DSP", "驱动", "硬件", "电路", "电气", "销售", "运维", "测试"]
    hit = next((t for t in mismatch_terms if t in (title or "")), None)
    if hit:
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"岗位标题包含「{hit}」，方向偏离AI应用开发（Python/Agent/RAG），先跳过。关键词：{hit}，{title or '岗位'}",
            "skipReason": "direction_mismatch",
        }

    points, evidence = compute_match_points(jd_text, pref)

    mkw = match_data.get("matchedKeywords") or []
    generic = {"AI", "大模型", "模型", "LLM", "GPT", "NLP", "CV", "视觉", "算法"}
    mkw2 = [k for k in mkw if isinstance(k, str) and len(k.strip()) >= 2 and k.strip() not in generic]
    points += min(len(mkw2) / 4.0, 2.0)

    if points >= 4:
        ms = "高"
    elif points >= 2:
        ms = "中"
    else:
        ms = "低"

    if ms == "低":
        ev = evidence[:2]
        if len(ev) < 2:
            candidates = []
            for t in ["嵌入式", "DSP", "驱动", "硬件", "前端", "运维", "销售", "C++", "Java", "Go", "Python", "Agent", "RAG"]:
                if t in jd_text:
                    candidates.append(t)
            ev = (ev + candidates)[:2]
        ev = ev or [title[:10] or "JD", "岗位"]
        ev = (ev + ev)[:2]
        return {
            "fit": False,
            "matchScore": "低",
            "reasoning": f"从 JD 里能对齐的关键点不足（<2个），更偏「{ev[0]} / {ev[1]}」方向，难以体现候选人的 Agent/RAG/后端优势，先跳过。关键词：{ev[0]}，{ev[1]}",
            "skipReason": "low_match",
        }

    ev = evidence[:3]
    if len(ev) < 2:
        mk = match_data.get("matchedKeywords") or []
        ev = (ev + mk)[:3]
    ev2 = (ev + ev)[:2]
    return {
        "fit": True,
        "matchScore": ms,
        "reasoning": f"JD 里包含「{ev2[0]}」「{ev2[1]}」等具体要求，且候选人有 Python/Agent 编排与 RAG/工作流落地经验，可快速对齐并交付。关键词：{ev2[0]}，{ev2[1]}",
        "skipReason": None,
    }


def run_match(jd_path: Path, resume_path: Path) -> Dict[str, Any]:
    cp = sh([
        sys.executable,
        str(SCRIPTS["match"]),
        "--jd-file",
        str(jd_path),
        "--resume",
        str(resume_path),
    ])
    out = cp.stdout.strip()
    try:
        return extract_json_from_mixed_output(out)
    except Exception:
        return {"matchScore": "low", "matchedKeywords": [], "_raw": out[-2000:]}


def run_generate_message(jd_path: Path, match_data: Dict[str, Any], resume_path: Path, style: str = "professional") -> str:
    match_json = json.dumps(match_data, ensure_ascii=False)
    cp = sh([
        sys.executable,
        str(SCRIPTS["gen_msg"]),
        "--jd-file",
        str(jd_path),
        "--match-json",
        match_json,
        "--resume",
        str(resume_path),
        "--style",
        style,
        "--json",
    ])
    out = cp.stdout.strip()
    try:
        data = extract_json_from_mixed_output(out)
        return (data or {}).get("message") or ""
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyzer worker (analysis-only)")
    ap.add_argument("--jd-json", required=True, help="Path to JD JSON")
    ap.add_argument("--resume", required=True, help="Path to resume markdown")
    ap.add_argument("--prefs", required=True, help="Path to candidate preferences JSON")
    ap.add_argument("--out", required=True, help="Output path for analyzer JSON")
    args = ap.parse_args()

    jd_path = Path(args.jd_json)
    resume_path = Path(args.resume)
    prefs_path = Path(args.prefs)
    out_path = Path(args.out)

    try:
        jd_data = load_json(jd_path)
        prefs = load_json(prefs_path)

        if not isinstance(jd_data, dict):
            raise ValueError("invalid jd json")

        if jd_data.get("status") and jd_data.get("status") != "ok":
            result = {
                "fit": False,
                "matchScore": "低",
                "skipReason": "jd_not_ready",
                "reasoning": f"JD 抓取状态异常：{jd_data.get('status')}",
                "messageDraft": "",
            }
            dump_json(out_path, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        match_data = run_match(jd_path, resume_path)
        ev = evaluate_fit(jd_data, match_data, prefs)

        fit = bool(ev.get("fit"))
        match_score = ev.get("matchScore") or normalize_match_score(match_data.get("matchScore"))
        reasoning = ev.get("reasoning") or ("fit=true" if fit else "fit=false")
        skip_reason = ev.get("skipReason") if not fit else None

        message_draft = ""
        if fit:
            message_draft = run_generate_message(jd_path, {**match_data, "matchScore": match_score}, resume_path)
            if not message_draft:
                fit = False
                match_score = "低"
                skip_reason = "message_generation_failed"
                reasoning = "消息草稿生成失败，跳过发送。"

        result = {
            "fit": fit,
            "matchScore": match_score,
            "skipReason": skip_reason,
            "reasoning": reasoning,
            "messageDraft": message_draft,
        }
        dump_json(out_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        result = {
            "fit": False,
            "matchScore": "低",
            "skipReason": "analyzer_exception",
            "reasoning": f"analyzer exception: {exc}",
            "messageDraft": "",
        }
        dump_json(out_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
