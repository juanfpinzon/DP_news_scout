from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
DEFAULT_PROMPT_TEXT_MAX_LENGTH = 400


class SelectableArticle(Protocol):
    url: str
    source: str
    published_at: str | None
    relevance_score: int


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


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


def _sanitize_prompt_text(
    value: str | None,
    *,
    max_length: int | None = DEFAULT_PROMPT_TEXT_MAX_LENGTH,
) -> str | None:
    if value is None:
        return None

    normalized = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    normalized = " ".join(normalized.split())
    if not normalized:
        return None

    if max_length is not None and len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip()

    return normalized or None


def _sanitize_prompt_excerpt(
    value: str | None,
    *,
    max_length: int = DEFAULT_PROMPT_TEXT_MAX_LENGTH,
) -> str:
    sanitized = _sanitize_prompt_text(value, max_length=max_length)
    return sanitized or ""


def _render_article_blocks(articles: list[dict[str, Any]]) -> str:
    rendered_blocks: list[str] = ["<articles>"]
    for index, article in enumerate(articles, start=1):
        rendered_blocks.append(f'<article id="{index}">')
        rendered_blocks.append(json.dumps(article, ensure_ascii=True, indent=2))
        rendered_blocks.append("</article>")
    rendered_blocks.append("</articles>")
    return "\n".join(rendered_blocks)


def _select_articles(
    articles: list[SelectableArticle],
    *,
    limit: int,
    max_per_source: int,
    now: datetime,
    current_week_days: int,
) -> list[SelectableArticle]:
    from src.analyzer.freshness import article_priority_key

    if limit <= 0:
        return []
    if max_per_source <= 0:
        raise ValueError("max_per_source must be greater than 0")

    grouped_articles: dict[str, list[SelectableArticle]] = defaultdict(list)
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

    selected: list[SelectableArticle] = []
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


__all__ = [
    "DEFAULT_PROMPT_TEXT_MAX_LENGTH",
    "PROMPTS_DIR",
    "_load_prompt",
    "_render_article_blocks",
    "_sanitize_prompt_excerpt",
    "_sanitize_prompt_text",
    "_select_articles",
    "_unwrap_json_block",
]
