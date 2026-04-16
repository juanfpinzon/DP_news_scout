# CLAUDE.md — Digital Procurement News Scout (DPNS)

## Project Summary

DPNS is a weekly automated email digest that currently monitors 16 configured procurement and digital transformation sources, uses Claude to score relevance and compose executive summaries, and delivers a curated briefing to Digital Procurement senior leaders at PepsiCo every Monday at 9:00 AM CET.

`AGENTS.md` is the canonical current implementation guide. This file is retained for compatibility and now mirrors the current high-level runtime defaults.

Full context lives in:
- **[PRD.md](PRD.md)** — product requirements, content strategy, email design spec, success metrics
- **[PLAN.md](PLAN.md)** — phased implementation plan, task breakdown, dependency graph

---

## Architecture

Four-stage sequential pipeline triggered in production by `cron-job.org` calling the GitHub Actions workflow via `repository_dispatch`. Manual GitHub testing uses `workflow_dispatch`.

```
Fetcher → Analyzer → Renderer → Sender
```

| Stage | Module | Responsibility |
|-------|--------|----------------|
| Fetcher | `src/fetcher/` | RSS + web scraping, dedup, SQLite persistence |
| Analyzer | `src/analyzer/` | LLM relevance scoring + digest composition |
| Renderer | `src/renderer/` | HTML email (Jinja2 + premailer) + plain-text fallback |
| Sender | `src/sender/` | AgentMail API delivery with retry logic |

Entry point: `python -m src.main`

---

## Tech Stack

- **Python 3.11+**
- **httpx** (async HTTP) + **feedparser** (RSS) + **beautifulsoup4** (HTML scraping)
- **openai** SDK via OpenRouter (`base_url="https://openrouter.ai/api/v1"`) — single LLM gateway for all model calls
- **Jinja2** + **premailer** (email template + CSS inlining)
- **AgentMail** (email delivery — `pip install agentmail`)
- **SQLite** via the Python `sqlite3` storage layer (article storage, run logs, delivery records)
- **structlog** (structured logging)
- **pyyaml** + **python-dotenv** (config)
- **GitHub Actions** (workflow runner for manual and external-dispatch runs)
- **cron-job.org** (production scheduler)

---

## Project Structure

```
dpns/
├── AGENTS.md                   # Canonical implementation reference (read for deep dives)
├── CLAUDE.md                   # This file
├── DESIGN.md                   # Visual / UX design decisions
├── PRD.md                      # Product requirements
├── PLAN.md                     # Implementation plan
├── PLAN_SearchFallback.md      # Search-fallback feature plan
├── PLAN_GLOBAL_MACRO_BRIEFING.md # Global macro briefing feature plan
├── README.md
├── pyproject.toml
├── .env.example
├── data/
│   └── dpns.db                 # SQLite runtime DB (gitignored)
├── config/
│   ├── sources.yaml            # Source registry (URL, tier, method, selectors)
│   ├── search_fallback_allowlist.yaml # Trusted publisher allowlist for Brave fallback
│   ├── recipients.yaml         # Email distribution list
│   └── settings.yaml           # Pipeline tuning (thresholds, limits, schedule)
├── prompts/
│   ├── context_preamble.md     # Shared LLM context about the team/org
│   ├── relevance_scoring.md    # Rubric for 1–10 relevance scoring
│   ├── digest_composition.md   # Format/tone instructions for digest output
│   ├── global_news_scoring.md  # Scoring rubric for global macro briefing
│   └── global_briefing_composition.md  # Format/tone for global briefing
├── src/
│   ├── main.py                 # Pipeline orchestrator
│   ├── fetcher/                # RSS, scraping, dedup, registry
│   │   ├── search_fallback.py  # Brave-backed fallback discovery + allowlist gate
│   ├── analyzer/               # LLM client, relevance scoring, digest composition
│   ├── renderer/               # HTML + plain-text email builders
│   ├── sender/                 # AgentMail integration
│   ├── storage/                # SQLite schema and helpers
│   └── utils/                  # Config loader, structured logging
├── templates/
│   └── digest_email.html       # Jinja2 email template (compiled output)
├── tests/
│   ├── conftest.py
│   ├── test_fetcher.py / test_analyzer.py / test_renderer.py / test_pipeline.py
│   ├── test_relevance.py / test_digest.py / test_global_briefing.py
│   ├── test_search_fallback.py / test_seed_sources.py
│   ├── test_config.py / test_db.py / test_prompts.py
│   └── test_main.py / test_run_manual.py / test_sender.py / test_test_email.py
├── scripts/
│   ├── run_manual.py           # CLI manual trigger (--dry-run, --preview, etc.)
│   ├── test_email.py           # Send a test email with mock data
│   └── seed_sources.py         # Validate all source URLs in sources.yaml
└── .github/workflows/
    └── daily_digest.yml        # Manual + repository_dispatch workflow entrypoint
```

---

## Key Design Decisions

### LLM Usage
- **Two-pass LLM pipeline**: first call scores relevance (batch of ~10 articles → JSON scores), second call composes the full digest (top ~15 articles → structured JSON output).
- **Provider: OpenRouter** — all LLM calls go through OpenRouter using the `openai` SDK with `base_url="https://openrouter.ai/api/v1"`.
- **Stage-specific defaults**:
  - relevance scoring: `anthropic/claude-haiku-4.5`
  - digest composition: `anthropic/claude-sonnet-4-6`
  - shared fallback: `anthropic/claude-haiku-4.5`
- Legacy `llm_model` / `LLM_MODEL` is still supported as a compatibility alias and will populate both stages if stage-specific settings are absent.
- All prompts live in `/prompts/` as Markdown files — editable by non-developers without touching code.
- System prompt always includes `context_preamble.md` which encodes PepsiCo Digital Procurement context.

### Content Sources
- Sources are tiered (Tier 1 = must-fetch, Tier 2 = supplemental, Tier 3 = conditional). Adding/removing sources requires no code change — edit `config/sources.yaml` only (F-08).
- RSS-first strategy; web scraping only where RSS is unavailable. Live freshness window: 7 days.
- `robots.txt` is deny-by-default: the fetcher checks it before every RSS/scrape fetch and will not proceed unless the file is confirmed missing (`404/410`) or permits access.
- Search fallback (`SEARCH_FALLBACK_ENABLED=true`) activates after a direct fetch fails or returns 0 recent articles. Only publishers in `config/search_fallback_allowlist.yaml` are accepted — edit this file to add trusted trade-media domains.

> For full details on fallback retry logic, per-source query tuning, and robots.txt edge cases, see `AGENTS.md`.

### Email Design
- Current desktop max width: 880px, table-based layout for Outlook compatibility.
- Color palette: deep navy header/footer `#1a2332`, teal accent `#0891b2`, teal-green support `#2d8b8b`, with tinted section cards (`#f0f9fb`, `#f1faee`) and outer background `#cdd4db`.
- Digest structure: Top Story → Key Developments → On Our Radar → Quick Hits → Footer.
- CSS inlined via `premailer` for broad email client compatibility.

### Storage
- SQLite at `data/dpns.db` — tracks articles, `pipeline_runs`, and `delivery_log`.
- Dedup window: 7 days by URL (normalized — tracking params stripped).
- Stored articles are used for recent-URL dedup and reuse testing, not as an LLM cache.
- Reuse mode only considers rows inside `reuse_seen_db_window_days` and excludes undated scraped rows.
- Current issue number is fixed to `0` through `config/settings.yaml` via `issue_number_override`.

---

## Environment Variables

```bash
# Required
OPENROUTER_API_KEY=sk-or-...
AGENTMAIL_API_KEY=...
AGENTMAIL_INBOX_ID=...   # Sending inbox ID from AgentMail console
EMAIL_FROM=news-scout@yourdomain.com

# Optional
LOG_LEVEL=INFO          # DEBUG for local dev
DRY_RUN=false           # Set true to skip sending
PIPELINE_TIMEOUT=600    # Max pipeline runtime (seconds)
LLM_SCORING_MODEL=anthropic/claude-haiku-4.5
LLM_DIGEST_MODEL=anthropic/claude-sonnet-4-6
LLM_MODEL_FALLBACK=anthropic/claude-haiku-4.5
RSS_LOOKBACK_HOURS=168
EMAIL_MAX_WIDTH_PX=880
ISSUE_NUMBER_OVERRIDE=0
SEARCH_FALLBACK_ENABLED=true
SEARCH_FALLBACK_PROVIDER=brave
SEARCH_FALLBACK_TIMEOUT_SECONDS=15
SEARCH_FALLBACK_MAX_RESULTS_PER_SOURCE=3
BRAVE_SEARCH_API_KEY=...   # Required when SEARCH_FALLBACK_ENABLED=true (the default)
```

---

## Development Workflow

### Initial setup
```bash
pip install -e ".[dev]"
cp .env.example .env   # then fill in required keys (see Environment Variables above)
```

### Running the pipeline locally
```bash
# Full dry-run (fetch + analyze + render, no send)
python scripts/run_manual.py --dry-run

# Preview rendered email in browser
python scripts/run_manual.py --preview

# Test fetching only
python scripts/run_manual.py --sources-only

# Send to test address
python scripts/run_manual.py --test-email you@example.com

# Full pipeline
python -m src.main
```

Note:

- Non-dry manual sends append a timestamped subject suffix to reduce mail-client threading during repeated tests.
- `--reuse-seen-db` only reuses recent eligible stored rows and excludes undated scraped entries.

### Runtime Triggering

- Production schedule: `cron-job.org` posts `repository_dispatch` with event type `run-daily-digest`
- Manual GitHub run: `workflow_dispatch`
- Local operator run: `python scripts/run_manual.py`

### Running tests
```bash
pytest tests/
pytest tests/test_fetcher.py -v   # Unit tests for a single module
pytest tests/test_pipeline.py -v  # Integration test (makes real LLM calls)
```

### Validating sources
```bash
python scripts/seed_sources.py    # Health-check all sources.yaml entries
```

---

## Commit Convention

```
feat(module): description [task-id]
fix(module): description [task-id]
docs: description
chore: description
```

Task IDs reference PLAN.md (e.g., `[2.1.2]` = Task 2.1.2 RSS parser).

---

## PRD Requirement IDs

Reference these IDs when building or reviewing features:

| Prefix | Area |
|--------|------|
| F-xx | Fetcher/ingestion |
| A-xx | Analyzer/LLM |
| E-xx | Email renderer |
| D-xx | Delivery/sender |
| S-xx | Scheduling/orchestration |

---

## Cost Targets

- OpenRouter API: ~$15–25/month (scoring + composition, ~15 articles/day × 22 weekdays)
- Email delivery (AgentMail free tier): $0
- Scheduling (cron-job.org free tier + GitHub Actions runner): $0
- **Total target: < $50/month** (PRD NFR)

---

## Out of Scope (v1)

- Web dashboard / archive UI
- Slack/Teams delivery
- Personalized digests per reader
- Real-time breaking news alerts
- Podcast transcription
- Internal procurement system integration
