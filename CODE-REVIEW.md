# Code Review Report — DPNS

_Date: 2026-04-16_
_Scope: Full codebase review (correctness, readability, architecture, security, performance)_

Findings ordered by severity.

---

## CRITICAL

### C1. Robots policy fails open on 5xx/timeouts.   -- ** FIXED ** 
- **File:** [src/fetcher/common.py:157-213](src/fetcher/common.py)
- **Problem:** CLAUDE.md states robots.txt is deny-by-default, but the current implementation only denies on HTTP 401/403. Any other non-2xx status (500, 502, 503, timeouts, network errors) results in `_fetch_parser` returning `None`, and `RobotsPolicy.allows()` then returns `True` (fail-open). Also, `_parser_cache` is unbounded and lives for process lifetime.
- **Fix:** Treat unknown states as deny. Only allow when (a) `RobotFileParser` was successfully populated, or (b) the server returned a definitive 404/410. Add a bounded in-memory TTL cache of robots fetches per host.
  ```python
  # _fetch_parser: return a sentinel distinguishing "missing" from "unknown"
  # allows(): default to False when parser is None unless we recorded "missing"
  ```

### C2. Search-fallback allowlist bypassed by HTTP redirects -- ** FIXED ** 
- **File:** [src/fetcher/search_fallback.py](src/fetcher/search_fallback.py); shared client in [src/fetcher/common.py:157](src/fetcher/common.py)
- **Problem:** Only the Brave-returned `candidate_url` is validated against `config/search_fallback_allowlist.yaml`. The `managed_async_client` has `follow_redirects=True`, so if an allowlisted publisher 301s to an arbitrary host (intentional or compromised), the final response is accepted and scraped. This is both an SSRF-ish exfiltration vector and a trust bypass.
- **Fix:** Disable auto-redirects for fallback fetches, capture the `Location` header, re-validate the target host against `resolve_allowed_publisher`, and cap redirect chain length (≤2). Alternatively, inspect `response.history` after the fetch and drop the result if any hop left the allowlist.

### C3. SQLite connection leak across every pipeline stage -- ** FIXED ** 
- **File:** [src/storage/db.py](src/storage/db.py) — all public helpers: `save_articles`, `get_recent_urls`, `load_articles`, `log_run`, `log_delivery`, `_connect_database`
- **Problem:** `sqlite3.Connection` used as a context manager commits/rolls back but does **not** close the connection. Every helper opens a new one. Long-running CI and future long-lived processes will leak file descriptors and WAL handles; pytest sessions can flake on Windows when the DB file is unlinked.
- **Fix:** Use `contextlib.closing` around `_connect_database(...)` or return a context manager that both commits and closes.
  ```python
  @contextmanager
  def _connect_database(path: str):
      conn = sqlite3.connect(path)
      try:
          yield conn
          conn.commit()
      except Exception:
          conn.rollback()
          raise
      finally:
          conn.close()
  ```

---

## HIGH

### H1. No delivery idempotency — retries can double-send
- **File:** [src/sender/email_sender.py](src/sender/email_sender.py); tests in [tests/test_sender.py:37-182](tests/test_sender.py)
- **Problem:** `send_digest` logs each attempt to `delivery_log` but never consults prior rows for the same `run_id` or for a (recipient-group, issue_number, date) key. A GitHub Actions retry or a `PIPELINE_TIMEOUT` re-trigger can deliver the same digest twice.
- **Fix:** Compute an idempotency key such as `sha256(run_id | issue_number | group | sorted_bcc_emails)` or simply `issue_number + group`; before calling AgentMail, check `delivery_log` for a prior `status='sent'` row. If found, short-circuit with `return True` and log `idempotent_skip`. Also consider an idempotency header to AgentMail if supported.

### H2. Prompt-injection surface via scraped article content
- **Files:**
  - [src/analyzer/relevance.py](src/analyzer/relevance.py) — batch scoring prompt builder; repair prompt includes `invalid_response` verbatim
  - [src/analyzer/digest.py](src/analyzer/digest.py) — digest composition prompt; article titles/summaries inserted into prompt body
  - [src/fetcher/search_fallback.py](src/fetcher/search_fallback.py) — pulls `og:title`, `og:description`, `meta[name=author]` straight into the article model
- **Problem:** Untrusted third-party HTML lands unescaped in LLM prompts. A malicious publisher (or a compromised feed) can inject instructions that influence relevance scores, rewrite the top story, or alter the subject/title.
- **Fix:**
  1. Wrap all externally-sourced fields in clearly delimited blocks (e.g., `<article id="1"> … </article>`) and instruct the model to treat content inside those tags as data only.
  2. Strip control characters and truncate pathological lengths before insertion.
  3. For the relevance repair path, do **not** echo the raw `invalid_response` back into a new prompt — either parse it with `json_repair`-style salvage locally, or include a sanitized excerpt only.
  4. Validate the composed digest: every headline/URL must match a fetched article.

### H3. Duplicate rate limiter and robots policy per concurrent fetch track
- **File:** [src/main.py:323-345](src/main.py); `fetch_all_sources_report` in `src/fetcher/__init__.py`
- **Problem:** `_run_pipeline_async` runs two fetch tracks concurrently via `asyncio.gather`, each creating independent `DomainRateLimiter` and `RobotsPolicy` instances. A source appearing in both tracks (or two tracks pointing to the same publisher domain) can exceed `rate_limit_seconds` and double-fetch robots.txt.
- **Fix:** Construct a single `DomainRateLimiter`, `RobotsPolicy`, and `managed_async_client` at `_run_pipeline_async` entry and thread them into each track via the override parameters.

### H4. URL dedup does not lowercase paths
- **File:** [src/fetcher/dedup.py](src/fetcher/dedup.py)
- **Problem:** `normalize_url` casefolds the netloc and strips tracking params but leaves the path as-is. CDN-rewritten or upper-cased paths (`/Article/…` vs `/article/…`) create duplicate rows and duplicate digest entries across the 7-day dedup window.
- **Fix:** Lowercase the path (most news publishers serve case-insensitively). Also collapse trailing slashes, strip default `index.html`, and drop `#fragment`.

### H5. Pipeline timeout wraps work but not cleanup
- **File:** [src/main.py](src/main.py) — `run_pipeline` → `asyncio.wait_for(_run_pipeline_async(...), timeout=config.settings.pipeline_timeout)`
- **Problem:** When `asyncio.wait_for` fires, the inner coroutine is cancelled mid-stage. `_finalize_pipeline_run` / `_log_pipeline_stage_failure` rely on normal control flow, so a timeout can leave `pipeline_runs.status='started'` forever and may abandon the HTTP client.
- **Fix:** Catch `asyncio.TimeoutError` in the sync `run_pipeline` wrapper, mark the run `status='timeout'` in `pipeline_runs`, and ensure `managed_async_client` / LLM client are created under `async with` blocks so cancellation propagates cleanly.

### H6. `main.py` is 1300+ lines and mixes many concerns
- **File:** [src/main.py](src/main.py)
- **Problem:** One module covers pipeline orchestration, fetch-track partitioning, reuse-mode reading from storage, issue-number resolution, subject formatting, progress callback wiring, and run-log finalization. Local changes are high-risk; only three tests cover this file ([tests/test_main.py](tests/test_main.py)).
- **Fix:** Extract `_partition_sources_by_category`, `_merge_fetch_summaries`, `load_raw_articles_from_storage`, `resolve_issue_number`, and the subject builder into dedicated modules (`src/pipeline/partition.py`, `src/pipeline/reuse.py`, `src/pipeline/issue_number.py`). Keep `main.py` as pure orchestration + CLI.

### H7. Global-briefing module reaches into digest internals
- **File:** [src/analyzer/global_briefing.py](src/analyzer/global_briefing.py) — imports `_parse_digest_item_list`, `_select_articles as _select_digest_articles`, `_unwrap_json_block`, `_load_prompt` from `digest.py`
- **Problem:** Cross-module use of underscore-prefixed helpers locks both files together — a refactor of `digest.py` will silently break global briefing and vice versa.
- **Fix:** Promote the shared helpers to a new `src/analyzer/_shared.py` (or `src/analyzer/json_utils.py`) and import from there in both modules.

---

## MEDIUM

### M1. Unbounded log file growth
- **File:** [src/utils/logging.py](src/utils/logging.py) uses `logging.FileHandler` at INFO+
- **Fix:** Swap in `logging.handlers.RotatingFileHandler(maxBytes=5_000_000, backupCount=5)` or `TimedRotatingFileHandler`.

### M2. LLM auth header is static for process lifetime
- **File:** [src/analyzer/llm_client.py](src/analyzer/llm_client.py)
- **Problem:** The `Authorization: Bearer` header is attached to the `metadata_client` at construction. If `OPENROUTER_API_KEY` rotates during a long-running process, metadata calls will 401 while the SDK's own client still works (or vice versa).
- **Fix:** Read the key lazily via a callable, or attach the header per-request.

### M3. Repair prompt echoes the invalid response verbatim
- **File:** [src/analyzer/relevance.py](src/analyzer/relevance.py)
- **Problem:** Beyond the injection risk (H2), echoing a malformed response into a new prompt wastes tokens and often re-produces the same failure mode.
- **Fix:** Attempt local JSON salvage first (`_unwrap_json_block` already exists in `digest.py` — share it, per H7); only re-prompt if salvage fails, and truncate the echoed excerpt to ~400 characters.

### M4. No idempotency test for sender
- **File:** [tests/test_sender.py](tests/test_sender.py)
- **Problem:** Tests cover retry + BCC + group switching, but none assert "second send for the same run_id/issue is a no-op".
- **Fix:** Add once H1 is implemented.

### M5. `test_main.py` mocks `run_pipeline` wholesale
- **File:** [tests/test_main.py:24-96](tests/test_main.py)
- **Problem:** Progress-callback tests short-circuit the actual pipeline, so timeout handling (H5), track partitioning, and issue-number resolution in `main.py` go untested.
- **Fix:** After H6 refactor, add focused tests per extracted helper.

### M6. Brittle exact-kwargs assertions
- **File:** [tests/test_sender.py:62-74](tests/test_sender.py)
- **Problem:** Exact-equality assertions on dict payloads break the moment AgentMail adds an optional field.
- **Fix:** Assert critical keys (`bcc`, `subject`, `html`) individually.

### M7. `conftest.py` has no shared fixtures
- **File:** [tests/conftest.py](tests/conftest.py)
- **Problem:** Every test module reconstructs `AppConfig`, `Settings`, and `EnvConfig` by hand (see [tests/test_sender.py:185-225](tests/test_sender.py) and [tests/test_main.py:99-139](tests/test_main.py)). Drift between these factories is already visible.
- **Fix:** Add a `make_config(tmp_path, **overrides)` factory fixture in `conftest.py`.

### M8. Scraper JS-page detection heuristic is untested
- **File:** [src/fetcher/scraper.py](src/fetcher/scraper.py) — `_looks_like_javascript_rendered_page`
- **Problem:** Heuristic text-length checks will misclassify legitimate short pages.
- **Fix:** Add fixture tests for typical short pages, JS-rendered pages, and paywalls.

---

## LOW

- **L1.** Duplicated `_pluralize` helpers across modules — centralize in `src/utils/text.py`.
- **L2.** Heart emoji in [src/renderer/plaintext.py:138](src/renderer/plaintext.py) — confirm intent.
- **L3.** `FakeAgentMailClient` test doubles duplicated in [tests/test_sender.py:10-34](tests/test_sender.py) — move to shared `tests/fakes.py`.
- **L4.** `managed_async_client` with `follow_redirects=True` is a global default in [src/fetcher/common.py:157](src/fetcher/common.py) — make it per-call explicit.
- **L5.** `PipelineResult` in [src/main.py](src/main.py) mixes operator and observability concerns — split into `PipelineSummary` and `PipelineMetrics` after H6.
- **L6.** `AGENTS.md` / `CLAUDE.md` reference "16 configured sources" but the count is not asserted anywhere — add a sanity check in [scripts/seed_sources.py](scripts/seed_sources.py).

---

## Executive Summary

DPNS is a well-structured four-stage pipeline with thoughtful retries, OpenRouter routing, and clean templating. The code is readable and tests exist for every module, which is rare for a one-weekly-email tool.

However, three issues need attention before the next production run: the robots.txt policy fails open on 5xx responses (contradicting the documented deny-by-default), the search-fallback allowlist is silently bypassed by HTTP redirects, and SQLite connections leak because the `sqlite3` context manager only commits. None of these have caused visible harm yet because the pipeline runs briefly in GitHub Actions, but all three are latent production risks.

**Top 3 priorities:**
1. Tighten `RobotsPolicy` deny-by-default and disable redirects on fallback fetches (C1, C2).
2. Add delivery idempotency keyed on `issue_number + group` to prevent retry double-sends (H1).
3. Split `main.py` (1.3k lines) into focused orchestration/partition/reuse modules to unlock meaningful tests (H6).
