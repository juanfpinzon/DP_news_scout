from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from src.analyzer.digest import (
    COMPOSITION_EXTRA_BODY,
    COMPOSITION_RESPONSE_FORMAT,
    MAX_JSON_ATTEMPTS,
    DigestCompositionError,
    DigestItem,
    _load_prompt,
    _parse_digest_item_list,
    _unwrap_json_block,
    _select_articles as _select_digest_articles,
)
from src.analyzer.freshness import resolve_reference_time
from src.analyzer.llm_client import LLMClient
from src.analyzer.relevance import ScoredArticle
from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger
from src.utils.progress import emit_progress

DEFAULT_MAX_TOKENS = 1800


class GlobalBriefingCompositionError(DigestCompositionError):
    """Raised when the LLM returns an invalid global briefing payload."""


async def compose_global_briefing(
    articles: list[ScoredArticle],
    *,
    llm_client: Any | None = None,
    settings: Settings | None = None,
    max_articles: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    logger: Any | None = None,
    progress_callback: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> list[DigestItem]:
    if not articles:
        return []
    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than 0")

    app_config: AppConfig | None = None
    if settings is None or llm_client is None:
        app_config = load_config()
        settings = settings or app_config.settings

    assert settings is not None
    selected_limit = (
        max_articles
        if max_articles is not None
        else settings.global_news_max_items
    )
    if selected_limit <= 0:
        raise ValueError("max_articles must be greater than 0")

    logger = logger or get_logger(__name__, pipeline_stage="analyzer")
    reference_now = resolve_reference_time(now)
    selected_articles = _select_digest_articles(
        articles,
        limit=selected_limit,
        max_per_source=settings.global_news_max_per_source,
        now=reference_now,
        current_week_days=settings.recency_priority_window_days,
    )
    if not selected_articles:
        return []

    system_prompt = _build_system_prompt()
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
                    global_briefing = _parse_global_briefing_payload(
                        response_text,
                        selected_articles,
                    )
                    break
                except GlobalBriefingCompositionError as exc:
                    if attempt >= MAX_JSON_ATTEMPTS:
                        raise

                    logger.warning(
                        "global_briefing_retrying_invalid_payload",
                        attempt=attempt,
                        max_attempts=MAX_JSON_ATTEMPTS,
                        selected_articles=len(selected_articles),
                        error=str(exc),
                    )
                    emit_progress(
                        progress_callback,
                        "Global macro briefing returned invalid JSON; retrying "
                        f"({attempt + 1}/{MAX_JSON_ATTEMPTS}).",
                    )
                    prompt_to_send = _build_json_repair_prompt(
                        articles=selected_articles,
                        invalid_response=response_text,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.error(
                "global_briefing_composition_failed",
                input_articles=len(articles),
                selected_articles=len(selected_articles),
                article_urls=[article.url for article in selected_articles],
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        logger.info(
            "global_briefing_composition_complete",
            input_articles=len(articles),
            selected_articles=len(selected_articles),
            global_briefing_items=len(global_briefing),
        )
        emit_progress(
            progress_callback,
            "Global macro briefing complete: "
            f"{len(global_briefing)} {'item' if len(global_briefing) == 1 else 'items'}.",
        )
        return global_briefing
    finally:
        if owns_client and hasattr(active_client, "aclose"):
            await active_client.aclose()


def _build_system_prompt() -> str:
    context = _load_prompt("context_preamble.md")
    composition = _load_prompt("global_briefing_composition.md")
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
        "Compose a short global macro briefing using only the articles below.\n"
        "Focus on procurement, supply-chain, logistics, compliance, or cost implications.\n"
        "Return fewer items or an empty list if the macro relevance is weak.\n"
        "Preserve source names, URLs, and dates exactly as provided.\n"
        "Return strict JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2)}"
    )


def _parse_global_briefing_payload(
    response_text: str,
    articles: list[ScoredArticle],
) -> list[DigestItem]:
    normalized_text = _unwrap_json_block(response_text)

    try:
        payload = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        raise GlobalBriefingCompositionError(
            "LLM returned invalid JSON for global briefing composition"
        ) from exc

    if not isinstance(payload, dict):
        raise GlobalBriefingCompositionError(
            "Global briefing payload must be a JSON object"
        )

    article_urls = {article.url for article in articles}
    article_lookup = {article.url: article for article in articles}

    try:
        return _parse_digest_item_list(
            payload.get("global_briefing"),
            field_name="global_briefing",
            article_lookup=article_lookup,
            article_urls=article_urls,
            used_urls=set(),
        )
    except DigestCompositionError as exc:
        raise GlobalBriefingCompositionError(str(exc)) from exc


def _build_json_repair_prompt(
    *,
    articles: list[ScoredArticle],
    invalid_response: str,
    error: str,
) -> str:
    allowed_articles = [
        {
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "published_at": article.published_at or "",
        }
        for article in articles
    ]
    required_shape = {
        "global_briefing": [
            {
                "url": "https://example.com/macro-item",
                "headline": "Executive headline",
                "summary": "Two to three sentence summary.",
                "why_it_matters": "One to two sentence implication.",
                "source": "Example Source",
                "date": "2026-04-04",
            }
        ]
    }
    return (
        "Repair the malformed global briefing output below.\n"
        "Return exactly one valid JSON object and nothing else.\n"
        "Use only the allowed article URLs and source names listed below.\n"
        "Each URL may appear at most once.\n"
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


__all__ = [
    "GlobalBriefingCompositionError",
    "compose_global_briefing",
]
