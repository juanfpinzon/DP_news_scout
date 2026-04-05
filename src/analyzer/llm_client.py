from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from openai import APIConnectionError, AsyncOpenAI

from src.utils.config import AppConfig, Settings, load_config
from src.utils.logging import get_logger

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
FALLBACK_STATUS_CODES = {429, 503}
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class UsageMetrics:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None


@dataclass(slots=True)
class GenerationMetadata:
    generation_id: str
    total_cost: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    provider_name: str | None = None
    model: str | None = None


class LLMClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_key: str | None = None,
        app_config: AppConfig | None = None,
        client: AsyncOpenAI | Any | None = None,
        metadata_client: httpx.AsyncClient | Any | None = None,
        logger: Any | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if app_config is None and (settings is None or api_key is None):
            app_config = load_config()

        if app_config is not None:
            settings = settings or app_config.settings
            api_key = api_key or app_config.env.openrouter_api_key

        if settings is None:
            raise ValueError("settings must be provided to initialize LLMClient")
        if not api_key:
            raise ValueError("api_key must be provided to initialize LLMClient")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be greater than 0")
        if backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds must be 0 or greater")

        self.settings = settings
        self.api_key = api_key
        self.logger = logger or get_logger(__name__, pipeline_stage="analyzer")
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep

        self._owns_openai_client = client is None
        self.client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

        self._owns_metadata_client = metadata_client is None
        self.metadata_client = metadata_client or httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> str:
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty")
        if not user_prompt.strip():
            raise ValueError("user_prompt must not be empty")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")

        primary_model = self.settings.llm_model
        fallback_model = self.settings.llm_model_fallback
        active_model = primary_model
        fallback_used = False
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            started_at = time.monotonic()
            try:
                request_kwargs: dict[str, Any] = {
                    "model": active_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": max_tokens,
                }
                if response_format is not None:
                    request_kwargs["response_format"] = response_format
                if extra_body is not None:
                    request_kwargs["extra_body"] = extra_body

                response = await self.client.chat.completions.create(
                    **request_kwargs,
                )
                content = _extract_text(response)
                usage = _extract_usage_metrics(response)
                generation = await self._fetch_generation_metadata(
                    generation_id=getattr(response, "id", None)
                )

                estimated_cost, cost_source = _resolve_cost(usage, generation)
                prompt_tokens = _coalesce_int(
                    generation.prompt_tokens if generation else None,
                    usage.prompt_tokens,
                )
                completion_tokens = _coalesce_int(
                    generation.completion_tokens if generation else None,
                    usage.completion_tokens,
                )
                total_tokens = _coalesce_int(
                    generation.total_tokens if generation else None,
                    usage.total_tokens,
                )
                self.logger.info(
                    "llm_completion_succeeded",
                    attempt=attempt,
                    requested_model=primary_model,
                    active_model=active_model,
                    response_model=getattr(response, "model", None),
                    fallback_used=fallback_used,
                    generation_id=generation.generation_id if generation else getattr(response, "id", None),
                    provider_name=generation.provider_name if generation else None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost=estimated_cost,
                    cost_source=cost_source,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                )
                return content
            except Exception as exc:  # pragma: no cover - branches covered below
                last_error = exc
                status_code = _extract_status_code(exc)
                retryable = _is_retryable(exc)
                should_fallback = (
                    active_model == primary_model
                    and fallback_model != primary_model
                    and status_code in FALLBACK_STATUS_CODES
                )
                next_model = fallback_model if should_fallback else active_model

                if not retryable or attempt >= self.max_attempts:
                    self.logger.error(
                        "llm_completion_failed",
                        attempt=attempt,
                        requested_model=primary_model,
                        active_model=active_model,
                        next_model=next_model,
                        fallback_used=fallback_used or should_fallback,
                        status_code=status_code,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    raise

                delay_seconds = self.backoff_base_seconds * (2 ** (attempt - 1))
                self.logger.warning(
                    "llm_completion_retrying",
                    attempt=attempt,
                    requested_model=primary_model,
                    active_model=active_model,
                    next_model=next_model,
                    fallback_after_error=should_fallback,
                    status_code=status_code,
                    retry_delay_seconds=delay_seconds,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

                active_model = next_model
                fallback_used = fallback_used or should_fallback
                await self._sleep(delay_seconds)

        raise RuntimeError("LLM completion failed") from last_error

    async def aclose(self) -> None:
        if self._owns_openai_client and hasattr(self.client, "close"):
            await self.client.close()
        if self._owns_metadata_client and hasattr(self.metadata_client, "aclose"):
            await self.metadata_client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def _fetch_generation_metadata(
        self,
        generation_id: str | None,
    ) -> GenerationMetadata | None:
        if not generation_id:
            return None

        try:
            response = await self.metadata_client.get(
                "/generation",
                params={"id": generation_id},
            )
        except httpx.HTTPError as exc:
            self.logger.warning(
                "llm_generation_metadata_unavailable",
                generation_id=generation_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

        if response.status_code >= 400:
            self.logger.warning(
                "llm_generation_metadata_unavailable",
                generation_id=generation_id,
                status_code=response.status_code,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            self.logger.warning(
                "llm_generation_metadata_unavailable",
                generation_id=generation_id,
                error="invalid_json",
            )
            return None

        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return None

        return GenerationMetadata(
            generation_id=generation_id,
            total_cost=_coerce_float(data.get("total_cost") or data.get("cost")),
            prompt_tokens=_coerce_int(data.get("prompt_tokens") or data.get("input_tokens")),
            completion_tokens=_coerce_int(
                data.get("completion_tokens") or data.get("output_tokens")
            ),
            total_tokens=_coerce_int(data.get("total_tokens")),
            provider_name=_coerce_str(data.get("provider_name")),
            model=_coerce_str(data.get("model")),
        )


def _extract_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError("LLM response did not include any choices")

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)

    if isinstance(content, str):
        text = content.strip()
        if text:
            return text

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue

            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
                continue

            text = getattr(item, "text", None) or getattr(item, "content", None)
            if isinstance(text, str):
                parts.append(text)

        combined = "".join(parts).strip()
        if combined:
            return combined

    raise ValueError("LLM response did not include text content")


def _extract_usage_metrics(response: Any) -> UsageMetrics:
    usage = getattr(response, "usage", None)
    if usage is None:
        return UsageMetrics()

    if isinstance(usage, dict):
        data = usage
    else:
        data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cost": getattr(usage, "cost", None),
        }

    return UsageMetrics(
        prompt_tokens=_coerce_int(data.get("prompt_tokens")),
        completion_tokens=_coerce_int(data.get("completion_tokens")),
        total_tokens=_coerce_int(data.get("total_tokens")),
        cost=_coerce_float(data.get("cost")),
    )


def _resolve_cost(
    usage: UsageMetrics,
    generation: GenerationMetadata | None,
) -> tuple[float | None, str | None]:
    if generation and generation.total_cost is not None:
        return generation.total_cost, "generation_metadata"
    if usage.cost is not None:
        return usage.cost, "response_usage"
    return None, None


def _extract_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status

    return None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, httpx.HTTPError, TimeoutError)):
        return True

    status_code = _extract_status_code(exc)
    return status_code in RETRYABLE_STATUS_CODES


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coalesce_int(*values: Any) -> int | None:
    for value in values:
        coerced = _coerce_int(value)
        if coerced is not None:
            return coerced
    return None


__all__ = ["LLMClient", "GenerationMetadata", "UsageMetrics"]
