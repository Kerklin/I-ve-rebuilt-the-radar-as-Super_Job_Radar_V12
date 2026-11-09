# AGENTS.md - rules for any AI working on Super Job Radar

## Project
`super_job_radar.py` reads `sources.json`, scores against `profile.json`, checks eligibility, writes `site/index.html` + `jobs.json` + `summary.md`.
Commands: `--test` (must pass) · `--check [name]` (live sources + repair hints) · `--why "title"` · `--audit` · `--install`.

## Rules
1. Never invent facts about Damir (degrees, dates, employers, numbers). Not documented → `[UNVERIFIED]`.
2. Keep 8 files. Standard library only. No AI or paid services in the scheduled run.
3. The workflow uses only `upload-pages-artifact@v5+` and `deploy-pages@v5+` on `ubuntu-24.04`. Run `--audit` after any workflow edit.
4. Never bypass robots.txt, logins, tokens you were not given, or anti-bot pages. Use an official API/feed or keep the source manual.
5. Never print or commit secrets (`RELIEFWEB_APPNAME`). Error messages are masked; keep it that way.
6. Wrong job shown/missing → add it to `test_cases.json` first (ask Damir), then change `profile.json`, then `--test`.
7. Repair a source only after 3 failed runs in a row (`fails_in_a_row`).
8. Ask before editing `super_job_radar.yml` or `test_cases.json` labels.

## Skills
- **vacancy-screen**: eligibility PASS/FAIL/UNCLEAR per hard requirement → fit /100 → gaps ✅/⚠️/❌ → APPLY ≥70 · CAUTION 55–69 · NO <55 → top 3 HR risks.
- **cv-tailor / cover-letter**: documented evidence only; ATS-safe; "Bosnian/Croatian/Serbian: Native"; never the "Bangui team" sentence; then REVIEW.
- **add-source**: Oracle → `oracle` (host, site CX_…); Workday → `workday`; Cornerstone → `cornerstone`; feed or list page → `feed_or_page` + `urls` + `job_link_contains`; test with `--check NAME`.
- **REVIEW**: every claim ✅/⚠️/❌, mandatory requirements, character limits → PASS / FIX FIRST.

## Lessons (V7–V11)
- Node 20 warning came from GitHub's built-in Pages workflow → Pages Source = GitHub Actions.
- ReliefWeb website/RSS = empty anti-bot page → API v2 + approved appname.
- World Bank (Cornerstone) 401 = session cookie not sent back → keep cookies (V12).
- GIZ 0 vacancies = titles not plain links → collect job IDs, read each job page (V12).
- UNjobs forbids robots. Menu links look like jobs → require an ID. "intern" ≠ "internal". EIB dates are DD/MM, Oracle MM/DD.

## Handoff (overwrite each session)
Date · Last task · State · Next step · Open questions
