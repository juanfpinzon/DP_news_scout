## Search Fallback For Blocked Or Empty Sources

### Status
Implemented on April 5, 2026.

Current shipped behavior:
- active sources use Brave fallback after direct fetch failure or direct `0 article` runs when `search_fallback_enabled` is on,
- inactive sources can run as fallback-only via `fallback_search.include_when_inactive: true`,
- accepted fallback publishers come from `config/search_fallback_allowlist.yaml`,
- fallback candidates are re-checked against their own `robots.txt`,
- persisted articles now keep `origin_source` and `discovery_method`.

Latest live verification on April 5, 2026:
- `python scripts/run_manual.py --sources-only` completed with `16/16` sources succeeded,
- SAP Ariba now succeeds on the fallback-only path,
- the current SAP Ariba query returned `0` allowlisted articles in the live window, so the source contributes coverage only when reputable third-party reporting is available.

### Summary
Add a new **Brave Search-backed fallback ingestion mode** so DPNS can recover coverage for sources that:
- fail to fetch directly, or
- fetch successfully but return **0 direct articles** inside the recency window.

Fallback articles must come only from a **strict, editable allowlist** of trusted publishers. The digest should display the **actual publisher name** only, with no visible fallback badge. Inactive sources like SAP Ariba can participate as **fallback-only** sources.

### Key Changes
- Add a new search fallback path to the fetcher.
  - Trigger fallback when a source has:
    - direct fetch failure (`robots`, timeout, HTTP error, parse error), or
    - direct fetch success with `0` recent articles.
  - For inactive-but-enabled fallback sources, skip direct fetch and run search immediately.
  - Result cap is configurable per source from **1 to 3**, default `3`.

- Make fallback behavior explicit in source config.
  - Extend `config/sources.yaml` with optional `fallback_search`:
    - `enabled: bool`
    - `include_when_inactive: bool`
    - `query: str | null`
    - `max_results: int | null` (`1..3`)
  - Seed SAP Ariba as:
    - `active: false`
    - `fallback_search.enabled: true`
    - `fallback_search.include_when_inactive: true`
    - `fallback_search.query: "\"SAP Ariba\" procurement OR spend management OR sourcing OR supplier management"`
    - `fallback_search.max_results: 3`

- Add global fallback settings and provider config.
  - Add to settings/env:
    - `search_fallback_enabled`
    - `search_fallback_provider=brave`
    - `search_fallback_timeout_seconds`
    - `search_fallback_max_results_per_source`
    - `BRAVE_SEARCH_API_KEY`
  - Because the Brave key is already in local `.env` and GitHub secrets, the plan assumes provider auth is available.

- Add an editable allowlist config.
  - Create `config/search_fallback_allowlist.yaml`.
  - Match on **registrable domain** plus common subdomains.
  - Structure:
    - `publishers`: `{domain, label, group, active}`
    - `deny_domains`
  - Use this config as the single source of truth so the list can be tuned without code changes.

- Keep the quality gate strict.
  - Accept only article URLs whose publisher domain is in the allowlist.
  - Reject:
    - `wikipedia.org`
    - social/user-generated platforms (`reddit.com`, `x.com`, `facebook.com`, `linkedin.com`, `youtube.com`, `medium.com`, `substack.com`)
    - known low-quality or AI-summary domains added to the denylist
    - the original blocked source’s own domain
  - Do not use LLM-based “AI-generated content” detection; the allowlist is the enforcement mechanism.

- Respect robots for fallback candidates too.
  - Search is discovery only.
  - Before fetching any candidate article for metadata extraction, run the existing robots/rate-limit checks on that candidate publisher domain.
  - Skip any candidate article whose own publisher disallows crawling.

- Preserve accurate attribution.
  - Extend fetched article metadata with:
    - `origin_source` = configured source that triggered fallback, e.g. `SAP Ariba`
    - `discovery_method` = `rss`, `scrape`, or `search_fallback`
  - Set `source` to the **actual publisher** for analyzer balancing, storage, and rendering.

- Define source success accounting clearly.
  - Direct failure + fallback success/empty = source counted as succeeded.
  - Direct failure + fallback error = source counted as failed.
  - Direct success with `0` articles + fallback success/empty/error = source counted as succeeded, because the primary source was reachable; fallback failure is logged as enrichment failure only.
  - Fallback-only source + fallback error = source counted as failed.

### Initial Allowlist Seeds
Seed the allowlist with these groups, all editable later in `config/search_fallback_allowlist.yaml`.

- Global mainstream and business:
  - `reuters.com`
  - `bloomberg.com`
  - `ft.com`
  - `wsj.com`
  - `apnews.com`
  - `cnbc.com`
  - `bbc.com`
  - `economist.com`
  - `asia.nikkei.com`

- Procurement and supply-chain trade:
  - `spendmatters.com`
  - `cporising.com`
  - `procurementmag.com`
  - `supplychaindive.com`
  - `sdcexec.com`
  - `conference.dpw.ai`
  - `artofprocurement.com`

- Digital transformation and enterprise tech trade:
  - `cio.com`
  - `ciodive.com`
  - `computerweekly.com`
  - `informationweek.com`
  - `zdnet.com`
  - `techtarget.com`
  - `venturebeat.com`
  - `hbr.org`

- Existing trusted DPNS editorial domains in categories `trade_media` and `mainstream` should also be imported automatically into the allowlist seed set unless explicitly disabled.

### Important Interface Changes
- `Source` config gains optional `fallback_search`.
- `Settings` gains global search-fallback settings and Brave API support.
- `RawArticle` and persisted article records gain `origin_source` and `discovery_method`.
- Fetcher dispatch supports four operational outcomes:
  - direct fetch
  - fallback after direct failure
  - fallback after direct `0 articles`
  - fallback-only for inactive configured sources

### Test Plan
- Config and validation
  - parse `fallback_search` blocks
  - enforce `max_results` range `1..3`
  - include inactive fallback-only sources in the run
  - load allowlist config and denylist config correctly

- Fetcher behavior
  - direct `robots` failure triggers fallback
  - direct timeout / HTTP / parse failure triggers fallback
  - direct success with `0` articles triggers fallback
  - direct success with articles does not trigger fallback
  - inactive fallback-only source runs only search path

- Quality gate
  - accept only allowlisted publisher domains
  - reject Wikipedia, social/user-generated, denylisted, and original blocked domains
  - reject fallback candidate articles blocked by their own `robots.txt`

- Attribution and digest
  - fallback article stores `origin_source`
  - fallback article stores `discovery_method=search_fallback`
  - displayed digest source is the actual publisher only
  - per-source balancing uses actual publisher names, not blocked source names

- Acceptance scenarios
  - SAP Ariba fallback-only source yields up to 3 reputable third-party articles when Brave returns trusted matches
  - a reachable source that returns `0` recent articles can still contribute fallback coverage
  - fallback provider outage does not falsely mark a directly reachable-but-empty source as failed

### Last Task: Documentation Updates
- Update all project documentation after implementation as the **final task**.
- Cover:
  - new search-fallback behavior and trigger conditions
  - Brave API dependency and required secrets/env vars
  - new source config fields and allowlist config
  - source-count/runtime changes if SAP Ariba or other sources become fallback-only
  - operator guidance for tuning allowlist entries and per-source fallback queries
- Update at minimum:
  - `README.md`
  - `AGENTS.md`
  - `CLAUDE.md`
  - `PLAN.md`
  - any search-fallback-specific planning doc such as `PLAN_SearchFallback.md`

### Assumptions And Defaults
- Brave Search API is the only planned provider for v1.
- The initial allowlist is curated manually, stored in config, and expected to evolve.
- “Not AI-generated” is implemented operationally as **strict domain allowlisting**, not content-style classification.
- Fallback is global for eligible sources, but per-source query overrides remain available and should be used for ambiguous brands like SAP Ariba.
- The initial allowlist seed is informed by official publisher positioning and editorial-brand reputation, including [Reuters Trust Principles](https://www.thomsonreuters.com/en/about-us/trust-principles.html), [Reuters fact sheet](https://www.thomsonreuters.com/content/dam/openweb/documents/pdf/reuters-news-agency/fact-sheet/reuters-fact-sheet.pdf), [AP official site](https://www.ap.org/), [Bloomberg News & Media](https://www.bloomberg.com/company/what-we-do/news-media/), [Financial Times commercial profile](https://ft-commercial-admin.phantom.tools/news-insights/ft-named-europes-leading-business-publication-for-6th-year-running/), [Nikkei Asia RSS/info](https://info.asia.nikkei.com/rss), [Nikkei Asia overview](https://corporate.asia.nikkei.com/why-nikkei-asia), and the live SAP block at [news.sap.com/robots.txt](https://news.sap.com/robots.txt).
