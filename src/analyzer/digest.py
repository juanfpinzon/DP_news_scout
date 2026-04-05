from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.analyzer.freshness import article_priority_key, resolve_reference_time
from src.analyzer.llm_client import LLMClient
from src.analyzer.relevance import ScoredArticle
from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger
from src.utils.progress import emit_progress

DEFAULT_MAX_TOKENS = 2600
MAX_JSON_ATTEMPTS = 3
COMPOSITION_RESPONSE_FORMAT = {"type": "json_object"}
COMPOSITION_EXTRA_BODY = {"plugins": [{"id": "response-healing"}]}
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class DigestCompositionError(ValueError):
    """Raised when the LLM returns an invalid digest payload."""


@dataclass(slots=True)
class DigestItem:
    url: str
    headline: str
    summary: str
    why_it_matters: str
    source: str
    date: str


@dataclass(slots=True)
class QuickHit:
    url: str
    one_liner: str
    source: str


@dataclass(slots=True)
class Digest:
    top_story: DigestItem
    key_developments: list[DigestItem]
    on_our_radar: list[DigestItem]
    quick_hits: list[QuickHit]
    global_briefing: list[DigestItem] = field(default_factory=list)


async def compose_digest(
    articles: list[ScoredArticle],
    *,
    llm_client: Any | None = None,
    settings: Settings | None = None,
    max_articles: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    logger: Any | None = None,
    progress_callback: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> Digest:
    if not articles:
        raise ValueError("compose_digest requires at least one scored article")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than 0")

    app_config: AppConfig | None = None
    if settings is None or llm_client is None:
        app_config = load_config()
        settings = settings or app_config.settings

    assert settings is not None
    selected_limit = max_articles if max_articles is not None else settings.max_digest_items
    if selected_limit <= 0:
        raise ValueError("max_articles must be greater than 0")

    logger = logger or get_logger(__name__, pipeline_stage="analyzer")
    system_prompt = _build_system_prompt()
    reference_now = resolve_reference_time(now)
    selected_articles = _select_articles(
        articles,
        limit=selected_limit,
        max_per_source=settings.max_digest_items_per_source,
        now=reference_now,
        current_week_days=settings.recency_priority_window_days,
    )
    user_prompt = _build_user_prompt(selected_articles, now=reference_now)

    owns_client = llm_client is None
    if llm_client is None:
        active_client = LLMClient(
            app_config=app_config,
            settings=settings,
            primary_model=settings.llm_digest_model,
        )
    else:
        with_primary_model = getattr(llm_client, "with_primary_model", None)
        active_client = (
            with_primary_model(settings.llm_digest_model)
            if callable(with_primary_model)
            else llm_client
        )

    try:
        try:
            prompt_to_send = user_prompt
            for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
                response_text = await active_client.complete(
                    system_prompt=system_prompt,
                    user_prompt=prompt_to_send,
                    max_tokens=max_tokens,
                    response_format=COMPOSITION_RESPONSE_FORMAT,
                    extra_body=COMPOSITION_EXTRA_BODY,
                )
                try:
                    digest = _parse_digest_payload(response_text, selected_articles)
                    break
                except DigestCompositionError as exc:
                    if attempt >= MAX_JSON_ATTEMPTS:
                        raise

                    logger.warning(
                        "digest_composition_retrying_invalid_payload",
                        attempt=attempt,
                        max_attempts=MAX_JSON_ATTEMPTS,
                        selected_articles=len(selected_articles),
                        error=str(exc),
                    )
                    emit_progress(
                        progress_callback,
                        "Digest composition returned invalid JSON; retrying "
                        f"({attempt + 1}/{MAX_JSON_ATTEMPTS}).",
                    )
                    prompt_to_send = _build_json_repair_prompt(
                        articles=selected_articles,
                        invalid_response=response_text,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.error(
                "digest_composition_failed",
                input_articles=len(articles),
                selected_articles=len(selected_articles),
                article_urls=[article.url for article in selected_articles],
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        logger.info(
            "digest_composition_complete",
            input_articles=len(articles),
            selected_articles=len(selected_articles),
            key_developments=len(digest.key_developments),
            on_our_radar=len(digest.on_our_radar),
            quick_hits=len(digest.quick_hits),
        )
        emit_progress(
            progress_callback,
            "Digest composition complete: "
            f"{len(digest.key_developments)} key developments, "
            f"{len(digest.on_our_radar)} on our radar, "
            f"{len(digest.quick_hits)} quick hits.",
        )
        return digest
    finally:
        if owns_client and hasattr(active_client, "aclose"):
            await active_client.aclose()


def _select_articles(
    articles: list[ScoredArticle],
    *,
    limit: int,
    max_per_source: int,
    now: datetime,
    current_week_days: int,
) -> list[ScoredArticle]:
    if limit <= 0:
        return []
    if max_per_source <= 0:
        raise ValueError("max_per_source must be greater than 0")

    grouped_articles: dict[str, list[ScoredArticle]] = defaultdict(list)
    canonical_source_names: dict[str, str] = {}

    for article in articles:
        source_name = article.source.strip()
        source_key = source_name.casefold() or article.url
        grouped_articles[source_key].append(article)
        canonical_source_names.setdefault(source_key, source_name)

    for source_articles in grouped_articles.values():
        source_articles.sort(
            key=lambda article: article_priority_key(
                article,
                now=now,
                current_week_days=current_week_days,
            ),
            reverse=True,
        )

    source_order = sorted(
        grouped_articles,
        key=lambda source_key: (
            article_priority_key(
                grouped_articles[source_key][0],
                now=now,
                current_week_days=current_week_days,
            ),
            canonical_source_names[source_key],
        ),
        reverse=True,
    )

    selected: list[ScoredArticle] = []
    selected_urls: set[str] = set()
    selected_counts: dict[str, int] = defaultdict(int)
    round_index = 0

    while len(selected) < limit:
        progress = False
        for source_key in source_order:
            source_articles = grouped_articles[source_key]
            if round_index >= len(source_articles):
                continue
            if selected_counts[source_key] >= max_per_source:
                continue

            article = source_articles[round_index]
            if article.url in selected_urls:
                continue

            selected.append(article)
            selected_urls.add(article.url)
            selected_counts[source_key] += 1
            progress = True

            if len(selected) >= limit:
                return selected

        if not progress:
            break
        round_index += 1

    leftovers = sorted(
        (article for article in articles if article.url not in selected_urls),
        key=lambda article: article_priority_key(
            article,
            now=now,
            current_week_days=current_week_days,
        ),
        reverse=True,
    )
    for article in leftovers:
        selected.append(article)
        if len(selected) >= limit:
            break

    return selected


def _build_system_prompt() -> str:
    context = _load_prompt("context_preamble.md")
    composition = _load_prompt("digest_composition.md")
    return f"{context}\n\n{composition}".strip()


def _build_user_prompt(articles: list[ScoredArticle], *, now: datetime) -> str:
    payload = {
        "articles": [
            {
                "url": article.url,
                "title": article.title,
                "source": article.source,
                "published_at": article.published_at or "",
                "summary": article.summary or "",
                "author": article.author or "",
                "relevance_score": article.relevance_score,
                "relevance_reasoning": article.reasoning,
            }
            for article in articles
        ]
    }

    return (
        f"Digest reference date: {now.date().isoformat()}.\n"
        "Bias the digest toward clearly dated current-week developments. Undated or older items should not displace fresher news unless they are materially more important.\n"
        "Compose the morning digest using only the articles below.\n"
        "Do not invent articles, URLs, sources, or dates.\n"
        "Each article URL may appear at most once across all sections.\n"
        "Preserve source diversity in the final digest when the article set allows it.\n"
        "Keep all `source` and `date` fields as plain JSON strings, never objects or arrays.\n"
        "Return strict JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2)}"
    )


def _parse_digest_payload(response_text: str, articles: list[ScoredArticle]) -> Digest:
    normalized_text = _unwrap_json_block(response_text)

    try:
        payload = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        raise DigestCompositionError("LLM returned invalid JSON for digest composition") from exc

    if not isinstance(payload, dict):
        raise DigestCompositionError("Digest payload must be a JSON object")

    article_urls = {article.url for article in articles}
    article_lookup = {article.url: article for article in articles}
    used_urls: set[str] = set()

    top_story = _parse_digest_item(
        payload.get("top_story"),
        field_name="top_story",
        article_lookup=article_lookup,
        article_urls=article_urls,
        used_urls=used_urls,
    )
    key_developments = _parse_digest_item_list(
        payload.get("key_developments"),
        field_name="key_developments",
        article_lookup=article_lookup,
        article_urls=article_urls,
        used_urls=used_urls,
    )
    on_our_radar = _parse_digest_item_list(
        payload.get("on_our_radar"),
        field_name="on_our_radar",
        article_lookup=article_lookup,
        article_urls=article_urls,
        used_urls=used_urls,
    )
    quick_hits = _parse_quick_hits(
        payload.get("quick_hits"),
        article_lookup=article_lookup,
        article_urls=article_urls,
        used_urls=used_urls,
    )

    return Digest(
        top_story=top_story,
        key_developments=key_developments,
        on_our_radar=on_our_radar,
        quick_hits=quick_hits,
    )


def _parse_digest_item_list(
    value: Any,
    *,
    field_name: str,
    article_lookup: dict[str, ScoredArticle],
    article_urls: set[str],
    used_urls: set[str],
) -> list[DigestItem]:
    if not isinstance(value, list):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a list")

    return [
        _parse_digest_item(
            item,
            field_name=f"{field_name}[{index}]",
            article_lookup=article_lookup,
            article_urls=article_urls,
            used_urls=used_urls,
        )
        for index, item in enumerate(value)
    ]


def _parse_digest_item(
    value: Any,
    *,
    field_name: str,
    article_lookup: dict[str, ScoredArticle],
    article_urls: set[str],
    used_urls: set[str],
) -> DigestItem:
    if not isinstance(value, dict):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be an object")

    url = _validate_digest_url(
        _require_string(value.get("url"), f"{field_name}.url"),
        field_name=field_name,
        article_urls=article_urls,
        used_urls=used_urls,
    )
    article = article_lookup[url]

    return DigestItem(
        url=url,
        headline=_require_string(value.get("headline"), f"{field_name}.headline"),
        summary=_require_string(value.get("summary"), f"{field_name}.summary"),
        why_it_matters=_require_string(
            value.get("why_it_matters"),
            f"{field_name}.why_it_matters",
        ),
        source=_resolve_digest_source(
            value.get("source"),
            field_name=f"{field_name}.source",
            article=article,
        ),
        date=_resolve_digest_date(
            value.get("date"),
            article=article,
        ),
    )


def _parse_quick_hits(
    value: Any,
    *,
    article_lookup: dict[str, ScoredArticle],
    article_urls: set[str],
    used_urls: set[str],
) -> list[QuickHit]:
    if not isinstance(value, list):
        raise DigestCompositionError("Digest payload field 'quick_hits' must be a list")

    quick_hits: list[QuickHit] = []
    for index, item in enumerate(value):
        field_name = f"quick_hits[{index}]"
        if not isinstance(item, dict):
            raise DigestCompositionError(f"Digest payload field '{field_name}' must be an object")

        url = _validate_digest_url(
            _require_string(item.get("url"), f"{field_name}.url"),
            field_name=field_name,
            article_urls=article_urls,
            used_urls=used_urls,
        )
        quick_hits.append(
            QuickHit(
                url=url,
                one_liner=_resolve_quick_hit_one_liner(
                    item.get("one_liner"),
                    field_name=f"{field_name}.one_liner",
                    article=article_lookup[url],
                ),
                source=_resolve_digest_source(
                    item.get("source"),
                    field_name=f"{field_name}.source",
                    article=article_lookup[url],
                ),
            )
        )
    return quick_hits


def _validate_digest_url(
    url: str,
    *,
    field_name: str,
    article_urls: set[str],
    used_urls: set[str],
) -> str:
    resolved_url = _resolve_digest_url(
        url,
        field_name=field_name,
        article_urls=article_urls,
    )
    if resolved_url in used_urls:
        raise DigestCompositionError(f"Digest payload reuses article URL across sections: {resolved_url}")
    used_urls.add(resolved_url)
    return resolved_url


def _resolve_digest_url(
    url: str,
    *,
    field_name: str,
    article_urls: set[str],
) -> str:
    normalized_url = _sanitize_digest_url(url)
    if normalized_url in article_urls:
        return normalized_url

    canonical_candidates = _find_canonical_url_matches(normalized_url, article_urls)
    if len(canonical_candidates) == 1:
        return canonical_candidates[0]

    # Only repair uniquely truncated URLs. Never coerce a longer model URL
    # down to a selected article, because that can silently relink content.
    truncated_candidates = sorted(
        candidate
        for candidate in article_urls
        if len(normalized_url) < len(candidate) and candidate.startswith(normalized_url)
    )
    if len(truncated_candidates) == 1:
        return truncated_candidates[0]

    brand_qualified_candidates = _find_brand_qualified_path_variant_matches(
        normalized_url,
        article_urls,
    )
    if len(brand_qualified_candidates) == 1:
        return brand_qualified_candidates[0]

    raise DigestCompositionError(
        f"Digest payload field '{field_name}' references unknown article URL: {url}"
    )


def _sanitize_digest_url(url: str) -> str:
    return url.strip().rstrip(".,);]")


def _canonicalize_digest_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def _find_canonical_url_matches(url: str, article_urls: set[str]) -> list[str]:
    canonical_target = _canonicalize_digest_url(url)
    return sorted(
        candidate
        for candidate in article_urls
        if _canonicalize_digest_url(candidate) == canonical_target
    )


def _find_brand_qualified_path_variant_matches(
    url: str,
    article_urls: set[str],
) -> list[str]:
    target = _split_digest_url_parts(url)
    if target is None:
        return []

    target_host, target_segments = target
    brand_tokens = _extract_host_brand_tokens(target_host)
    if len(target_segments) < 3 or not brand_tokens:
        return []

    matches: list[str] = []
    for candidate in article_urls:
        candidate_parts = _split_digest_url_parts(candidate)
        if candidate_parts is None:
            continue

        candidate_host, candidate_segments = candidate_parts
        if candidate_host != target_host:
            continue
        if len(candidate_segments) != len(target_segments):
            continue

        differing_indexes = [
            index
            for index, (target_segment, candidate_segment) in enumerate(
                zip(target_segments, candidate_segments, strict=True)
            )
            if target_segment.casefold() != candidate_segment.casefold()
        ]
        if len(differing_indexes) != 1:
            continue

        differing_index = differing_indexes[0]
        if differing_index >= len(target_segments) - 2:
            continue

        if not _is_brand_qualified_segment_variant(
            target_segments[differing_index],
            candidate_segments[differing_index],
            brand_tokens=brand_tokens,
        ):
            continue

        matches.append(candidate)

    return sorted(matches)


def _split_digest_url_parts(url: str) -> tuple[str, list[str]] | None:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return None

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    return host, path_segments


def _extract_host_brand_tokens(host: str) -> set[str]:
    first_label = host.split(".", maxsplit=1)[0]
    return {
        token
        for token in re.split(r"[^a-z0-9]+", first_label.casefold())
        if token
    }


def _is_brand_qualified_segment_variant(
    left: str,
    right: str,
    *,
    brand_tokens: set[str],
) -> bool:
    left_tokens = [token for token in re.split(r"[-_]+", left.casefold()) if token]
    right_tokens = [token for token in re.split(r"[-_]+", right.casefold()) if token]
    if not left_tokens or not right_tokens:
        return False
    if abs(len(left_tokens) - len(right_tokens)) != 1:
        return False

    longer_tokens = left_tokens if len(left_tokens) > len(right_tokens) else right_tokens
    shorter_tokens = right_tokens if longer_tokens is left_tokens else left_tokens

    extra_token: str | None = None
    if shorter_tokens == longer_tokens[1:]:
        extra_token = longer_tokens[0]
    elif shorter_tokens == longer_tokens[:-1]:
        extra_token = longer_tokens[-1]

    return extra_token in brand_tokens if extra_token is not None else False


def _require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")

    normalized = value.strip()
    if not allow_empty and not normalized:
        raise DigestCompositionError(f"Digest payload field '{field_name}' must not be empty")
    return normalized


def _resolve_quick_hit_one_liner(
    value: Any,
    *,
    field_name: str,
    article: ScoredArticle,
) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized

    fallback = (article.summary or "").strip()
    if fallback:
        first_sentence = re.split(r"(?<=[.!?])\s+", fallback, maxsplit=1)[0].strip()
        if first_sentence:
            return first_sentence

    title = article.title.strip()
    if not title:
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")
    if title.endswith((".", "!", "?")):
        return title
    return f"{title}."


def _resolve_digest_source(
    value: Any,
    *,
    field_name: str,
    article: ScoredArticle,
) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized

    fallback = article.source.strip()
    if fallback:
        return fallback
    raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")


def _resolve_digest_date(
    value: Any,
    *,
    article: ScoredArticle,
) -> str:
    if isinstance(value, str):
        return value.strip()
    return (article.published_at or "").strip()


def _unwrap_json_block(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()

    embedded_fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if embedded_fenced:
        return embedded_fenced.group(1).strip()

    first_brace = stripped.find("{")
    if first_brace >= 0:
        decoder = json.JSONDecoder()
        try:
            payload, _end = decoder.raw_decode(stripped[first_brace:])
        except json.JSONDecodeError:
            return stripped
        return json.dumps(payload, ensure_ascii=False)

    return stripped


def _build_json_repair_prompt(
    *,
    articles: list[ScoredArticle],
    invalid_response: str,
    error: str,
) -> str:
    allowed_articles = {
        "articles": [
            {
                "url": article.url,
                "title": article.title,
                "source": article.source,
                "date": article.published_at or "",
            }
            for article in articles
        ]
    }
    required_shape = {
        "top_story": {
            "url": "https://example.com/top-story",
            "headline": "Executive headline",
            "summary": "Two to three sentence summary.",
            "why_it_matters": "One to two sentence implication.",
            "source": "Example Source",
            "date": "2026-04-04",
        },
        "key_developments": [
            {
                "url": "https://example.com/key-development",
                "headline": "Executive headline",
                "summary": "Two to three sentence summary.",
                "why_it_matters": "One to two sentence implication.",
                "source": "Example Source",
                "date": "2026-04-04",
            }
        ],
        "on_our_radar": [
            {
                "url": "https://example.com/radar-item",
                "headline": "Executive headline",
                "summary": "Two to three sentence summary.",
                "why_it_matters": "One to two sentence implication.",
                "source": "Example Source",
                "date": "",
            }
        ],
        "quick_hits": [
            {
                "url": "https://example.com/quick-hit",
                "one_liner": "Single-sentence takeaway.",
                "source": "Example Source",
            }
        ],
    }
    return (
        "Repair the malformed digest output below.\n"
        "Return exactly one valid JSON object and nothing else.\n"
        "Use only the allowed article URLs and source names listed below.\n"
        "Each URL may appear at most once across all sections.\n"
        "Use an empty string for missing dates.\n"
        "Every `source` and `date` field must be a plain JSON string, never an object or array.\n\n"
        "Required JSON shape:\n"
        f"{json.dumps(required_shape, ensure_ascii=True, indent=2)}\n\n"
        "Allowed articles:\n"
        f"{json.dumps(allowed_articles, ensure_ascii=True, indent=2)}\n\n"
        "Validation error to fix:\n"
        f"{error}\n\n"
        "Malformed output to repair:\n"
        f"{invalid_response}"
    )


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


__all__ = [
    "Digest",
    "DigestCompositionError",
    "DigestItem",
    "QuickHit",
    "compose_digest",
]
