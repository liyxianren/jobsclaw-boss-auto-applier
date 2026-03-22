# AGENTS.md

This repository contains a BOSS直聘 auto-apply workflow that is split into skills and scripts.

## Main Route
- User intent: 投递 / 投简历 / 自动投递 / 批量投递
- Main entry: `skills/boss-auto-applier/SKILL.md`

## Canonical Responsibility Split
- Scripts own browser startup, DOM extraction, page recovery, retry, send, and reconciliation.
- The model only reads the resume and JD files, then writes fit decisions and greeting copy.
- Browser state is expected on Chrome CDP `18801` with the BOSS profile.

## Included Skills
- `boss-auto-applier`: top-level route and end-to-end orchestration
- `boss-job-searcher`: Stage 1, search + rank + JD scrape
- `boss-job-analyzer`: Stage 2, fit evaluation + greeting generation
- `boss-job-sender`: Stage 3, send + reconcile
- `boss-zhipin-search`: low-level search bundle
- `boss-jd-evaluator`: fit-only evaluator
- `boss-greeting-sender`: send-only wrapper
- `jd-greeting-generator`: JD scrape + greeting helpers

## Notes
- This export excludes private runtime data, real databases, and historical run artifacts.
- `candidate-resume.md` and `candidate-preferences.json` are templates.
- `README.md` is the public quick-start overview.
- `docs/HANDOFF.md` is the detailed maintainer handoff guide.
