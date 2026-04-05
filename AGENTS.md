# AGENTS.md — Digital Procurement News Scout (DPNS)

## Project Summary

DPNS is a daily automated email digest that currently monitors 16 configured procurement and digital transformation sources, uses Claude to score relevance and compose executive summaries, and delivers a curated briefing to Digital Procurement senior leaders at PepsiCo every weekday at 9:00 AM CET.

Full context lives in:
- **[PRD.md](PRD.md)** — product requirements, content strategy, email design spec, success metrics
- **[PLAN.md](PLAN.md)** — phased implementation plan, task breakdown, dependency graph
- **[CLAUDE.md](CLAUDE.md)** — original project guidance this file is derived from

Use this file as the default orientation doc for coding agents working in this repository.

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

Primary operator entry point for local work: `python scripts/run_manual.py`

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
├── AGENTS.md                   # Agent-facing project guide
├── CLAUDE.md                   # Original project guide
├── PRD.md                      # Product requirements
├── PLAN.md                     # Implementation plan
├── pyproject.toml
├── .env.example
├── config/
│   ├── sources.yaml            # Source registry (URL, tier, method, selectors)
│   ├── search_fallback_allowlist.yaml # Trusted publisher allowlist for Brave fallback
│   ├── recipients.yaml         # Email distribution list
│   └── settings.yaml           # Pipeline tuning (thresholds, limits, schedule)
├── prompts/
│   ├── context_preamble.md     # Shared LLM context about the team/org
│   ├── relevance_scoring.md    # Rubric for 1–10 relevance scoring
│   └── digest_composition.md   # Format/tone instructions for digest output
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
│   ├── test_fetcher.py
│   ├── test_analyzer.py
│   ├── test_renderer.py
│   └── test_pipeline.py
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
- Sources are tiered (Tier 1 = must-fetch, Tier 2 = supplemental, Tier 3 = conditional).
- Source config in `config/sources.yaml` — adding/removing sources requires no code change (F-08).
- Current validated active set: 16 sources total, with 6 RSS feeds, 9 direct scrape sources, and 1 fallback-only source.
- RSS-first strategy; web scraping only where RSS is unavailable.
- Live fetch freshness window is currently 7 days.
- Robots.txt respected; 1 req/sec per domain rate limit.
- `robots.txt` is checked before RSS and scrape fetches.
- If the DPNS-managed HTTP client gets `401/403` on `robots.txt`, the fetcher may do one fallback retry for the robots file only.
- If that retry fails, the policy remains deny-by-default unless `robots.txt` is confirmed missing (`404/410`).
- Caller-supplied `httpx.AsyncClient` instances are not bypassed during robots evaluation.
- With `search_fallback_enabled: true`, active sources automatically try Brave search fallback after a direct failure or a direct fetch that returns `0` recent articles.
- Inactive sources can be reintroduced as fallback-only with `fallback_search.enabled: true` and `fallback_search.include_when_inactive: true`.
- Search fallback only accepts publishers from `config/search_fallback_allowlist.yaml`, rejects denylisted/user-generated domains, and re-checks candidate-site robots before fetching article metadata.
- Fallback articles persist `origin_source` and `discovery_method=search_fallback` while keeping the actual publisher as `source`.
- Search fallback now emits a per-source summary in progress/log output, including counts such as Brave results returned, allowlist blocks, stale candidates, robots blocks, and candidate fetch failures.
- Per-source `fallback_search.query` overrides are the main tuning lever for ambiguous brands; the current tuned set includes SAP Ariba, Archlet, Keelvar, SpendHQ, GEP, Zip, Sievo, Digital Procurement World, and Mars Newsroom.
- `config/search_fallback_allowlist.yaml` remains operator-editable and now includes additional trusted trade-media seeds such as `globaltrademag.com`, `dcvelocity.com`, `thescxchange.com`, and `cpostrategy.media`.

### Email Design
- Current desktop max width: 880px, table-based layout for Outlook compatibility.
- Color palette: deep navy header/footer `#1a2332`, teal accent `#0891b2`, teal-green support `#2d8b8b`, with tinted section cards (`#f0f9fb`, `#f1faee`) and outer background `#cdd4db`.
- Digest structure: Top Story → Key Developments → On Our Radar → Quick Hits → Footer.
- CSS inlined via `premailer` for broad email client compatibility.

### Storage
- SQLite at `data/dpns.db` — tracks articles, `pipeline_runs`, and `delivery_log`.
- Dedup window: 7 days by URL (normalized — tracking params stripped).
- Stored articles are used for recent-URL dedup and testing reuse flows, not as an LLM cache.
- Reuse mode only considers rows inside `reuse_seen_db_window_days` and excludes undated scraped rows.
- Current issue number is fixed to `0` through `config/settings.yaml` via `issue_number_override`.

### Current Runtime Settings Worth Knowing

- `max_digest_items: 15`
- `max_digest_items_per_source: 3`
- `llm_scoring_model: anthropic/claude-haiku-4.5`
- `llm_digest_model: anthropic/claude-sonnet-4-6`
- `llm_model_fallback: anthropic/claude-haiku-4.5`
- `rss_lookback_hours: 168`
- `dedup_window_days: 7`
- `reuse_seen_db_window_days: 7`
- `email_max_width_px: 880`
- `issue_number_override: 0`
- `search_fallback_enabled: true`
- `search_fallback_provider: brave`
- `search_fallback_timeout_seconds: 15`
- `search_fallback_max_results_per_source: 3`

---

## Agent Working Norms

- Prefer config-driven changes over hardcoding when working with sources, recipients, prompt text, thresholds, or schedule details.
- Keep the pipeline stages decoupled; avoid introducing cross-stage coupling unless there is a strong operational reason.
- Preserve local editability for non-developers, especially in `config/`, `prompts/`, and email template content.
- Respect the existing source-ingestion constraints: RSS first, scraping only when needed, robots-aware, rate-limited, and allowlist-gated when search fallback is involved.
- When search fallback returns `0` articles, check the fallback summary counts before changing code; in most cases the right fix is a source query override or a deliberate allowlist adjustment.
- Treat email client compatibility as a product constraint, not a polish item.
- When implementing features or reviews, reference PRD requirement IDs where possible (`F-xx`, `A-xx`, `E-xx`, `D-xx`, `S-xx`).

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
MAX_DIGEST_ITEMS=15
MAX_DIGEST_ITEMS_PER_SOURCE=3
EMAIL_MAX_WIDTH_PX=880
ISSUE_NUMBER_OVERRIDE=0
SEARCH_FALLBACK_ENABLED=true
SEARCH_FALLBACK_PROVIDER=brave
SEARCH_FALLBACK_TIMEOUT_SECONDS=15
SEARCH_FALLBACK_MAX_RESULTS_PER_SOURCE=3
BRAVE_SEARCH_API_KEY=...
```

---

## Development Workflow

### Runtime Triggering

- Production schedule: `cron-job.org` posts `repository_dispatch` with event type `run-daily-digest`
- Manual GitHub run: `workflow_dispatch`
- Local operator run: `python scripts/run_manual.py`

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

### Manual Run Flags

`scripts/run_manual.py` supports these flags:

- `--dry-run`
  Full fetch/analyze/render pipeline, but skip send.
- `--preview`
  Build the live digest, write HTML/plain-text previews, and open the HTML preview.
- `--preview-path PATH`
  Override the HTML preview output path.
- `--plaintext-path PATH`
  Override the plain-text preview output path.
- `--test-email EMAIL`
  Build the live digest and send it only to the supplied address.
- `--sources-only`
  Fetch sources only. No LLM analysis, no rendering, no email.
- `--ignore-seen-db`
  Fetch live sources while ignoring the current seen-URL dedup state in SQLite.
- `--reuse-seen-db`
  Skip live fetch and rebuild the run from articles currently stored in SQLite.

Invalid combinations:

- `--dry-run` cannot be combined with `--preview` or `--test-email`.
- `--sources-only` cannot be combined with `--dry-run`, `--preview`, or `--test-email`.
- `--ignore-seen-db` cannot be combined with `--reuse-seen-db`.
- `--sources-only` cannot be combined with `--reuse-seen-db`.

### Test Runs: Important Behavior

This repository now has three distinct testing behaviors that matter for repeated same-day runs.

1. Normal run
   Command: `python scripts/run_manual.py`
   Behavior:
   Fetches live sources, applies DB-backed recent-URL dedup, runs analysis, sends email, and on successful live completion persists article metadata back to SQLite.

2. Refetch while ignoring what was already seen
   Command: `python scripts/run_manual.py --ignore-seen-db`
   Behavior:
   Fetches live sources again, ignores the current DB seen set during dedup, and is intended for repeated testing against live content. Batch-local duplicate URLs are still removed. On preview/test-email runs in this mode, the `articles` table is refreshed as part of the explicit testing flow.

3. Reuse what is already in SQLite
   Command: `python scripts/run_manual.py --dry-run --reuse-seen-db`
   Behavior:
   Skips network fetch completely and reuses stored articles from `data/dpns.db`. Only recent rows inside the reuse window are eligible, and undated scraped rows are excluded. This is the most repeatable option for testing layout, prompt changes, or send behavior without hitting sources again. It fails if no recent eligible rows remain.

Model validation note:

- `python scripts/run_manual.py --dry-run --reuse-seen-db` is the recommended repeatable check after changing analyzer models.
- Confirm in `data/logs/dpns.jsonl` that relevance batches request the scoring model and digest composition requests the digest model.

Special note:

- `--preview` and `--test-email` use live fetched content by default.
- `--preview --reuse-seen-db` and `--test-email --reuse-seen-db` are the recommended repeatable test modes once the DB has been populated.
- `--sources-only` never persists fetched articles.
- Non-dry manual sends append a timestamped subject suffix to reduce mail-client threading during repeated test sends.

### SQLite Behavior Summary

- The pipeline fetches live sources on every normal run.
- Stored articles are not used as an LLM cache.
- Stored articles are used for recent-URL dedup and for the explicit `--reuse-seen-db` testing mode.
- The `articles` table is upserted by normalized URL.
- Clearing `articles` removes the current seen set and also removes the source material for `--reuse-seen-db`.

### Mock Template Testing

For design-only or static content checks, use:

```bash
python scripts/test_email.py
python scripts/test_email.py --issue-number 0 --date "April 4, 2026"
python scripts/test_email.py --to you@example.com
```

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

### Recommended Local Test Sequences

Populate the DB from live content without being blocked by the current seen set:

```bash
python scripts/run_manual.py --ignore-seen-db --dry-run
```

Preview from the exact stored DB set without refetching:

```bash
python scripts/run_manual.py --preview --reuse-seen-db
```

Send a one-off repeatable test email from the stored DB set:

```bash
python scripts/run_manual.py --test-email you@example.com --reuse-seen-db
```

---

## Commit Convention

```
feat(module): description [task-id]
fix(module): description [task-id]
docs: description
chore: description
```

Task IDs reference `PLAN.md` (for example, `[2.1.2]` = Task 2.1.2 RSS parser).

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
