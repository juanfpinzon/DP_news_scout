# Plan: Add "Global Macro Briefing" Section to DPNS Digest

## Context

Given current world geopolitics (trade wars, tariffs, supply chain disruptions), senior procurement leaders need a concise daily snapshot of macro events that could impact PepsiCo's supply chains, commodity costs, and sourcing strategy. This adds a small, dedicated section to the email digest — separate from the procurement-tech content — that surfaces 2-3 high-impact global news items from the world's most reputable outlets, each with a "why it matters" framed for procurement professionals.

The key architectural constraint: global news would score 3-5 on the current procurement-tech rubric and get filtered out. We need a **parallel scoring track** with a different prompt.

---

## 1. Add `"global_news"` Source Category

**File:** `src/utils/source_validation.py:6-14`

Add `"global_news"` to `ALLOWED_SOURCE_CATEGORIES`.

---

## 2. Add Global News Sources to `config/sources.yaml`

Add 7 sources with `category: "global_news"`, `tier: 2`, `method: "rss"`:

| Source | RSS Feed URL | Notes |
|--------|-------------|-------|
| Reuters | `https://www.rss.reuters.com/news/world` | May need fallback; Reuters has restructured feeds |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | |
| DW | `https://rss.dw.com/rdf/rss-en-all` | |
| BBC World | `http://feeds.bbci.co.uk/news/world/rss.xml` | |
| CNN World | `http://rss.cnn.com/rss/edition_world.rss` | |
| Bloomberg | `active: false` + `fallback_search` | No public RSS; Brave search fallback |
| Financial Times | `active: false` + `fallback_search` | Paywalled RSS; Brave search fallback |

Each source gets a `fallback_search.query` tuned for procurement-relevant macro news, e.g.:
```yaml
fallback_search:
  enabled: true
  query: '"Bloomberg" commodity OR tariff OR trade policy OR supply chain disruption'
```

Bloomberg and FT also get `fallback_search.include_when_inactive: true`.

---

## 3. Add Settings for Global News Track

**File:** `config/settings.yaml` — add:
```yaml
global_news_relevance_threshold: 5
global_news_max_items: 3
global_news_max_per_source: 2
```

**File:** `src/utils/config.py`
- Add 3 fields to `Settings` dataclass (with defaults):
  ```python
  global_news_relevance_threshold: int = 5
  global_news_max_items: int = 3
  global_news_max_per_source: int = 2
  ```
- Wire through `load_config()` with `_get_int()` calls
- Add validation in `_validate_settings()`

---

## 4. Create Global News Scoring Prompt

**New file:** `prompts/global_news_scoring.md`

Same format as `relevance_scoring.md` but with procurement-macro relevance gates:
1. **Commodity markets** — events affecting agricultural commodities (corn, sugar, oats, palm oil, cocoa), energy prices, raw material supply
2. **Trade policy & tariffs** — new tariffs, sanctions, trade agreements, export restrictions affecting CPG supply chains
3. **Geopolitical disruption** — conflicts, instability affecting logistics routes, manufacturing regions, supplier countries
4. **Macroeconomic shifts** — currency movements, inflation, central bank decisions with procurement cost implications
5. **Regulatory & compliance** — international environmental regulations, labor laws, food safety, ESG mandates
6. **Supply chain logistics** — shipping disruptions, port closures, canal blockages, natural disasters
7. **Sovereign risk** — nationalizations, sanctions, political events threatening supplier relationships

Rubric: 8-10 = direct, immediate procurement/supply impact for CPG; 6-7 = significant macro trends procurement leaders should monitor; 4-5 = general international news with loose relevance; 1-3 = no procurement angle.

Same JSON output shape as existing scoring prompt: `{"scores": [{url, score, reasoning}]}`.

---

## 5. Parameterize `score_articles()` to Accept Alternate Prompts

**File:** `src/analyzer/relevance.py`

- Add `scoring_prompt_name: str = "relevance_scoring.md"` parameter to `score_articles()` (line 44)
- Pass it to `_build_system_prompt()` (line 74)
- Update `_build_system_prompt()` (line 175) to accept and use the parameter:
  ```python
  def _build_system_prompt(scoring_prompt_name: str = "relevance_scoring.md") -> str:
      context = _load_prompt("context_preamble.md")
      scoring = _load_prompt(scoring_prompt_name)
      return f"{context}\n\n{scoring}".strip()
  ```

This is backward-compatible — existing callers continue to work unchanged.

---

## 6. Add `global_briefing` Field to `Digest` Dataclass

**File:** `src/analyzer/digest.py:47-53`

```python
@dataclass(slots=True)
class Digest:
    top_story: DigestItem
    key_developments: list[DigestItem]
    on_our_radar: list[DigestItem]
    quick_hits: list[QuickHit]
    global_briefing: list[DigestItem] = field(default_factory=list)
```

Import `field` from `dataclasses` (already imported via `dataclass`; just add `field`).

Default `= field(default_factory=list)` ensures full backward compat — existing code that constructs `Digest` without this field continues to work.

---

## 7. Create Global Briefing Composition

**New file:** `prompts/global_briefing_composition.md`

Instructions for composing 2-3 global macro items in `DigestItem` format (headline + summary + why_it_matters + source + date). Editorial voice: "Trusted geopolitical analyst briefing procurement leaders on supply chain and cost implications." JSON output shape: `{"global_briefing": [{...}]}`.

Key difference from `digest_composition.md`: no top_story/key_developments/on_our_radar/quick_hits sections — just a flat list. Instruct LLM to return fewer items (or empty list) if articles lack meaningful procurement implications.

**New file:** `src/analyzer/global_briefing.py`

`compose_global_briefing()` function that:
1. Takes `list[ScoredArticle]` + settings
2. Selects top N articles (respecting `global_news_max_items` and `global_news_max_per_source`)
3. Calls LLM (using `llm_digest_model`) with `context_preamble.md` + `global_briefing_composition.md`
4. Parses JSON response into `list[DigestItem]`
5. Validates URLs against input set (reuse URL canonicalization from `digest.py`)
6. Returns `list[DigestItem]` (may be empty)

Reuse: `_load_prompt()`, `_canonicalize_url()`, `article_priority_key()` from existing modules.

**File:** `src/analyzer/__init__.py` — export `compose_global_briefing`.

---

## 8. Update Pipeline Orchestration

**File:** `src/main.py`

### Source partitioning (after line ~86):
```python
all_sources = load_source_registry(...)
procurement_sources = [s for s in all_sources if s.category != "global_news"]
global_news_sources = [s for s in all_sources if s.category == "global_news"]
```

### Parallel fetch (replace single `fetch_all_sources_report` call):
```python
procurement_fetch, global_fetch = await asyncio.gather(
    fetch_all_sources_report(sources=procurement_sources, ...),
    fetch_all_sources_report(sources=global_news_sources, ...),
)
```
Aggregate `FetchSummary` stats for logging.

### Parallel scoring:
```python
procurement_scored, global_scored = await asyncio.gather(
    score_articles(procurement_articles, ...),
    score_articles(
        global_articles,
        scoring_prompt_name="global_news_scoring.md",
        threshold=settings.global_news_relevance_threshold,
        ...
    ),
)
```

### Parallel composition:
```python
digest, global_briefing = await asyncio.gather(
    compose_digest(procurement_scored, ...),
    compose_global_briefing(global_scored, settings=settings, ...),
)
digest.global_briefing = global_briefing
```

### Graceful degradation:
Wrap each global news stage in try/except. If global news fails at any stage, log a warning and set `digest.global_briefing = []`. Global news failure must never block the primary digest.

### Update helpers:
- `_count_digest_articles()` (line 678): add `+ len(digest.global_briefing)`
- `_collect_included_urls()` (line 723): add `*(item.url for item in digest.global_briefing)`

---

## 9. Update Renderers

### HTML Template

**File:** `templates/digest_email.html`

Insert new section between "On Our Radar" `{% endif %}` (line 301) and "Quick Hits" `{% if quick_hits %}` (line 303).

Visual design — match "On Our Radar" card style with a distinct accent:
- Section header: `🌍 Global Macro Briefing` (10px, uppercase, dark navy)
- Same 2px dark navy underline bar
- Per-item: 3px left accent bar (amber/warm tone `#d4a849` to distinguish from teal procurement sections)
- Content: headline + summary + why_it_matters + source/date + Read link
- Wrapped in `{% if global_briefing|length %}`

### Plaintext Renderer

**File:** `src/renderer/plaintext.py`

Insert "GLOBAL MACRO BRIEFING" section between "On Our Radar" (line 88) and "Quick Hits" (line 90). Follow same pattern as On Our Radar items, including `why_it_matters` text.

### Source Counter

**File:** `src/renderer/common.py`

Add to `count_unique_sources()`:
```python
for item in digest.global_briefing:
    if item.source.strip():
        normalized_sources.add(item.source.strip().casefold())
```

---

## 10. Update Search Fallback Allowlist

**File:** `config/search_fallback_allowlist.yaml`

Add global news publishers to the `publishers` list with `group: "global_news"`:
- `reuters.com`, `aljazeera.com`, `dw.com`, `bbc.com` / `bbc.co.uk`, `cnn.com`, `bloomberg.com`, `ft.com`

Add `"global_news"` to `auto_include_source_categories`.

---

## Files Summary

### New files (4):
| File | Purpose |
|------|---------|
| `prompts/global_news_scoring.md` | Scoring rubric for geopolitical/macro articles |
| `prompts/global_briefing_composition.md` | Composition instructions for global briefing section |
| `src/analyzer/global_briefing.py` | `compose_global_briefing()` function |
| `tests/test_global_briefing.py` | Unit tests for new module |

### Modified files (12):
| File | Changes |
|------|---------|
| `src/utils/source_validation.py` | Add `"global_news"` to allowed categories |
| `config/sources.yaml` | Add 7 global news source entries |
| `config/settings.yaml` | Add 3 global news settings |
| `config/search_fallback_allowlist.yaml` | Add global news publishers |
| `src/utils/config.py` | Add 3 fields to `Settings`, wire in `load_config()` |
| `src/analyzer/relevance.py` | Parameterize `score_articles()` with `scoring_prompt_name` |
| `src/analyzer/digest.py` | Add `global_briefing` field to `Digest` |
| `src/main.py` | Dual-track orchestration, parallel fetch/score/compose |
| `templates/digest_email.html` | New global briefing HTML section |
| `src/renderer/plaintext.py` | New global briefing plaintext section |
| `src/renderer/common.py` | Include global briefing in source count |
| `src/analyzer/__init__.py` | Export `compose_global_briefing` |

---

## Implementation Order

1. `source_validation.py` — add category (unblocks everything)
2. `config.py` + `settings.yaml` — add settings
3. `prompts/global_news_scoring.md` — create scoring prompt
4. `relevance.py` — parameterize `score_articles()`
5. `digest.py` — add `global_briefing` field
6. `prompts/global_briefing_composition.md` — create composition prompt
7. `src/analyzer/global_briefing.py` — composition function
8. `common.py` + `plaintext.py` + `digest_email.html` — renderers
9. `main.py` — dual-track pipeline orchestration
10. `sources.yaml` + `search_fallback_allowlist.yaml` — add sources
11. Tests

Each step is independently testable. Steps 1-5 are safe backward-compatible changes.

---

## Verification

1. **Source validation**: `python scripts/seed_sources.py` — confirms all new RSS feeds are reachable
2. **Dry run**: `python scripts/run_manual.py --dry-run` — full pipeline with global news track
3. **Preview**: `python scripts/run_manual.py --preview` — visual check of new section in rendered email
4. **Unit tests**: `pytest tests/test_global_briefing.py -v`
5. **Full test suite**: `pytest tests/ -v` — confirm no regressions
6. **Cost check**: Monitor OpenRouter usage — global news scoring adds ~1 extra Haiku batch call, composition adds ~1 Sonnet call with 3-4 articles (minimal cost impact)
