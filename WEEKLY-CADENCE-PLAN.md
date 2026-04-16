# Weekly Cadence Implementation Plan

## Context
DPNS currently sends a daily weekday digest. We're switching to a single weekly send on **Monday 09:00 CET**. The plan source is `WEEKLY_CADENCE_PLAN.md` (in the `check+weekly-cadence` worktree). This is a configuration/copy/test change — no new code paths, no schema migrations.

All changes are in the current worktree: `/Users/juanfpinzon/python_projects/DP_news_scout/.claude/worktrees/agitated-meitner-9ca335`

---

## Changes

### 1. [config/settings.yaml](config/settings.yaml)
Widen dedup windows from 7 → 14 days to provide a safe overlap buffer across weekly runs.
- Line 20: `dedup_window_days: 7` → `dedup_window_days: 14`
- Line 26: `reuse_seen_db_window_days: 7` → `reuse_seen_db_window_days: 14`

### 2. [.github/workflows/daily_digest.yml](.github/workflows/daily_digest.yml)
Update display name only; filename and `repository_dispatch` event type stay the same (cron-job.org URL/payload unchanged).
- Line 1: `name: Daily Digest` → `name: Weekly Digest`
- Line 19: `group: dpns-daily-digest` → `group: dpns-weekly-digest`

### 3. [prompts/relevance_scoring.md](prompts/relevance_scoring.md)
- Line 3: `"daily executive digest"` → `"weekly executive digest"`
- Lines 19–25 (Freshness Bias): replace `"weekday morning digest … last 24-72 hours"` with: `"This is a Monday-morning weekly briefing. Favor clearly dated developments from the past 7 days, with slight bias toward the last 2–3 days when relevance is tied."`

### 4. [prompts/global_news_scoring.md](prompts/global_news_scoring.md)
- Line 3: `"daily executive digest"` → `"weekly executive digest"`
- Lines 17–23 (Freshness Bias): replace `"weekday morning briefing … last 24-72 hours"` with: `"This is a Monday-morning weekly briefing. Favor clearly dated developments from the past 7 days, with slight bias toward the last 2–3 days when importance is comparable."`

### 5. [src/main.py](src/main.py) — `_build_no_news_email()` (lines 845, 852)
- Both occurrences of `"cleared the relevance threshold today"` → `"cleared the relevance threshold this week"`

### 6. [src/renderer/plaintext.py](src/renderer/plaintext.py) — line 1
- `"daily digest email"` → `"weekly digest email"`

### 7. Test fixtures — `dedup_window_days: 7` → `14`
All changes are fixture dict values only, no logic changes.

| File | Lines |
|------|-------|
| [tests/test_config.py](tests/test_config.py) | 26 (assertion), 138, 183, 225, 265 (fixture dicts) |
| [tests/test_digest.py](tests/test_digest.py) | 75–76 |
| [tests/test_global_briefing.py](tests/test_global_briefing.py) | 72–73 |
| [tests/test_main.py](tests/test_main.py) | 116–117 |
| [tests/test_analyzer.py](tests/test_analyzer.py) | 120–121 |
| [tests/test_pipeline.py](tests/test_pipeline.py) | 1111–1112 |
| [tests/test_relevance.py](tests/test_relevance.py) | 68–69 |
| [tests/test_sender.py](tests/test_sender.py) | 206–207 |
| [tests/test_fetcher.py](tests/test_fetcher.py) | 1656–1657 |
| [tests/test_search_fallback.py](tests/test_search_fallback.py) | 642–643 |
| [tests/test_run_manual.py](tests/test_run_manual.py) | 456–457 |

Also update `reuse_seen_db_window_days: 7` → `14` in any fixture that sets it explicitly.

### 8. Docs
- [CLAUDE.md](CLAUDE.md) line 5: `"daily automated email digest"` / `"every weekday at 9:00 AM CET"` → `"weekly automated email digest"` / `"every Monday at 9:00 AM CET"`
- [AGENTS.md](AGENTS.md) line 4: same two substitutions
- [README.md](README.md): update any `"daily"` / `"weekday"` send references in the intro

---

## Verification
1. `pytest tests/ -q` — all tests pass (especially `test_config.py` dedup assertions, `test_fetcher.py` dedup logic)
2. `python scripts/run_manual.py --dry-run` — articles fetched > 0, no send
3. `python scripts/run_manual.py --preview` — HTML renders cleanly with 12–15 items
4. `python scripts/run_manual.py --test-email juancho704@gmail.com` — test email delivered

**External step (operator, not code):** Update cron-job.org from weekday 08:30 to every Monday 08:30 CET.
