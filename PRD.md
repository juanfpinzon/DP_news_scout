# PRD: Digital Procurement News Scout

## Document Info

| Field | Value |
|-------|-------|
| **Project** | Digital Procurement News Scout (DPNS) |
| **Owner** | Digital Procurement Team — PepsiCo |
| **Audience** | Senior Leadership, Digital Procurement |
| **Status** | Draft v1.0 |
| **Date** | 2026-03-26 |

---

## 1. Problem Statement

Digital Procurement senior leaders at PepsiCo need to stay current on procurement technology trends, platform updates, AI advancements, and market shifts — but have no time to monitor 20+ specialized sources daily. Relevant intelligence is scattered across analyst firms, consulting publications, vendor blogs, podcasts, and trade media.

**Today's pain:**
- Leaders spend 30–60 min/day scanning fragmented sources (or skip it entirely)
- No single curated view tailored to a **Digital Procurement transformation context**
- Generic news aggregators lack the editorial lens of "why does this matter to us?"
- Team members surface articles ad-hoc in Slack/email — inconsistent, duplicated, incomplete

**Desired outcome:**
A daily, automated, executive-quality email briefing delivered at 9:00 AM (CET) that surfaces the most relevant procurement and digital transformation news, explains why each item matters to the team, and requires zero manual curation.

---

## 2. Target Users

### Primary
- **Digital Procurement Senior Leadership** — VP/Director-level leaders driving the digital transformation of PepsiCo's procurement org
- Need: fast situational awareness, strategic signals, competitive intelligence

### Secondary
- **Digital Procurement Program Managers** — people executing S2P/P2P implementations
- Need: platform updates, vendor moves, implementation best practices
- **Extended Procurement Leadership** — CPO office, category leaders
- Need: high-level awareness of digital trends affecting procurement strategy

---

## 3. Content Strategy

### 3.1 Source Taxonomy & Priority

Sources are grouped into tiers. The system should attempt all Tier 1 sources daily; Tier 2 and 3 are supplemental.

#### Tier 1 — Must-fetch daily
| Source | Type | Focus |
|--------|------|-------|
| Gartner (procurement blog) | Analyst | Market trends, Magic Quadrant updates |
| McKinsey (operations/procurement) | Consulting | Strategic POVs, digital transformation |
| Spend Matters | Trade media | S2P/P2P platform news, vendor analysis |
| CPO Rising | Trade media | Procurement leadership, strategy |
| Art of Procurement | Trade media/podcast | Practitioner insights |
| Procurement Leaders | Community | Executive perspectives |

#### Tier 2 — Fetch daily, lower priority
| Source | Type | Focus |
|--------|------|-------|
| SAP / SAP Ariba (blog/news) | Vendor ★ | Platform updates, roadmap — **actively used** |
| Archlet (blog/news) | Vendor ★ | Sourcing optimization — **actively used** |
| Keelvar (blog/news) | Vendor ★ | Sourcing automation — **actively used** |
| Selectica / Determine (blog/news) | Vendor ★ | Contract management — **actively used** |
| SpendhQ (blog/news) | Vendor ★ | Spend analytics — **actively used** |
| Pirt (blog/news) | Vendor ★ | Procurement platform — **actively used** |
| Tirzo (blog/news) | Vendor ★ | Procurement platform — **actively used** |
| Coupa (blog/news) | Vendor | Platform updates, benchmarks |
| Ivalua (blog/news) | Vendor | Platform updates |
| Jaggaer (blog/news) | Vendor | Platform updates |
| GEP (blog/insights) | Vendor | Thought leadership |
| Zip (blog) | Vendor | Intake/orchestration trends |
| Sievo (blog) | Vendor | Spend analytics, data insights |
| Procurious | Community | Networking, opinions |
| Supply Chain Digital | Trade media | Supply chain + procurement |

> ★ = platform currently in use by the Digital Procurement team; score any news about these vendors +2 bonus relevance points.

#### Tier 2 (continued) — Consulting & Advisory
| Source | Type | Focus |
|--------|------|-------|
| Deloitte (procurement insights) | Consulting | Industry reports, digital procurement |
| Accenture (procurement insights) | Consulting | Technology trends, procurement transformation |
| Hackett Group (procurement) | Consulting/Advisory | Benchmarking, best practices, procurement maturity |
| PwC (procurement) | Consulting | Procurement strategy, digital transformation |
| EY (procurement) | Consulting | Procurement advisory, supply chain |
| Kearney (procurement) | Consulting | Procurement strategy, sourcing excellence |
| Digital Procurement World | Community/Events | Industry news, digital procurement trends |

#### Tier 2 (continued) — Peer / Competitor Intelligence
| Source | Type | Focus |
|--------|------|-------|
| Unilever (newsroom) | Peer CPG | Procurement & supply chain announcements |
| Mars (newsroom) | Peer CPG | Procurement & digital transformation news |
| Mondelez (newsroom) | Peer CPG | Procurement & supply chain initiatives |
| Procter & Gamble (newsroom) | Peer CPG | Procurement technology & strategy |
| Kraft Heinz (newsroom) | Peer CPG | Procurement & operations news |

> Peer CPG newsrooms are monitored for procurement-related content only. The LLM relevance filter ensures only articles about procurement, sourcing, supply chain digitization, or related topics are surfaced — general corporate news is excluded.

#### Tier 3 — Fetch if relevant articles detected
| Source | Type | Focus |
|--------|------|-------|
| Forbes (procurement/supply chain) | Mainstream | Executive-facing trends |
| HBR | Mainstream | Strategy, leadership |
| CIO Magazine | Mainstream | IT/digital transformation |
| BCG (operations) | Consulting | Digital strategy |

### 3.2 Content Filtering Criteria

Articles must pass **at least one** relevance gate:
1. **Direct procurement tech** — S2P, P2P, procurement platforms, sourcing tech, CLM, SRM
2. **AI/ML in procurement** — GenAI for procurement, autonomous procurement, AI-assisted sourcing
3. **Digital transformation** — ERP modernization, cloud migration, process automation in procurement
4. **Market moves** — M&A in procuretech, funding rounds, vendor partnerships, analyst ratings
5. **Leadership/strategy** — CPO agenda, procurement org design, talent, change management
6. **Adjacent relevance** — Supply chain digitization, ESG compliance tech, risk management platforms
7. **Competitive intelligence** — Peer CPG companies (Unilever, Mars, Mondelez, P&G, Kraft Heinz, and similar) publishing about their procurement digitization, sourcing strategies, supplier programs, or technology adoption

Articles that are purely marketing fluff, product demos without substance, or unrelated to procurement should be filtered out.

### 3.3 Editorial Voice & Format

The digest should read like a **trusted advisor's morning brief**, not a raw RSS dump.

**Tone:** executive, concise, opinionated (explains "so what?"), professional but not stiff
**Structure per article:**
- Headline (linked to source)
- 2–3 sentence summary
- "Why it matters to us" — 1–2 sentences connecting to Digital Procurement transformation context
- Source attribution + publication date

**Overall digest structure:**
1. **Header** — branded, date, issue number
2. **Top Story** (1 item) — the single most impactful piece for the team
3. **Key Developments** (3–5 items) — important news grouped loosely by theme
4. **On Our Radar** (2–4 items) — emerging trends, vendor moves, things to watch
5. **Quick Hits** (3–5 items) — one-line summaries with links for anything notable but not deep
6. **Footer** — disclaimer, feedback link, unsubscribe (if applicable)

---

## 4. Functional Requirements

### 4.1 Content Ingestion (Fetcher)

| ID | Requirement | Priority |
|----|-------------|----------|
| F-01 | Fetch latest articles from all Tier 1–3 sources via RSS, web scraping, or API | P0 |
| F-02 | Maintain a source registry (URL, type, tier, scraping method, selectors) | P0 |
| F-03 | Deduplicate articles across sources (same story covered by multiple outlets) | P0 |
| F-04 | Filter to articles published in the last 7 days | P0 |
| F-05 | Handle source failures gracefully — log errors, continue with remaining sources | P0 |
| F-06 | Store raw fetched articles in a local DB/file store with metadata | P1 |
| F-07 | Respect robots.txt and rate limits per source | P0 |
| F-08 | Support adding/removing sources via config file (no code change) | P1 |

### 4.2 Content Analysis (Digest Engine)

| ID | Requirement | Priority |
|----|-------------|----------|
| A-01 | Use  OpenRouter API to score relevance of each article | P0 |
| A-02 | Generate 2–3 sentence summary for each relevant article | P0 |
| A-03 | Generate "Why it matters" editorial context for each article | P0 |
| A-04 | Rank and categorize articles into Top Story / Key Dev / Radar / Quick Hits | P0 |
| A-05 | Deduplicate semantically similar articles (not just URL match) | P1 |
| A-06 | Limit total digest to ~12–15 items max to respect reader time | P0 |
| A-07 | Support model fallback within OpenRouter (e.g., swap to a cheaper/faster model) if the primary model is unavailable | P1 |
| A-08 | Include a system prompt that encodes PepsiCo Digital Procurement context | P0 |

### 4.3 Email Composition (Renderer)

| ID | Requirement | Priority |
|----|-------------|----------|
| E-01 | Generate a responsive HTML email compatible with Outlook, Gmail, Apple Mail | P0 |
| E-02 | Professional visual design — clean, branded, scannable | P0 |
| E-03 | Each article links to original source | P0 |
| E-04 | Mobile-friendly (single column, readable fonts, tap targets) | P0 |
| E-05 | Include date, issue number, source count in header | P1 |
| E-06 | Plain-text fallback for accessibility | P1 |
| E-07 | Support a "feedback" link or reply-to for recipients | P2 |

### 4.4 Delivery (Sender)

| ID | Requirement | Priority |
|----|-------------|----------|
| D-01 | Send email daily at 9:00 AM CET to a configurable recipient list | P0 |
| D-02 | Use AgentMail for email delivery (supports bidirectional comms and agentic workflows) | P0 |
| D-03 | Support a distribution list managed via config/env vars | P0 |
| D-04 | Retry on send failure (up to 3 attempts with backoff) | P1 |
| D-05 | Log delivery status per run | P0 |
| D-06 | Skip sending if zero relevant articles found (send "no news" notice instead) | P1 |

### 4.5 Scheduling & Orchestration

| ID | Requirement | Priority |
|----|-------------|----------|
| S-01 | Daily cron job / scheduler triggers the full pipeline | P0 |
| S-02 | Pipeline: Fetch → Analyze → Render → Send (sequential, fail-safe) | P0 |
| S-03 | Full run should complete in < 10 minutes | P0 |
| S-04 | Support manual trigger for testing / ad-hoc runs | P1 |
| S-05 | Logging for each pipeline stage (structured, queryable) | P0 |

---

## 5. Non-Functional Requirements

| Area | Requirement |
|------|-------------|
| **Reliability** | Must send on ≥95% of weekdays; graceful degradation if some sources fail |
| **Cost** | Target < $50/month total (API + email + hosting) |
| **Security** | API keys in env vars / secrets manager; no PII in logs; HTTPS only |
| **Maintainability** | Source list editable via config; prompt templates in separate files |
| **Observability** | Structured logs; alert on pipeline failure; weekly summary of sources fetched |
| **Scalability** | Initially 5–20 recipients; architecture should support 100+ without redesign |

---

## 6. Technical Architecture (Proposed)

```
┌─────────────────────────────────────────────────────────┐
│                    SCHEDULER (cron)                      │
│              Triggers daily at 8:00 AM ET               │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              1. FETCHER MODULE                          │
│  - Reads source_registry.yaml                           │
│  - RSS parser (feedparser) + web scraper (httpx + BS4)  │
│  - Dedup by URL, filter by date                         │
│  - Output: raw_articles[] → JSON                        │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              2. DIGEST ENGINE                           │
│  - OpenRouter API (`openai` SDK, custom base_url)       │
│  - Relevance scoring + filtering                        │
│  - Summary + "Why it matters" generation                │
│  - Categorization + ranking                             │
│  - Output: digest_items[] → JSON                        │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              3. RENDERER MODULE                         │
│  - Jinja2 + premailer HTML email template               │
│  - Inlines CSS for email client compatibility           │
│  - Plain-text fallback generation                       │
│  - Output: email.html + email.txt                       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              4. SENDER MODULE                           │
│  - AgentMail API                                        │
│  - Reads recipient_list from config                     │
│  - Retry logic + delivery logging                       │
│  - Output: delivery_log entry                           │
└─────────────────────────────────────────────────────────┘
```

### Tech Stack Recommendation

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.11+ | Best ecosystem for scraping, LLM APIs, email |
| Scraping | `httpx` + `beautifulsoup4` + `feedparser` | Async HTTP, robust HTML parsing, RSS native |
| LLM | `openai` SDK via OpenRouter (`base_url="https://openrouter.ai/api/v1"`) | Single gateway to all models; model swappable via config |
| Email template | Jinja2 + premailer inline CSS | Matches the current implementation and keeps email-client-safe output |
| Email sending | AgentMail | Built for AI agents; bidirectional email; simple Python SDK; free tier available |
| Scheduling | GitHub Actions cron / Railway cron / `crontab` on a VPS | Zero-infra option (GH Actions) or cheap VPS |
| Storage | SQLite (local) or Supabase (hosted) | Article dedup, run history, delivery logs |
| Config | YAML files + `.env` for secrets | Human-readable, easy to edit |

---

## 7. LLM Prompt Strategy

### 7.1 Relevance Scoring Prompt

The system prompt should encode:
- Who the readers are (Digital Procurement leaders at a major CPG company)
- What transformation they're executing (full S2P/P2P digital transformation)
- Platforms/vendors **actively in use**: SAP / SAP Ariba, Archlet, Keelvar, Selectica, SpendhQ, Pirt, Tirzo — any news about these tools gets a +2 relevance bonus
- Scoring rubric: 1–10 relevance with reasoning

### 7.2 Digest Composition Prompt

After filtering, a second LLM call takes the top ~15 articles and:
- Writes executive summaries
- Generates the "Why it matters" angle
- Assigns category (Top Story / Key Dev / Radar / Quick Hit)
- Orders by impact

### 7.3 Prompt Management

Prompts should live in `/prompts/` as separate `.txt` or `.md` files:
- `relevance_scoring.md`
- `digest_composition.md`
- `context_preamble.md` (shared context about the team)

This allows non-developers to refine the editorial voice without touching code.

---

## 8. Email Design Spec

### Visual Requirements
- **Max width:** 880px
- **Font:** `GT Pressura LCG Black` for headings with serif fallbacks; `Helvetica Neue` / Helvetica / Arial for body copy
- **Header:** Deep navy (#1a2332) with date, issue number, and brand masthead
- **Top Story:** Highlighted with accent border (teal #0891b2) and tinted card background
- **Section headers:** Bold, uppercase, subtle divider line
- **Article cards:** Tinted backgrounds (`#f0f9fb`, `#f1faee`) with subtle borders and clear hierarchy
- **Links:** Teal (#0891b2), underlined on hover
- **Footer:** Deep navy, smaller text, feedback link
- **Responsive:** Single-column, 16px+ body text for mobile

### Example Layout (ASCII)
```
╔══════════════════════════════════════════╗
║  🔍 DIGITAL PROCUREMENT NEWS SCOUT      ║
║  March 26, 2026 · Issue #142            ║
╠══════════════════════════════════════════╣
║                                          ║
║  ★ TOP STORY                             ║
║  ┌────────────────────────────────────┐  ║
║  │ Headline (linked)                  │  ║
║  │ Summary text...                    │  ║
║  │ 💡 Why it matters: ...             │  ║
║  │ Source · Date                      │  ║
║  └────────────────────────────────────┘  ║
║                                          ║
║  KEY DEVELOPMENTS                        ║
║  ┌────────────────────────────────────┐  ║
║  │ Article 1...                       │  ║
║  ├────────────────────────────────────┤  ║
║  │ Article 2...                       │  ║
║  └────────────────────────────────────┘  ║
║                                          ║
║  ON OUR RADAR                            ║
║  ┌────────────────────────────────────┐  ║
║  │ Article 1...                       │  ║
║  └────────────────────────────────────┘  ║
║                                          ║
║  QUICK HITS                              ║
║  • One-liner + link                      ║
║  • One-liner + link                      ║
║                                          ║
║  ─────────────────────────────────────   ║
║  Curated by DPNS · Feedback · Settings   ║
╚══════════════════════════════════════════╝
```

---

## 9. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Delivery rate | ≥ 95% of weekdays | Delivery logs |
| Open rate | ≥ 60% | Email provider analytics |
| Content relevance | ≥ 80% of articles rated useful | Monthly survey (5 questions) |
| Time to read | < 5 minutes | Estimated from word count |
| Source coverage | ≥ 80% of Tier 1 sources fetched daily | Fetch logs |
| Pipeline reliability | < 5% failure rate | Error logs |
| Cost per month | < $50 | Billing dashboards |

---

## 10. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Source websites block scraping | Content gaps | Use RSS where available; rotate user agents; have backup sources |
| LLM API outage | No digest generated | OpenRouter fallback; cache last successful digest; alert on failure |
| Low relevance quality | Leadership loses trust | Weekly prompt tuning; feedback loop; manual review first 2 weeks |
| Email deliverability issues | Digest goes to spam | Use verified domain; warm up sender; SPF/DKIM/DMARC setup |
| Cost overrun on LLM tokens | Budget exceeded | Token budgets per run; use smaller model for scoring, larger for composition |
| Source RSS/page structure changes | Silent fetch failures | Monitor source fetch success rates; alert on drops |

---

## 11. Out of Scope (v1)

- Web dashboard / archive UI (future: searchable history)
- Slack/Teams integration (future: post digest to channel)
- Personalized digests per reader (future: role-based filtering)
- Real-time alerts for breaking news
- Podcast transcription and summarization
- Integration with internal procurement systems

---

## 12. Decisions & Resolved Questions

| # | Question | Decision |
|---|----------|----------|
| 1 | **Sender identity** | AgentMail inbox. `AGENTMAIL_INBOX_ID` is provided by the user at setup time — the setup process must prompt for it. |
| 2 | **Internal vs. external hosting** | Fully external: GitHub Actions (scheduler) + OpenRouter (LLM) + AgentMail (email). No PepsiCo infra required. |
| 3 | **Platforms in use** | SAP / SAP Ariba, Archlet, Keelvar, Selectica, SpendhQ, Pirt, Tirzo. Used to tailor the relevance scoring prompt and "Why it matters" context. |
| 4 | **Recipient management** | Admin-managed config file (`config/recipients.yaml`) for v1. Self-serve subscribe/unsubscribe is a v2 enhancement. |
| 5 | **Content approval** | No human review gate for v1. Revisit after PoC — manual spot-checks during the dry-run week (Task 7.1.1) will inform whether a review step is needed. |
| 6 | **API keys** | Juan Pinzon provisions OpenRouter and AgentMail keys for development and PoC testing. Production key management and spend limits to be decided with Tatjana. |
