from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.analyzer.llm_client import LLMClient
from src.fetcher.models import RawArticle
from src.storage.db import ArticleRecord
from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger
from src.utils.progress import emit_progress

DEFAULT_BATCH_SIZE = 10
DEFAULT_MAX_TOKENS = 1600
MAX_SCORING_ATTEMPTS = 3
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
    scoring_prompt_name: str = "relevance_scoring.md",
    threshold: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    logger: Any | None = None,
    progress_callback: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> list[ScoredArticle]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than 0")
    if not articles:
        return []

    app_config: AppConfig | None = None
    if settings is None:
        app_config = load_config()
        settings = app_config.settings

    assert settings is not None
    threshold = threshold if threshold is not None else settings.relevance_threshold
    if not 1 <= threshold <= 10:
        raise ValueError("threshold must be between 1 and 10")

    logger = logger or get_logger(__name__, pipeline_stage="analyzer")
    system_prompt = _build_system_prompt(scoring_prompt_name=scoring_prompt_name)

    owns_client = llm_client is None
    if llm_client is None:
        client_kwargs: dict[str, Any] = {
            "settings": settings,
            "primary_model": settings.llm_scoring_model,
        }
        if app_config is not None:
            client_kwargs["app_config"] = app_config
        active_client = LLMClient(**client_kwargs)
    else:
        with_primary_model = getattr(llm_client, "with_primary_model", None)
        active_client = (
            with_primary_model(settings.llm_scoring_model)
            if callable(with_primary_model)
            else llm_client
        )

    try:
        retained_articles: list[ScoredArticle] = []
        total_batches = (len(articles) + batch_size - 1) // batch_size

        for batch_index, batch in enumerate(_chunked(articles, batch_size), start=1):
            user_prompt = _build_user_prompt(batch, now=now)
            try:
                prompt_to_send = user_prompt
                for attempt in range(1, MAX_SCORING_ATTEMPTS + 1):
                    response_text = await active_client.complete(
                        system_prompt=system_prompt,
                        user_prompt=prompt_to_send,
                        max_tokens=max_tokens,
                    )
                    try:
                        score_map = _parse_scores_payload(response_text, batch)
                        break
                    except RelevanceScoringError as exc:
                        if attempt >= MAX_SCORING_ATTEMPTS:
                            raise

                        logger.warning(
                            "relevance_batch_retrying_invalid_payload",
                            batch_index=batch_index,
                            total_batches=total_batches,
                            attempt=attempt,
                            max_attempts=MAX_SCORING_ATTEMPTS,
                            batch_articles=len(batch),
                            article_urls=[article.url for article in batch],
                            error=str(exc),
                        )
                        emit_progress(
                            progress_callback,
                            "Relevance batch "
                            f"{batch_index}/{total_batches} returned invalid output; "
                            f"retrying ({attempt + 1}/{MAX_SCORING_ATTEMPTS}).",
                        )
                        prompt_to_send = _build_score_repair_prompt(
                            batch=batch,
                            invalid_response=response_text,
                            error=str(exc),
                        )
            except Exception as exc:
                logger.error(
                    "relevance_batch_failed",
                    batch_index=batch_index,
                    total_batches=total_batches,
                    batch_articles=len(batch),
                    article_urls=[article.url for article in batch],
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
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
                        origin_source=article.origin_source,
                        discovery_method=article.discovery_method,
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
            emit_progress(
                progress_callback,
                "Relevance batch "
                f"{batch_index}/{total_batches} complete: kept {retained_in_batch} "
                f"of {len(batch)} {_pluralize(len(batch), 'article')} above threshold {threshold}.",
            )

        logger.info(
            "relevance_scoring_complete",
            input_articles=len(articles),
            retained_articles=len(retained_articles),
            threshold=threshold,
            batch_size=batch_size,
            total_batches=total_batches,
        )
        emit_progress(
            progress_callback,
            "Relevance scoring complete: "
            f"{len(retained_articles)} relevant {_pluralize(len(retained_articles), 'article')} retained.",
        )
        return retained_articles
    finally:
        if owns_client and hasattr(active_client, "aclose"):
            await active_client.aclose()


def _build_system_prompt(scoring_prompt_name: str = "relevance_scoring.md") -> str:
    context = _load_prompt("context_preamble.md")
    scoring = _load_prompt(scoring_prompt_name)
    return f"{context}\n\n{scoring}".strip()


def _build_user_prompt(articles: list[RawArticle], *, now: datetime | None) -> str:
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
        f"Digest reference date: {(now.isoformat() if now is not None else 'current UTC time')}.\n"
        "Fresh, clearly dated current-week developments should outrank older or undated items when relevance is comparable.\n"
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
        resolved_url = _resolve_score_url(url, expected_url_set)
        if resolved_url in parsed_scores:
            raise RelevanceScoringError(
                f"Duplicate score returned for article URL: {resolved_url}"
            )

        normalized_score = _normalize_score(score, resolved_url)
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise RelevanceScoringError(
                f"Missing reasoning for relevance score payload item: {resolved_url}"
            )

        parsed_scores[resolved_url] = ScoreResult(
            url=resolved_url,
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


def _resolve_score_url(url: str, expected_urls: set[str]) -> str:
    normalized_url = _sanitize_score_url(url)
    if normalized_url in expected_urls:
        return normalized_url

    canonical_candidates = _find_canonical_score_url_matches(
        normalized_url,
        expected_urls,
    )
    if len(canonical_candidates) == 1:
        return canonical_candidates[0]

    truncated_candidates = _find_safe_truncated_score_url_matches(
        normalized_url,
        expected_urls,
    )
    if len(truncated_candidates) == 1:
        return truncated_candidates[0]

    raise RelevanceScoringError(f"Unexpected article URL in score payload: {url}")


def _sanitize_score_url(url: str) -> str:
    return url.strip().rstrip(".,);]")


def _canonicalize_score_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def _find_canonical_score_url_matches(url: str, expected_urls: set[str]) -> list[str]:
    canonical_target = _canonicalize_score_url(url)
    return sorted(
        candidate
        for candidate in expected_urls
        if _canonicalize_score_url(candidate) == canonical_target
    )


def _build_score_repair_prompt(
    *,
    batch: list[RawArticle],
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
        for article in batch
    ]
    required_shape = {
        "scores": [
            {
                "url": "https://example.com/article-1",
                "score": 8,
                "reasoning": "One sentence explaining the score.",
            }
        ]
    }
    return (
        "Repair the malformed relevance scoring output below.\n"
        "Return exactly one valid JSON object and nothing else.\n"
        "Use only the allowed article URLs listed below.\n"
        "Return one score item for every allowed article URL, and each URL must appear exactly once.\n"
        "Scores must be integers between 1 and 10.\n"
        "Each `reasoning` value must be a non-empty string.\n\n"
        "Validation error:\n"
        f"{error}\n\n"
        "Required JSON shape:\n"
        f"{json.dumps(required_shape, ensure_ascii=True, indent=2)}\n\n"
        "Allowed articles:\n"
        f"{json.dumps(allowed_articles, ensure_ascii=True, indent=2)}\n\n"
        "Malformed output to repair:\n"
        f"{invalid_response.strip()}\n"
    )


def _find_safe_truncated_score_url_matches(
    url: str,
    expected_urls: set[str],
) -> list[str]:
    target_identity = _split_score_url_identity(url)
    if target_identity is None:
        return []

    return sorted(
        candidate
        for candidate in expected_urls
        if _is_safe_truncated_score_url_match(
            url,
            candidate,
            target_identity=target_identity,
        )
    )


def _is_safe_truncated_score_url_match(
    url: str,
    candidate: str,
    *,
    target_identity: tuple[str, str, str],
) -> bool:
    if len(url) >= len(candidate) or not candidate.startswith(url):
        return False

    candidate_identity = _split_score_url_identity(candidate)
    if candidate_identity != target_identity:
        return False

    suffix = candidate[len(url) :]
    return url.endswith(("?", "#")) or suffix[:1] in {"?", "#"}


def _split_score_url_identity(url: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return None

    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return scheme, host, path


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


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
