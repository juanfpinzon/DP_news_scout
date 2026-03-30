# AGENTS.md — Digital Procurement News Scout (DPNS)

## Project Summary

DPNS is a daily automated email digest that monitors 20+ procurement and digital transformation news sources, uses Claude to score relevance and compose executive summaries, and delivers a curated briefing to Digital Procurement senior leaders at PepsiCo every weekday at 9:00 AM CET.

Full context lives in:
- **[PRD.md](PRD.md)** — product requirements, content strategy, email design spec, success metrics
- **[PLAN.md](PLAN.md)** — phased implementation plan, task breakdown, dependency graph
- **[CLAUDE.md](CLAUDE.md)** — original project guidance this file is derived from

Use this file as the default orientation doc for coding agents working in this repository.

---

## Architecture

Four-stage sequential pipeline triggered by a daily cron (GitHub Actions):

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
- **SQLite** via **sqlite-utils** (article storage, run logs, delivery logs)
- **structlog** (structured logging)
- **pyyaml** + **python-dotenv** (config)
- **GitHub Actions** (cron scheduler)

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
│   ├── recipients.yaml         # Email distribution list
│   └── settings.yaml           # Pipeline tuning (thresholds, limits, schedule)
├── prompts/
│   ├── context_preamble.md     # Shared LLM context about the team/org
│   ├── relevance_scoring.md    # Rubric for 1–10 relevance scoring
│   └── digest_composition.md   # Format/tone instructions for digest output
├── src/
│   ├── main.py                 # Pipeline orchestrator
│   ├── fetcher/                # RSS, scraping, dedup, registry
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
    └── daily_digest.yml        # Weekday cron at 8 AM UTC (9 AM CET, standard time)
```

---

## Key Design Decisions

### LLM Usage
- **Two-pass LLM pipeline**: first call scores relevance (batch of ~10 articles → JSON scores), second call composes the full digest (top ~15 articles → structured JSON output).
- **Provider: OpenRouter** — all LLM calls go through OpenRouter using the `openai` SDK with `base_url="https://openrouter.ai/api/v1"`. Default model is `anthropic/claude-sonnet-4-6` but can be swapped to any OpenRouter-supported model via `config/settings.yaml` with no code change.
- All prompts live in `/prompts/` as Markdown files — editable by non-developers without touching code.
- System prompt always includes `context_preamble.md` which encodes PepsiCo Digital Procurement context.

### Content Sources
- Sources are tiered (Tier 1 = must-fetch, Tier 2 = supplemental, Tier 3 = conditional).
- Source config in `config/sources.yaml` — adding/removing sources requires no code change (F-08).
- RSS-first strategy; web scraping only where RSS is unavailable.
- Robots.txt respected; 1 req/sec per domain rate limit.

### Email Design
- Max width 640px, table-based layout for Outlook compatibility.
- Color palette: navy header `#1a2744`, teal accent `#0891b2`, light gray cards `#f8f9fa`.
- Digest structure: Top Story → Key Developments → On Our Radar → Quick Hits → Footer.
- CSS inlined via `premailer` for broad email client compatibility.

### Storage
- SQLite at `data/dpns.db` — tracks articles (dedup), pipeline runs, and delivery logs.
- Dedup window: 7 days by URL (normalized — tracking params stripped).
- Issue numbers auto-increment from pipeline run count.

---

## Agent Working Norms

- Prefer config-driven changes over hardcoding when working with sources, recipients, prompt text, thresholds, or schedule details.
- Keep the pipeline stages decoupled; avoid introducing cross-stage coupling unless there is a strong operational reason.
- Preserve local editability for non-developers, especially in `config/`, `prompts/`, and email template content.
- Respect the existing source-ingestion constraints: RSS first, scraping only when needed, robots-aware, and rate-limited.
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
```

---

## Development Workflow

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
- Scheduling (GitHub Actions free tier): $0
- **Total target: < $50/month** (PRD NFR)

---

## Out of Scope (v1)

- Web dashboard / archive UI
- Slack/Teams delivery
- Personalized digests per reader
- Real-time breaking news alerts
- Podcast transcription
- Internal procurement system integration
