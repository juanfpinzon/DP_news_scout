# Implementation Plan: Digital Procurement News Scout

## Overview

This plan breaks the DPNS project into phases, epics, and individual tasks sized for Claude Code sessions. Each task is designed to be completable in a single coding session with clear inputs/outputs.

**Estimated total effort:** 5–7 days of focused development
**Tech stack:** Python 3.11+ · httpx · feedparser · BeautifulSoup4 · openai SDK (via OpenRouter) · Jinja2 · AgentMail · SQLite · GitHub Actions

---

## Project Structure

```
dpns/
├── README.md
├── pyproject.toml              # Dependencies & project config
├── .env.example                # Template for secrets
├── .env                        # Local secrets (gitignored)
├── config/
│   ├── sources.yaml            # Source registry (URLs, tiers, methods)
│   ├── recipients.yaml         # Email recipient list
│   └── settings.yaml           # Pipeline settings (limits, schedule, etc.)
├── prompts/
│   ├── context_preamble.md     # Shared context about the team/org
│   ├── relevance_scoring.md    # Prompt for scoring article relevance
│   └── digest_composition.md   # Prompt for generating the digest
├── src/
│   ├── __init__.py
│   ├── main.py                 # Pipeline orchestrator (entry point)
│   ├── fetcher/
│   │   ├── __init__.py
│   │   ├── rss.py              # RSS feed parser
│   │   ├── scraper.py          # Web scraper for non-RSS sources
│   │   ├── registry.py         # Source registry loader
│   │   └── dedup.py            # URL-based deduplication
│   ├── analyzer/
│   │   ├── __init__.py
│   │   ├── relevance.py        # Relevance scoring via LLM
│   │   ├── digest.py           # Digest composition via LLM
│   │   └── llm_client.py       # OpenRouter LLM client (openai SDK)
│   ├── renderer/
│   │   ├── __init__.py
│   │   ├── html_email.py       # HTML email builder
│   │   ├── plaintext.py        # Plain-text fallback
│   │   └── templates/
│   │       └── digest.html     # Jinja2 email template
│   ├── sender/
│   │   ├── __init__.py
│   │   └── email_sender.py     # Resend/SendGrid integration
│   ├── storage/
│   │   ├── __init__.py
│   │   └── db.py               # SQLite for articles, runs, logs
│   └── utils/
│       ├── __init__.py
│       ├── logging.py          # Structured logging setup
│       └── config.py           # Config loader (YAML + env)
├── templates/
│   └── digest_email.html       # MJML source (compiled to renderer/templates/)
├── tests/
│   ├── test_fetcher.py
│   ├── test_analyzer.py
│   ├── test_renderer.py
│   └── test_pipeline.py
├── scripts/
│   ├── run_manual.py           # Manual trigger for testing
│   ├── test_email.py           # Send a test email
│   └── seed_sources.py         # Populate source registry
└── .github/
    └── workflows/
        └── daily_digest.yml    # GitHub Actions cron schedule
```

---

## Phase 1: Foundation (Day 1)

### Epic 1.1 — Project Setup

**Task 1.1.1: Initialize project structure** ✅
```
Create the directory structure above.
Set up pyproject.toml with dependencies:
  - httpx[http2], feedparser, beautifulsoup4, lxml
  - openai (used as OpenRouter client)
  - jinja2, premailer (CSS inlining)
  - agentmail (email)
  - pyyaml, python-dotenv
  - sqlite-utils
  - structlog
Create .env.example with all required env vars.
Create a basic README.md.
```

**Task 1.1.2: Config system** ✅
```
Build src/utils/config.py:
  - Load settings.yaml, sources.yaml, recipients.yaml
  - Merge with environment variables (.env)
  - Provide typed access (dataclass or Pydantic model)
  - Validate required fields on startup

Create config/settings.yaml with defaults:
  - max_articles_per_source: 10
  - max_digest_items: 15
  - relevance_threshold: 6 (out of 10)
  - digest_send_time: "09:00"
  - timezone: "Central European Time"
  - llm_model: "anthropic/claude-sonnet-4-6"     # primary model via OpenRouter
  - llm_model_fallback: "anthropic/claude-4-5-haiku" # cheaper fallback if primary unavailable
```

**Task 1.1.3: Logging setup** ✅
```
Build src/utils/logging.py:
  - structlog configuration
  - Console output (dev) + JSON file output (prod)
  - Log levels: DEBUG for dev, INFO for prod
  - Include timestamp, module, pipeline_stage in every log
```

**Task 1.1.4: Database schema** ✅
```
Build src/storage/db.py:
  - SQLite database at data/dpns.db
  - Tables:
    - articles (id, url, title, source, published_at, fetched_at, content_snippet, relevance_score, included_in_digest)
    - pipeline_runs (id, started_at, completed_at, status, sources_fetched, articles_found, articles_included, error_log)
    - delivery_log (id, run_id, sent_at, recipient_count, status, error)
  - Helper functions: save_articles(), get_recent_urls(days=2), log_run(), log_delivery()
```

---

## Phase 2: Content Fetching (Day 2)

### Epic 2.1 — Source Registry

**Task 2.1.1: Build source registry** ✅ REVISED (2026-04-04)
```
Create config/sources.yaml with ALL sources:
  For each source, define:
    - name: "Spend Matters"
    - url: "https://spendmatters.com/feed/"
    - tier: 1
    - method: "rss" | "scrape"
    - selectors: (if scrape) { article: "css selector", title: "...", link: "...", date: "..." }
    - active: true
    - category: "trade_media" | "analyst" | "consulting" | "vendor" | "mainstream" | "community" | "peer_cpg"

Build src/fetcher/registry.py:
  - Load sources.yaml
  - Filter by active=true
  - Sort by tier (1 first)
  - Return list of Source dataclass objects

Research and populate actual RSS feed URLs for all 20+ sources.

REVISION COMPLETED (2026-04-04):
  - Implemented scripts/seed_sources.py (was a stub) — validates all URLs and selectors.
  - Switched McKinsey, Hackett Group, HBR to confirmed working RSS feeds.
  - Fixed DPW domain (digitalprocurementworld.com → conference.dpw.ai).
  - Fixed P&G URL (news.pg.com → us.pg.com/newsroom redirect).
  - Tuned selectors for Webflow sites: Archlet, Keelvar, Zip (custom CSS selectors).
  - Tuned selectors for Sievo (HubSpot CMS: .l-news-listing__item, .c-blog-item-big).
  - Fixed scraper _extract_url to handle <a> tag containers (Zip's .article-preview-block).
  - Deactivated 17 sources that require Playwright (403 bot protection or JS-rendered):
    Gartner, BCG, Kearney, Deloitte, Accenture, PwC, EY, Coupa, JAGGAER, Ivalua,
    Supply Chain Digital, Procurious, Unilever, Kraft Heinz, Mondelez, P&G,
    Procurement Leaders (paywall).
  - Result: 16 active sources, all passing validation (0 failures, 0 warnings).
```

**Task 2.1.2: RSS feed parser** ✅
```
Build src/fetcher/rss.py:
  - async function fetch_rss(source: Source) -> list[RawArticle]
  - Use feedparser to parse RSS/Atom feeds
  - Extract: title, url, published_date, summary/description, author
  - Filter to last 48 hours
  - Handle feed errors gracefully (timeout, malformed XML, 404)
  - Return normalized RawArticle dataclass
```

**Task 2.1.3: Web scraper for non-RSS sources** ✅
```
Build src/fetcher/scraper.py:
  - async function scrape_source(source: Source) -> list[RawArticle]
  - Use httpx (async) + BeautifulSoup4
  - Apply CSS selectors from source config
  - Extract same fields as RSS parser
  - Handle: JS-rendered pages (note: may need to flag these for future Playwright integration)
  - Respect rate limits: 1 request per second per domain
  - Rotate user-agent strings
  - Return normalized RawArticle dataclass
```

**Task 2.1.4: Deduplication** ✅
```
Build src/fetcher/dedup.py:
  - URL normalization (strip tracking params, trailing slashes, www)
  - Check against SQLite articles table (last 7 days)
  - Remove exact URL duplicates from current batch
  - Return deduplicated list
```

**Task 2.1.5: Fetcher orchestrator** ✅
```
Build src/fetcher/__init__.py with fetch_all_sources():
  - Load registry
  - Run all fetchers concurrently (asyncio.gather with semaphore limit=5)
  - Collect results, handle per-source errors
  - Run dedup
  - Save raw articles to SQLite
  - Log: sources attempted, succeeded, failed, articles found
  - Return list[RawArticle]
```

---

## Phase 3: Content Analysis (Day 3)

### Epic 3.1 — LLM Integration

**Task 3.1.1: LLM client with fallback** ✅
```
Build src/analyzer/llm_client.py:
  - class LLMClient using openai SDK pointed at OpenRouter:
      client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
  - Method: async complete(system_prompt, user_prompt, max_tokens) -> str
  - Model configured via settings.yaml (llm_model); falls back to llm_model_fallback if primary returns 429/503
  - Token tracking: log input/output tokens per call
  - Retry: 3 attempts with exponential backoff
  - Cost estimation: log estimated cost per call (using OpenRouter /api/v1/generation metadata)
```

**Task 3.1.2: Context preamble prompt** ✅
```
Create prompts/context_preamble.md:
  "You are an expert editorial assistant for the Digital Procurement
   team at a major CPG company (PepsiCo). This team is leading the
   digital transformation of the procurement organization, implementing
   Source-to-Pay (S2P) and Procure-to-Pay (P2P) platforms, leveraging
   AI/ML for procurement processes, and modernizing supplier management.

   Platforms and tools currently in use by this team:
   - SAP / SAP Ariba (S2P/P2P backbone)
   - Archlet (sourcing optimization)
   - Keelvar (sourcing automation)
   - Selectica (contract management)
   - SpendhQ (spend analytics)
   - Pirt (procurement platform)
   - Tirzo (procurement platform)

   Any news, updates, or analysis about these specific platforms is
   highly relevant and should receive a +2 bonus to its relevance score.

   Peer / competitor CPG companies to watch for competitive intelligence:
   - Unilever, Mars, Mondelez, Procter & Gamble, Kraft Heinz
   - Any news about these companies' procurement digitization, sourcing
     strategies, supplier programs, or technology adoption is relevant.
   - General corporate news from these companies (earnings, marketing,
     brand launches) should be EXCLUDED unless it directly relates to
     procurement, supply chain technology, or digital transformation.

   The readers are VP and Director-level leaders who need to stay
   informed about:
   - Procurement technology platform developments (especially platforms above)
   - AI/GenAI applications in procurement
   - Digital transformation best practices and case studies
   - Market moves (M&A, funding, partnerships in procuretech)
   - Strategic trends affecting CPO/procurement leadership
   - Regulatory and compliance changes affecting procurement
   - What peer CPG companies are doing in digital procurement (competitive intelligence)

   Always evaluate content through the lens of: 'Would a Digital
   Procurement senior leader at a Fortune 500 CPG company find this
   actionable or strategically relevant?'"
```

**Task 3.1.3: Relevance scoring** ✅
```
Build src/analyzer/relevance.py:
  - Takes a batch of RawArticle objects
  - Sends articles in batches of ~10 to LLM with relevance_scoring prompt
  - Prompt asks LLM to return JSON:
    { "scores": [ { "url": "...", "score": 8, "reasoning": "..." } ] }
  - Score 1-10 scale with defined rubric in prompt
  - Filter articles below threshold (default: 6)
  - Return scored and filtered list

Create prompts/relevance_scoring.md with the scoring rubric.
```

**Task 3.1.4: Digest composition** ✅
```
Build src/analyzer/digest.py:
  - Takes top ~15 scored articles
  - Sends to LLM with digest_composition prompt
  - Prompt instructs LLM to return structured JSON:
    {
      "top_story": { "url": "...", "headline": "...", "summary": "...", "why_it_matters": "...", "source": "...", "date": "..." },
      "key_developments": [ ... ],
      "on_our_radar": [ ... ],
      "quick_hits": [ { "url": "...", "one_liner": "...", "source": "..." } ]
    }
  - Validate LLM output structure
  - Return Digest dataclass

Create prompts/digest_composition.md with format instructions and examples.
```

---

## Phase 4: Email Rendering (Day 4)

### Epic 4.1 — Email Template & Renderer

**Task 4.1.1: Design HTML email template** ✅
```
⚡ Use the /frontend-design skill for this task to generate the template.

Create templates/digest_email.html (Jinja2 template):
  - Inline CSS (email clients strip <style> blocks in many cases)
  - Table-based layout for Outlook compatibility
  - 640px max width, centered
  - Sections:
    - Header: navy background, white text, date + issue #
    - Top Story: teal left-border accent, larger headline
    - Key Developments: card-style with light gray bg
    - On Our Radar: simpler list with brief descriptions
    - Quick Hits: bullet list, one line each
    - Footer: gray, small text, feedback link
  - All links in teal (#0891b2)
  - Test rendering in Litmus or Email on Acid (manual)

Compile/inline using premailer for final output.
```

**Task 4.1.2: HTML email builder** ✅
```
Build src/renderer/html_email.py:
  - function render_digest(digest: Digest, issue_number: int, date: str) -> str
  - Load Jinja2 template
  - Populate with digest data
  - Run through premailer to inline all CSS
  - Return HTML string
```

**Task 4.1.3: Plain-text fallback** ✅
```
Build src/renderer/plaintext.py:
  - function render_plaintext(digest: Digest, issue_number: int, date: str) -> str
  - Structured plain text version:
    ===================================
    DIGITAL PROCUREMENT NEWS SCOUT
    March 26, 2026 · Issue #142
    ===================================

    ★ TOP STORY
    Headline
    Summary...
    Why it matters: ...
    → Read more: [URL]
    Source · Date

    KEY DEVELOPMENTS
    ─────────────────
    1. Headline
       Summary...
       → [URL]
    ...
```

**Task 4.1.4: Email preview script** ✅
```
Build scripts/test_email.py:
  - Generate a digest with mock data (realistic sample articles)
  - Render HTML and plain text
  - Save HTML to /tmp/preview.html for browser testing
  - Optionally send to a test email address
```

---

## Phase 5: Email Delivery (Day 5)

### Epic 5.1 — Sender & Scheduling

**Task 5.1.1: Email sender** ✅
```
Build src/sender/email_sender.py:
  - function send_digest(html: str, plaintext: str, subject: str) -> bool
  - Load recipients from config/recipients.yaml
  - Use AgentMail SDK (pip install agentmail):
    - Initialize AgentMail(api_key=AGENTMAIL_API_KEY)
    - Use inbox identified by AGENTMAIL_INBOX_ID env var
      ⚠️  SETUP NOTE: prompt the user to provide their AGENTMAIL_INBOX_ID
          (created in the AgentMail console) before the first run.
    - Call client.inboxes.messages.send(inbox_id, to=..., bcc=..., subject=..., html=..., text=...)
    - BCC all recipients for privacy; use configurable FROM inbox
  - Retry: 3 attempts, exponential backoff (AgentMail auto-retries 408/429/5xx)
  - Log: delivery status, errors, recipient count
  - Return success/failure

Create config/recipients.yaml:
  - Admin-managed list of email addresses (v1)
  - Support groups (leadership, extended, test)
  - Self-serve subscribe/unsubscribe is a v2 enhancement
```

**Task 5.1.2: Pipeline orchestrator** ✅
```
Build src/main.py:
  - function run_pipeline():
    1. Log pipeline start
    2. Fetch all sources → raw_articles
    3. Score relevance → scored_articles
    4. Compose digest → digest object
    5. Render HTML + plain text
    6. Send email
    7. Log pipeline completion (success/failure, stats)
  - Wrap each stage in try/except with graceful degradation
  - Calculate and store issue_number (from DB run count)
  - Total timeout: 10 minutes
  - CLI entry point: `python -m src.main`
```

**Task 5.1.3: Manual run script** ✅
```
Build scripts/run_manual.py:
  - Parse CLI args:
    --dry-run (fetch + analyze, don't send)
    --test-email (send to test address only)
    --preview (save HTML to file, open in browser)
    --sources-only (just test fetching)
  - Call pipeline with appropriate flags
```

**Task 5.1.4: GitHub Actions cron** ✅
```
Create .github/workflows/daily_digest.yml:
  - cron: "0 13 * * 1-5"  (1 PM UTC = 9 AM ET, weekdays only)
  - Steps:
    1. Checkout repo
    2. Setup Python 3.11
    3. Install dependencies
    4. Run pipeline: python -m src.main
  - Secrets: OPENROUTER_API_KEY, AGENTMAIL_API_KEY, AGENTMAIL_INBOX_ID, EMAIL_FROM, etc.
  - Notifications: alert on failure (GitHub notifications or Slack webhook)

Alternative: Railway/Render cron job config if self-hosted.
```

---

## Phase 6: Testing & Polish (Day 6)

### Epic 6.1 — Testing

**Task 6.1.1: Unit tests — Fetcher**
```
Build tests/test_fetcher.py:
  - Test RSS parsing with sample feed XML
  - Test scraper with sample HTML
  - Test URL dedup logic
  - Test source registry loading
  - Mock HTTP responses (use respx or httpx mock)
```

**Task 6.1.2: Unit tests — Analyzer**
```
Build tests/test_analyzer.py:
  - Test relevance scoring with mock LLM responses
  - Test digest composition with mock LLM responses
  - Test LLM fallback behavior
  - Test JSON parsing of LLM output (including malformed responses)
```

**Task 6.1.3: Unit tests — Renderer**
```
Build tests/test_renderer.py:
  - Test HTML rendering with sample digest data
  - Test plain-text rendering
  - Verify all links are present
  - Verify template handles empty sections gracefully
```

**Task 6.1.4: Integration test — Full pipeline**
```
Build tests/test_pipeline.py:
  - End-to-end test with:
    - 2-3 real RSS sources (fast, reliable ones)
    - Real LLM call (small batch)
    - HTML rendering
    - Dry-run (no send)
  - Assert: pipeline completes, digest has content, HTML is valid
```

### Epic 6.2 — Hardening

**Task 6.2.1: Error handling audit**
```
Review all modules for:
  - Unhandled exceptions
  - Missing timeouts on HTTP calls
  - LLM response validation
  - Graceful degradation (partial source failure shouldn't block pipeline)
  - Add structured error context to all log messages
```

**Task 6.2.2: Source URL validation**
```
Build scripts/seed_sources.py:
  - Hit every source URL in sources.yaml
  - Report: reachable, returns articles, RSS vs scrape working
  - Flag any sources that need selector updates
  - Output a health report
```

---

## Phase 7: Launch & Iterate (Day 7+)

### Epic 7.1 — Soft Launch

**Task 7.1.1: Dry-run week**
```
- Run pipeline daily for 5 days in dry-run mode
- Review outputs manually each day
- Tune:
  - Source selectors that aren't working
  - Relevance threshold (too strict? too loose?)
  - Prompt wording for summary quality
  - Email template rendering issues
- Document all tuning changes
```

**Task 7.1.2: Test send to inner circle**
```
- Send to 3-5 team members for 1 week
- Collect feedback on:
  - Relevance (are articles useful?)
  - Readability (is the format right?)
  - Completeness (any sources missing?)
  - Frequency (daily is right?)
```

**Task 7.1.3: Full launch**
```
- Add all recipients to config
- Enable cron schedule
- Monitor first week closely
- Set up alerts for pipeline failures
```

---

## Task Dependency Graph

```
1.1.1 (project setup)
  ├── 1.1.2 (config) ──────────┐
  ├── 1.1.3 (logging) ─────────┤
  └── 1.1.4 (database) ────────┤
                                │
2.1.1 (source registry) ───────┤
  ├── 2.1.2 (RSS parser) ──────┤
  ├── 2.1.3 (web scraper) ─────┤
  ├── 2.1.4 (dedup) ───────────┤
  └── 2.1.5 (fetch orchestr.) ─┤──── Fetcher complete
                                │
3.1.1 (LLM client) ────────────┤
  ├── 3.1.2 (context prompt) ──┤
  ├── 3.1.3 (relevance) ───────┤
  └── 3.1.4 (digest comp.) ────┤──── Analyzer complete
                                │
4.1.1 (HTML template) ─────────┤
  ├── 4.1.2 (HTML builder) ────┤
  ├── 4.1.3 (plaintext) ───────┤
  └── 4.1.4 (preview script) ──┤──── Renderer complete
                                │
5.1.1 (email sender) ──────────┤
  ├── 5.1.2 (orchestrator) ────┤──── Pipeline complete
  ├── 5.1.3 (manual run) ──────┤
  └── 5.1.4 (GH Actions) ─────┤──── Scheduling complete
                                │
6.1.1–6.1.4 (tests) ───────────┤
6.2.1–6.2.2 (hardening) ───────┤──── Quality gate
                                │
7.1.1 (dry-run week) ──────────┤
7.1.2 (test send) ─────────────┤
7.1.3 (full launch) ───────────┘──── 🚀 Live
```

---

## Claude Code Session Guide

Each task above is sized for a single Claude Code session. Here's how to work through them efficiently:

### Session workflow
1. **Open the task** — reference this plan by task ID (e.g., "Let's work on Task 2.1.2")
2. **Context** — Claude Code will read this plan + the PRD to understand the full picture
3. **Build** — implement the task, writing code + tests
4. **Verify** — run the code / tests before closing the session
5. **Commit** — commit with a message like `feat(fetcher): implement RSS parser [2.1.2]`

### Tips for Claude Code sessions
- Keep the PRD.md and PLAN.md in the repo root — Claude Code will use them for context
- Reference specific requirement IDs (F-01, A-03, etc.) when building features
- Ask Claude Code to read the relevant prompt files before building analyzer modules
- For the email template, ask Claude Code to generate the HTML and save a preview you can open in a browser
- Run `scripts/run_manual.py --dry-run` frequently to test the full pipeline

### Commit convention
```
feat(module): description [task-id]
fix(module): description [task-id]
docs: description
chore: description
```

---

## Environment Variables Required

```bash
# LLM APIs
OPENROUTER_API_KEY=sk-or-...   # Dev/PoC: provisioned by Juan Pinzon
                                # Production: to be decided with Tatjana

# Email
AGENTMAIL_API_KEY=...           # Dev/PoC: provisioned by Juan Pinzon
AGENTMAIL_INBOX_ID=...          # ⚠️  Must be provided by user at setup
                                #    Create a sending inbox in the AgentMail
                                #    console and paste the ID here.
EMAIL_FROM=news-scout@yourdomain.com

# Optional
LOG_LEVEL=INFO
DRY_RUN=false
PIPELINE_TIMEOUT=600
```

---

## Cost Estimate (Monthly)

| Item | Estimate |
|------|----------|
| OpenRouter API (~15 articles/day × 22 weekdays, scoring + composition) | ~$15–25 |
| Resend (free tier: 3,000 emails/month) | $0 |
| GitHub Actions (free tier: 2,000 min/month) | $0 |
| Domain for email sender (optional) | ~$12/year |
| **Total** | **~$15–30/month** |

---

## Future Enhancements (v2+)

- **Web archive dashboard** — searchable history of all digests
- **Slack/Teams delivery** — post digest as a rich message
- **Personalization** — different digest variants for different roles
- **Feedback loop** — track which articles get clicked, tune relevance model
- **Breaking news alerts** — real-time monitoring for high-impact events
- **Podcast integration** — transcribe and summarize procurement podcasts
- **Internal data integration** — reference internal project status alongside external news
