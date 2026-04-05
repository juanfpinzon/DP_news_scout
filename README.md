# Digital Procurement News Scout

Digital Procurement News Scout (DPNS) is a daily procurement and digital transformation digest that:

- monitors 16 currently configured active sources (6 RSS, 10 scrape),
- fetches articles from RSS and approved scrape sources,
- scores relevance with a lower-cost Claude model via OpenRouter,
- composes the executive briefing with Sonnet via OpenRouter,
- renders HTML and plain-text email output,
- sends the digest through AgentMail.

## What The App Does Today

- Live pipeline entry point: `python -m src.main`
- Manual operator entry point: `python scripts/run_manual.py`
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
- The live fetch window for RSS and scrape ingestion is currently 7 days.
- Stored articles in SQLite are used for recent-URL dedup, not as an LLM cache.
- Successful live runs persist article metadata back into SQLite.
- `config/settings.yaml` currently sets `issue_number_override: 0`, so all live/manual runs show `Issue #0`.
- Digest article selection is source-balanced with `max_digest_items_per_source: 3`.
- Desktop email width is configurable and currently set to `880px`.

## Configuration

Primary runtime config lives in `config/settings.yaml`.

Current notable settings:

```yaml
max_digest_items: 15
max_digest_items_per_source: 3
llm_scoring_model: anthropic/claude-haiku-4.5
llm_digest_model: anthropic/claude-sonnet-4-6
llm_model_fallback: anthropic/claude-haiku-4.5
rss_lookback_hours: 168
dedup_window_days: 7
email_max_width_px: 880
issue_number_override: 0
```

Meaning:

- `max_digest_items`: total article slots passed into digest composition.
- `max_digest_items_per_source`: soft per-source cap before the selector fills remaining slots.
- `llm_scoring_model`: primary model used for batched relevance scoring.
- `llm_digest_model`: primary model used for final digest composition.
- `llm_model_fallback`: shared fallback model used by both analyzer stages on retry/fallback conditions.
- `rss_lookback_hours`: live fetch freshness window for RSS and scrape ingestion, currently 7 days.
- `dedup_window_days`: recent URL dedup window against SQLite.
- `email_max_width_px`: desktop max width for the HTML digest.
- `issue_number_override`: when set, overrides dynamic issue numbering. Remove it or set it to `null` to restore dynamic numbering later.

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
LLM_SCORING_MODEL=anthropic/claude-haiku-4.5
LLM_DIGEST_MODEL=anthropic/claude-sonnet-4-6
LLM_MODEL_FALLBACK=anthropic/claude-haiku-4.5
RSS_LOOKBACK_HOURS=168
EMAIL_MAX_WIDTH_PX=880
ISSUE_NUMBER_OVERRIDE=0
```

## LLM Model Split

The analyzer is now intentionally split by task:

- Relevance scoring uses `llm_scoring_model` and defaults to `anthropic/claude-haiku-4.5`.
- Digest composition uses `llm_digest_model` and defaults to `anthropic/claude-sonnet-4-6`.
- Both stages share `llm_model_fallback`.

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
- Scheduled or direct `python -m src.main` runs keep the canonical subject line without the manual suffix.

## External Scheduler Trigger

The production workflow can be triggered in two ways:

- `workflow_dispatch` for manual GitHub UI or CLI runs
- `repository_dispatch` with event type `run-daily-digest` for external schedulers

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
