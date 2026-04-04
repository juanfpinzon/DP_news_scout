from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.analyzer.llm_client import LLMClient
from src.fetcher.models import RawArticle
from src.storage.db import ArticleRecord
from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger

DEFAULT_BATCH_SIZE = 10
DEFAULT_MAX_TOKENS = 1600
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class RelevanceScoringError(ValueError):
    """Raised when the LLM returns an invalid relevance-scoring payload."""


@dataclass(slots=True)
class ScoreResult:
    url: str
    score: int
    reasoning: str


@dataclass(slots=True)
class ScoredArticle(RawArticle):
    relevance_score: int = 0
    reasoning: str = ""

    def to_record(self) -> ArticleRecord:
        record = RawArticle.to_record(self)
        record.relevance_score = float(self.relevance_score)
        return record


async def score_articles(
    articles: list[RawArticle],
    *,
    llm_client: Any | None = None,
    settings: Settings | None = None,
    threshold: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    logger: Any | None = None,
) -> list[ScoredArticle]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than 0")
    if not articles:
        return []

    app_config: AppConfig | None = None
    if settings is None or llm_client is None:
        app_config = load_config()
        settings = settings or app_config.settings

    assert settings is not None
    threshold = threshold if threshold is not None else settings.relevance_threshold
    if not 1 <= threshold <= 10:
        raise ValueError("threshold must be between 1 and 10")

    logger = logger or get_logger(__name__, pipeline_stage="analyzer")
    system_prompt = _build_system_prompt()

    owns_client = llm_client is None
    active_client = llm_client or LLMClient(
        app_config=app_config,
        settings=settings,
    )

    try:
        retained_articles: list[ScoredArticle] = []
        total_batches = (len(articles) + batch_size - 1) // batch_size

        for batch_index, batch in enumerate(_chunked(articles, batch_size), start=1):
            user_prompt = _build_user_prompt(batch)
            response_text = await active_client.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )
            score_map = _parse_scores_payload(response_text, batch)
            retained_in_batch = 0

            for article in batch:
                score = score_map[article.url]
                if score.score < threshold:
                    continue

                retained_articles.append(
                    ScoredArticle(
                        url=article.url,
                        title=article.title,
                        source=article.source,
                        source_url=article.source_url,
                        category=article.category,
                        published_at=article.published_at,
                        fetched_at=article.fetched_at,
                        summary=article.summary,
                        author=article.author,
                        relevance_score=score.score,
                        reasoning=score.reasoning,
                    )
                )
                retained_in_batch += 1

            logger.info(
                "relevance_batch_scored",
                batch_index=batch_index,
                total_batches=total_batches,
                batch_articles=len(batch),
                retained_articles=retained_in_batch,
                threshold=threshold,
            )

        logger.info(
            "relevance_scoring_complete",
            input_articles=len(articles),
            retained_articles=len(retained_articles),
            threshold=threshold,
            batch_size=batch_size,
            total_batches=total_batches,
        )
        return retained_articles
    finally:
        if owns_client and hasattr(active_client, "aclose"):
            await active_client.aclose()


def _build_system_prompt() -> str:
    context = _load_prompt("context_preamble.md")
    scoring = _load_prompt("relevance_scoring.md")
    return f"{context}\n\n{scoring}".strip()


def _build_user_prompt(articles: list[RawArticle]) -> str:
    if len({article.url for article in articles}) != len(articles):
        raise RelevanceScoringError("Each article in a scoring batch must have a unique URL")

    payload = {
        "articles": [
            {
                "url": article.url,
                "title": article.title,
                "source": article.source,
                "category": article.category,
                "published_at": article.published_at,
                "summary": article.summary,
                "author": article.author,
            }
            for article in articles
        ]
    }

    return (
        "Score every article below and return JSON only.\n"
        "Do not omit any article. Preserve each URL exactly as provided.\n\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2)}"
    )


def _parse_scores_payload(response_text: str, batch: list[RawArticle]) -> dict[str, ScoreResult]:
    normalized_text = _unwrap_json_block(response_text)

    try:
        payload = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        raise RelevanceScoringError("LLM returned invalid JSON for relevance scoring") from exc

    if not isinstance(payload, dict):
        raise RelevanceScoringError("Relevance scoring payload must be a JSON object")

    scores = payload.get("scores")
    if not isinstance(scores, list):
        raise RelevanceScoringError("Relevance scoring payload must include a 'scores' list")

    expected_urls = [article.url for article in batch]
    expected_url_set = set(expected_urls)
    parsed_scores: dict[str, ScoreResult] = {}

    for item in scores:
        if not isinstance(item, dict):
            raise RelevanceScoringError("Each score item must be a JSON object")

        url = item.get("url")
        score = item.get("score")
        reasoning = item.get("reasoning")

        if not isinstance(url, str) or not url.strip():
            raise RelevanceScoringError("Each score item must include a non-empty 'url'")
        if url not in expected_url_set:
            raise RelevanceScoringError(f"Unexpected article URL in score payload: {url}")
        if url in parsed_scores:
            raise RelevanceScoringError(f"Duplicate score returned for article URL: {url}")

        normalized_score = _normalize_score(score, url)
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise RelevanceScoringError(
                f"Missing reasoning for relevance score payload item: {url}"
            )

        parsed_scores[url] = ScoreResult(
            url=url,
            score=normalized_score,
            reasoning=reasoning.strip(),
        )

    missing_urls = [url for url in expected_urls if url not in parsed_scores]
    if missing_urls:
        raise RelevanceScoringError(
            "Missing relevance scores for article URLs: "
            + ", ".join(missing_urls)
        )

    return parsed_scores


def _normalize_score(value: Any, url: str) -> int:
    if isinstance(value, bool):
        raise RelevanceScoringError(f"Invalid score for article URL {url}: {value!r}")

    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise RelevanceScoringError(f"Invalid score for article URL {url}: {value!r}") from exc

    if not 1 <= score <= 10:
        raise RelevanceScoringError(
            f"Score must be between 1 and 10 for article URL {url}: {score!r}"
        )
    return score


def _unwrap_json_block(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return stripped


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


def _chunked(items: list[RawArticle], size: int) -> Iterable[list[RawArticle]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


__all__ = [
    "RelevanceScoringError",
    "ScoreResult",
    "ScoredArticle",
    "score_articles",
]
