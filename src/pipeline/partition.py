from __future__ import annotations

from typing import Any

from src.fetcher import FetchSummary
from src.fetcher.dedup import normalize_url
from src.fetcher.models import RawArticle

GLOBAL_NEWS_CATEGORY = "global_news"


def partition_sources_by_category(sources: list[Any]) -> tuple[list[Any], list[Any]]:
    procurement_sources: list[Any] = []
    global_news_sources: list[Any] = []
    for source in sources:
        category = str(getattr(source, "category", "")).strip().casefold()
        if category == GLOBAL_NEWS_CATEGORY:
            global_news_sources.append(source)
        else:
            procurement_sources.append(source)
    return procurement_sources, global_news_sources


def partition_articles_by_category(
    articles: list[RawArticle],
) -> tuple[list[RawArticle], list[RawArticle]]:
    procurement_articles: list[RawArticle] = []
    global_news_articles: list[RawArticle] = []
    for article in articles:
        category = str(article.category).strip().casefold()
        if category == GLOBAL_NEWS_CATEGORY:
            global_news_articles.append(article)
        else:
            procurement_articles.append(article)
    return procurement_articles, global_news_articles


def empty_fetch_summary(*, sources_attempted: int = 0, sources_failed: int = 0) -> FetchSummary:
    return FetchSummary(
        articles=[],
        sources_attempted=sources_attempted,
        sources_succeeded=0,
        sources_failed=sources_failed,
        articles_found=0,
        articles_deduplicated=0,
        articles_saved=0,
    )


def merge_fetch_summaries(*summaries: FetchSummary) -> FetchSummary:
    merged_articles: list[RawArticle] = []
    article_index_by_url: dict[str, int] = {}

    for summary in summaries:
        for article in summary.articles:
            normalized = normalize_url(article.url)
            existing_index = article_index_by_url.get(normalized)
            if existing_index is None:
                article_index_by_url[normalized] = len(merged_articles)
                merged_articles.append(article)
                continue

            existing_article = merged_articles[existing_index]
            if should_replace_merged_article(existing_article, article):
                merged_articles[existing_index] = article

    return FetchSummary(
        articles=merged_articles,
        sources_attempted=sum(summary.sources_attempted for summary in summaries),
        sources_succeeded=sum(summary.sources_succeeded for summary in summaries),
        sources_failed=sum(summary.sources_failed for summary in summaries),
        articles_found=sum(summary.articles_found for summary in summaries),
        articles_deduplicated=len(merged_articles),
        articles_saved=sum(summary.articles_saved for summary in summaries),
    )


def summary_has_total_fetch_outage(summary: FetchSummary | None) -> bool:
    return bool(summary and summary.total_fetch_outage)


def should_replace_merged_article(existing: RawArticle, candidate: RawArticle) -> bool:
    existing_is_fallback = existing.discovery_method == "search_fallback"
    candidate_is_fallback = candidate.discovery_method == "search_fallback"

    if existing_is_fallback != candidate_is_fallback:
        return existing_is_fallback and not candidate_is_fallback

    if existing.category != GLOBAL_NEWS_CATEGORY and candidate.category == GLOBAL_NEWS_CATEGORY:
        return True

    return False
