from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analyzer.llm_client import LLMClient
from src.analyzer.relevance import ScoredArticle
from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger

DEFAULT_MAX_TOKENS = 2600
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


async def compose_digest(
    articles: list[ScoredArticle],
    *,
    llm_client: Any | None = None,
    settings: Settings | None = None,
    max_articles: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    logger: Any | None = None,
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
    selected_articles = _select_articles(articles, selected_limit)
    user_prompt = _build_user_prompt(selected_articles)

    owns_client = llm_client is None
    active_client = llm_client or LLMClient(
        app_config=app_config,
        settings=settings,
    )

    try:
        try:
            response_text = await active_client.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )
            digest = _parse_digest_payload(response_text, selected_articles)
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
        return digest
    finally:
        if owns_client and hasattr(active_client, "aclose"):
            await active_client.aclose()


def _select_articles(articles: list[ScoredArticle], limit: int) -> list[ScoredArticle]:
    return sorted(
        articles,
        key=lambda article: article.relevance_score,
        reverse=True,
    )[:limit]


def _build_system_prompt() -> str:
    context = _load_prompt("context_preamble.md")
    composition = _load_prompt("digest_composition.md")
    return f"{context}\n\n{composition}".strip()


def _build_user_prompt(articles: list[ScoredArticle]) -> str:
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
        "Compose the morning digest using only the articles below.\n"
        "Do not invent articles, URLs, sources, or dates.\n"
        "Each article URL may appear at most once across all sections.\n"
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
    used_urls: set[str] = set()

    top_story = _parse_digest_item(
        payload.get("top_story"),
        field_name="top_story",
        article_urls=article_urls,
        used_urls=used_urls,
    )
    key_developments = _parse_digest_item_list(
        payload.get("key_developments"),
        field_name="key_developments",
        article_urls=article_urls,
        used_urls=used_urls,
    )
    on_our_radar = _parse_digest_item_list(
        payload.get("on_our_radar"),
        field_name="on_our_radar",
        article_urls=article_urls,
        used_urls=used_urls,
    )
    quick_hits = _parse_quick_hits(
        payload.get("quick_hits"),
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
    article_urls: set[str],
    used_urls: set[str],
) -> list[DigestItem]:
    if not isinstance(value, list):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a list")

    return [
        _parse_digest_item(
            item,
            field_name=f"{field_name}[{index}]",
            article_urls=article_urls,
            used_urls=used_urls,
        )
        for index, item in enumerate(value)
    ]


def _parse_digest_item(
    value: Any,
    *,
    field_name: str,
    article_urls: set[str],
    used_urls: set[str],
) -> DigestItem:
    if not isinstance(value, dict):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be an object")

    url = _require_string(value.get("url"), f"{field_name}.url")
    _validate_digest_url(url, field_name=field_name, article_urls=article_urls, used_urls=used_urls)

    return DigestItem(
        url=url,
        headline=_require_string(value.get("headline"), f"{field_name}.headline"),
        summary=_require_string(value.get("summary"), f"{field_name}.summary"),
        why_it_matters=_require_string(
            value.get("why_it_matters"),
            f"{field_name}.why_it_matters",
        ),
        source=_require_string(value.get("source"), f"{field_name}.source"),
        date=_require_string(value.get("date"), f"{field_name}.date", allow_empty=True),
    )


def _parse_quick_hits(
    value: Any,
    *,
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

        url = _require_string(item.get("url"), f"{field_name}.url")
        _validate_digest_url(url, field_name=field_name, article_urls=article_urls, used_urls=used_urls)
        quick_hits.append(
            QuickHit(
                url=url,
                one_liner=_require_string(item.get("one_liner"), f"{field_name}.one_liner"),
                source=_require_string(item.get("source"), f"{field_name}.source"),
            )
        )
    return quick_hits


def _validate_digest_url(
    url: str,
    *,
    field_name: str,
    article_urls: set[str],
    used_urls: set[str],
) -> None:
    if url not in article_urls:
        raise DigestCompositionError(
            f"Digest payload field '{field_name}' references unknown article URL: {url}"
        )
    if url in used_urls:
        raise DigestCompositionError(f"Digest payload reuses article URL across sections: {url}")
    used_urls.add(url)


def _require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")

    normalized = value.strip()
    if not allow_empty and not normalized:
        raise DigestCompositionError(f"Digest payload field '{field_name}' must not be empty")
    return normalized


def _unwrap_json_block(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()
    return stripped


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
