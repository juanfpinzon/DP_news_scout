# Digital Procurement News Scout

Digital Procurement News Scout (DPNS) is a daily procurement and digital transformation digest that:

- monitors 20 currently configured active sources (11 RSS, 9 scrape), plus 3 fallback-only sources,
- fetches articles from RSS, approved scrape sources, and Brave-backed search fallback,
- scores procurement and macro relevance with Claude via OpenRouter,
- composes both the main executive briefing and a separate global macro section with Sonnet via OpenRouter,
- renders HTML and plain-text email output,
- sends the digest through AgentMail.

## What The App Does Today

- Live pipeline entry point: `python -m src.main`
- Manual operator entry point: `python scripts/run_manual.py`
- Production scheduler: `cron-job.org` calling GitHub Actions via `repository_dispatch`
- Mock-render preview tool: `python scripts/test_email.py`
- SQLite storage: `data/dpns.db`
- Live fetch freshness window: 7 days
- Current issue number override: fixed to `0` via `config/settings.yaml`

## Quick Start

1. Create a Python 3.11+ virtual environment.
2. Install dependencies:

```bash
pip install -e .
```

3. Copy the example env file and fill in secrets:

```bash
cp .env.example .env
```

4. Set the required secrets in `.env`:

```bash
OPENROUTER_API_KEY=sk-or-...
AGENTMAIL_API_KEY=...
AGENTMAIL_INBOX_ID=...
EMAIL_FROM=...
BRAVE_SEARCH_API_KEY=...   # required for search fallback
```

5. Run a safe local test first:

```bash
python scripts/run_manual.py --dry-run
```

## Runtime Model

The main pipeline is:

`Fetcher -> Analyzer -> Renderer -> Sender`

Important current behavior:

- The pipeline fetches live sources on every normal run.
- The analyzer now runs two parallel tracks: the main procurement-tech digest and a separate `global_news` macro track that feeds the `Global Macro Briefing` section.
- The live fetch window for RSS and scrape ingestion is currently 7 days.
- Stored articles in SQLite are used for recent-URL dedup, not as an LLM cache.
- Successful live runs persist article metadata back into SQLite.
- `config/settings.yaml` currently sets `issue_number_override: 0`, so all live/manual runs show `Issue #0`.
- Digest article selection is source-balanced with `max_digest_items_per_source: 3`.
- Desktop email width is configurable and currently set to `880px`.
- RSS and scrape fetches check `robots.txt` before fetching source content.
- If the DPNS-managed HTTP client gets `401/403` on `robots.txt`, the fetcher performs one fallback retry for the robots file only.
- If that retry also fails, the fetcher stays conservative and treats the source as disallowed unless `robots.txt` is confirmed missing (`404/410`).
- Caller-supplied `httpx.AsyncClient` instances are never bypassed during robots checks.
- When `search_fallback_enabled` is on, active sources automatically try Brave search fallback after a direct fetch failure or a direct fetch that returns `0` recent articles.
- Inactive sources can participate as fallback-only sources through `fallback_search.include_when_inactive: true`.
- Search fallback accepts only allowlisted publisher domains from `config/search_fallback_allowlist.yaml`, rejects common low-trust/user-generated domains, and re-checks `robots.txt` on the candidate publisher before fetching the article page.
- Fallback articles keep the actual publisher name as `source` and store `origin_source` plus `discovery_method=search_fallback` internally for audit/debugging.
- Fallback publisher groups now determine the stored article category, so Reuters/BBC/CNN/Bloomberg/FT fallback hits can land in the macro track even when discovered from procurement sources.
- Fetch progress now emits a per-source fallback summary such as `Brave returned 10 results; 8 blocked by allowlist; 2 stale.` so zero-result fallbacks are easier to diagnose from run logs alone.

## Configuration

Primary runtime config lives in `config/settings.yaml`.

Current notable settings:

```yaml
max_digest_items: 15
max_digest_items_per_source: 3
relevance_threshold: 6
global_news_relevance_threshold: 5
global_news_max_items: 3
global_news_max_per_source: 2
llm_scoring_model: anthropic/claude-haiku-4.5
llm_digest_model: anthropic/claude-sonnet-4-6
llm_model_fallback: anthropic/claude-haiku-4.5
rss_lookback_hours: 168
dedup_window_days: 7
email_max_width_px: 880
issue_number_override: 0
search_fallback_enabled: true
search_fallback_provider: brave
search_fallback_timeout_seconds: 15
search_fallback_max_results_per_source: 3
```

Meaning:

- `max_digest_items`: total article slots passed into digest composition.
- `max_digest_items_per_source`: soft per-source cap before the selector fills remaining slots.
- `relevance_threshold`: cutoff for the main procurement scoring track.
- `global_news_relevance_threshold`: cutoff for the macro scoring track.
- `global_news_max_items`: maximum item count for the `Global Macro Briefing` section.
- `global_news_max_per_source`: soft per-source cap for macro briefing selection.
- `llm_scoring_model`: primary model used for batched relevance scoring.
- `llm_digest_model`: primary model used for final digest composition.
- `llm_model_fallback`: shared fallback model used by both analyzer stages on retry/fallback conditions.
- `rss_lookback_hours`: live fetch freshness window for RSS and scrape ingestion, currently 7 days.
- `dedup_window_days`: recent URL dedup window against SQLite.
- `email_max_width_px`: desktop max width for the HTML digest.
- `issue_number_override`: when set, overrides dynamic issue numbering. Remove it or set it to `null` to restore dynamic numbering later.
- `search_fallback_enabled`: global switch for Brave-backed fallback on blocked, failed, or empty active sources.
- `search_fallback_provider`: currently fixed to `brave`.
- `search_fallback_timeout_seconds`: timeout for the Brave request itself.
- `search_fallback_max_results_per_source`: default cap for accepted fallback articles per source, clamped to `1..3`.

Legacy compatibility:

- Older configs can still provide `llm_model` or `LLM_MODEL`.
- If stage-specific settings are absent, that legacy value is used for both scoring and digest composition.
- If stage-specific settings are present, they override the legacy single-model alias.

Environment variables can also override settings. Relevant optional overrides include:

```bash
DRY_RUN=false
PIPELINE_TIMEOUT=600
MAX_DIGEST_ITEMS=15
MAX_DIGEST_ITEMS_PER_SOURCE=3
RELEVANCE_THRESHOLD=6
GLOBAL_NEWS_RELEVANCE_THRESHOLD=5
GLOBAL_NEWS_MAX_ITEMS=3
GLOBAL_NEWS_MAX_PER_SOURCE=2
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
BRAVE_SEARCH_API_KEY=...
```

## Search Fallback

Search fallback is config-driven:

- `config/sources.yaml` can add a `fallback_search` block per source with `enabled`, `include_when_inactive`, `query`, and `max_results`.
- Active sources fall back automatically when the global switch is on unless a source explicitly sets `fallback_search.enabled: false`.
- Inactive sources only run through search when `fallback_search.enabled: true` and `include_when_inactive: true`.
- `config/search_fallback_allowlist.yaml` is the editable allowlist/denylist for accepted fallback publishers, with additional trusted publishers layered on top of the auto-included active DPNS `trade_media`, `mainstream`, and `global_news` domains.
- Global-news publishers in that allowlist are shared with procurement-source fallback too; the publisher `group` controls the category assigned to the recovered article.

Operator guidance:

- Use `fallback_search.query` for ambiguous brands or vendor names where the default query of `"Source Name"` is too weak.
- The current tuned query set covers `SAP Ariba`, `Archlet`, `Keelvar`, `SpendHQ`, `GEP`, `Zip`, `Sievo`, `Digital Procurement World`, and `Mars Newsroom`.
- If a fallback source still returns `0` articles, inspect the new summary line in the fetch log first. The most common reasons are:
  - `blocked by allowlist`: Brave found articles, but the publisher domain is not currently trusted.
  - `stale`: the article was outside the current `rss_lookback_hours` window.
  - `article fetch failed` or `blocked by robots.txt`: the candidate publisher page could not be fetched compliantly.
- Expand `config/search_fallback_allowlist.yaml` only with editorial publishers you are comfortable treating as trusted input to the digest.

Current notable example:

- `SAP Ariba` remains uncrawlable directly because `news.sap.com` blocks generic crawlers, but it now runs as a fallback-only source using Brave plus the allowlist gate.
- Recent trade-media allowlist additions include `globaltrademag.com`, `dcvelocity.com`, `thescxchange.com`, and `cpostrategy.media`.
- Global macro fallback coverage now includes trusted publishers such as `Reuters`, `Al Jazeera`, `DW`, `BBC`, `CNN`, `Bloomberg`, and `Financial Times`.

## LLM Model Split

The analyzer is now intentionally split by task:

- Relevance scoring uses `llm_scoring_model` and defaults to `anthropic/claude-haiku-4.5`.
- Digest composition uses `llm_digest_model` and defaults to `anthropic/claude-sonnet-4-6`.
- Both stages share `llm_model_fallback`.

The digest also now has a dedicated macro track:

- `global_news` sources are scored with `prompts/global_news_scoring.md`.
- Procurement sources are still scored with `prompts/relevance_scoring.md`.
- The macro section is composed independently into `Global Macro Briefing`.
- If the macro track fails, the primary procurement digest still completes and sends.

This is the default cost-optimization path:

- scoring is the high-call-volume, lower-complexity stage,
- composition is the lower-volume, quality-sensitive stage.

## Manual Run Modes

The main operator tool is:

```bash
python scripts/run_manual.py
```

Supported flags:

- `--dry-run`
  Runs fetch, relevance scoring, composition, and rendering, but skips send.
- `--preview`
  Builds the live digest, writes HTML and plain-text preview files, and opens the HTML in a browser.
- `--preview-path PATH`
  Overrides the HTML preview destination. Default: `/tmp/preview.html`.
- `--plaintext-path PATH`
  Overrides the plain-text preview destination.
- `--test-email EMAIL`
  Builds the live digest and sends it only to the supplied recipient.
- `--sources-only`
  Fetches sources and reports counts without LLM analysis or email.
- `--ignore-seen-db`
  Fetches live sources while ignoring the current seen-URL dedup state in SQLite.
- `--reuse-seen-db`
  Skips network fetch and reuses the articles already stored in SQLite.

Mutually exclusive or constrained combinations:

- `--dry-run` cannot be combined with `--preview` or `--test-email`.
- `--sources-only` cannot be combined with `--dry-run`, `--preview`, or `--test-email`.
- `--ignore-seen-db` cannot be combined with `--reuse-seen-db`.
- `--sources-only` cannot be combined with `--reuse-seen-db`.

Additional behavior:

- Non-dry manual sends append a timestamped subject suffix such as `Manual run 13:40:29 UTC` or `Manual test 08:00:00 UTC` to reduce mail-client threading during repeated tests.
- Externally triggered workflow runs and direct `python -m src.main` runs keep the canonical subject line without the manual suffix.

## External Scheduler Trigger

The workflow supports two operational triggers:

- `workflow_dispatch` for manual GitHub UI or CLI runs
- `repository_dispatch` with event type `run-daily-digest` for external schedulers

Current production scheduling uses `cron-job.org` to POST a `repository_dispatch` event to the default branch workflow. Native GitHub scheduled workflows are not the active production trigger for this repository.

Example GitHub API call for an external scheduler:

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/juanfpinzon/DP_news_scout/dispatches \
  -d '{
    "event_type": "run-daily-digest",
    "client_payload": {
      "source": "external-scheduler",
      "dry_run": false
    }
  }'
```

Notes:

- Keep `dry_run` set to `false` for the real weekday send.
- Set `dry_run` to `true` if you want a safe external smoke test.
- `repository_dispatch` runs use the current default branch workflow definition.
- Recommended production schedule: weekdays at `09:00` in `Europe/Madrid`.

## Test Runs And SQLite Behavior

This is the most important part for local testing.

### Normal full pipeline run

```bash
python scripts/run_manual.py
```

Behavior:

- Fetches live sources.
- Applies recent-URL dedup against `data/dpns.db`.
- Runs LLM scoring and digest composition.
- Sends email to the configured recipient group.
- On successful live completion, persists fetched/scored/included article metadata into SQLite.

### Dry run

```bash
python scripts/run_manual.py --dry-run
```

Behavior:

- Fetches live sources.
- Applies recent-URL dedup against SQLite.
- Runs analysis and rendering.
- Does not send email.
- Does not persist final article state through the main pipeline success path.

### Preview or one-off test email

```bash
python scripts/run_manual.py --preview
python scripts/run_manual.py --test-email you@example.com
```

Behavior:

- Uses live fetched content by default.
- Does not use the full sender pipeline path.
- By default, does not refresh the stored article set in SQLite.

### Refetch even if articles were already seen

```bash
python scripts/run_manual.py --ignore-seen-db
python scripts/run_manual.py --preview --ignore-seen-db
python scripts/run_manual.py --test-email you@example.com --ignore-seen-db
```

Behavior:

- Fetches live sources again.
- Ignores the current seen-URL dedup state in SQLite.
- Still deduplicates duplicates within the current fetched batch.
- On full pipeline send, SQLite is refreshed through the normal success path.
- On preview/test-email, SQLite is also refreshed because this mode is explicitly for testing repeated runs.

Use this when:

- you want to rerun the same day’s content again,
- you just cleared the DB and want a fresh population,
- you need another test send without being blocked by the 7-day dedup window.

### Reuse what is already in SQLite

```bash
python scripts/run_manual.py --dry-run --reuse-seen-db
python scripts/run_manual.py --preview --reuse-seen-db
python scripts/run_manual.py --test-email you@example.com --reuse-seen-db
```

Behavior:

- Skips network fetch entirely.
- Loads stored articles from `data/dpns.db`.
- Re-runs scoring and digest composition against those stored article records.
- Reuse only includes stored articles inside `reuse_seen_db_window_days` and excludes undated scraped rows so stale scrape content cannot resurface as fresh.
- Useful for repeatable testing of rendering/send behavior without hitting live sources.
- Fails cleanly if the `articles` table is empty.

Use this when:

- you want repeatable previews from a known stored set,
- you want to test template/send changes without fetching new content,
- you want to resend from the current DB state.

Recommended validation flow for model changes:

```bash
python scripts/run_manual.py --dry-run --reuse-seen-db
```

In `data/logs/dpns.jsonl`, verify:

- relevance batches log `requested_model=anthropic/claude-haiku-4.5`
- digest composition logs `requested_model=anthropic/claude-sonnet-4-6`

### Sources only

```bash
python scripts/run_manual.py --sources-only
python scripts/run_manual.py --sources-only --ignore-seen-db
```

Behavior:

- Fetches sources only.
- No LLM analysis.
- No email.
- No SQLite persistence.

## Mock Email Tool

`scripts/test_email.py` is separate from the live pipeline. It renders a static mock digest for design/debugging.

Examples:

```bash
python scripts/test_email.py
python scripts/test_email.py --issue-number 0 --date "April 4, 2026"
python scripts/test_email.py --to you@example.com
```

Use this when:

- you want to test the template with predictable mock content,
- you want to inspect email layout without involving live fetch/LLM behavior.

## Common Testing Flows

Fresh full e2e from live sources, ignoring whatever was already seen:

```bash
python scripts/run_manual.py --ignore-seen-db
```

Preview the current stored article set without fetching:

```bash
python scripts/run_manual.py --preview --reuse-seen-db
```

Send a one-off test email from the current stored article set:

```bash
python scripts/run_manual.py --test-email you@example.com --reuse-seen-db
```

Send a one-off test email from live sources even if those URLs were already seen earlier today:

```bash
python scripts/run_manual.py --test-email you@example.com --ignore-seen-db
```

## Development

Run the main test suite:

```bash
pytest tests/ -v
```

Useful focused suites:

```bash
pytest tests/test_fetcher.py -v
pytest tests/test_pipeline.py -v
pytest tests/test_renderer.py -v
pytest tests/test_run_manual.py -v
```

Validate sources:

```bash
python scripts/seed_sources.py
```
