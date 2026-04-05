from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APITimeoutError
import pytest

from src.analyzer import LLMClient
from src.utils.config import Settings


class DummyLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **kwargs) -> None:
        self.records.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.records.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs) -> None:
        self.records.append(("error", event, kwargs))

    def find(self, level: str, event: str) -> dict[str, object]:
        for record_level, record_event, payload in self.records:
            if record_level == level and record_event == event:
                return payload
        raise AssertionError(f"Missing log record: {level} {event}")


class FakeAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeCompletionsAPI:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeOpenAIClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletionsAPI(outcomes))

    @property
    def calls(self) -> list[dict[str, object]]:
        return self.chat.completions.calls

    async def close(self) -> None:
        return None


class FakeMetadataResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class FakeMetadataClient:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(self, path: str, params: dict[str, str] | None = None):
        self.calls.append((path, params))
        if self.responses:
            response = self.responses.pop(0)
        else:
            response = FakeMetadataResponse(404, {})

        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        return None


class CapturingOpenAIClient:
    init_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs) -> None:
        CapturingOpenAIClient.init_kwargs = kwargs

    async def close(self) -> None:
        return None


def build_settings() -> Settings:
    return Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Central European Time",
        llm_scoring_model="anthropic/claude-haiku-4.5",
        llm_digest_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-haiku-4.5",
        database_path="data/test.db",
        log_level="INFO",
        log_file="data/logs/test.jsonl",
        dry_run=True,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
    )


def build_chat_response(
    *,
    content: str,
    prompt_tokens: int = 12,
    completion_tokens: int = 8,
    total_tokens: int = 20,
    cost: float | None = 0.0015,
    response_id: str = "gen-123",
    model: str = "anthropic/claude-sonnet-4-6",
):
    return SimpleNamespace(
        id=response_id,
        model=model,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
        ),
    )


def test_llm_client_logs_token_usage_and_generation_cost() -> None:
    async def run() -> tuple[str, DummyLogger, FakeOpenAIClient, FakeMetadataClient]:
        logger = DummyLogger()
        openai_client = FakeOpenAIClient([build_chat_response(content="Digest body")])
        metadata_client = FakeMetadataClient(
            [
                FakeMetadataResponse(
                    200,
                    {
                        "data": {
                            "total_cost": 0.0031,
                            "prompt_tokens": 15,
                            "completion_tokens": 9,
                            "total_tokens": 24,
                            "provider_name": "Anthropic",
                        }
                    },
                )
            ]
        )
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=metadata_client,
            logger=logger,
        )
        result = await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=300,
        )
        return result, logger, openai_client, metadata_client

    result, logger, openai_client, metadata_client = asyncio.run(run())

    assert result == "Digest body"
    assert openai_client.calls[0]["model"] == "anthropic/claude-sonnet-4-6"
    assert openai_client.calls[0]["max_tokens"] == 300
    assert openai_client.calls[0]["messages"] == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "User prompt"},
    ]
    assert metadata_client.calls == [("/generation", {"id": "gen-123"})]

    success_log = logger.find("info", "llm_completion_succeeded")
    assert success_log["requested_model"] == "anthropic/claude-sonnet-4-6"
    assert success_log["active_model"] == "anthropic/claude-sonnet-4-6"
    assert success_log["prompt_tokens"] == 15
    assert success_log["completion_tokens"] == 9
    assert success_log["total_tokens"] == 24
    assert success_log["estimated_cost"] == pytest.approx(0.0031)
    assert success_log["cost_source"] == "generation_metadata"
    assert success_log["provider_name"] == "Anthropic"
    assert success_log["fallback_used"] is False


def test_llm_client_forwards_structured_output_options() -> None:
    async def run() -> FakeOpenAIClient:
        openai_client = FakeOpenAIClient([build_chat_response(content="Digest body")])
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=DummyLogger(),
        )
        await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=300,
            response_format={"type": "json_object"},
            extra_body={"plugins": [{"id": "response-healing"}]},
        )
        return openai_client

    openai_client = asyncio.run(run())

    assert openai_client.calls[0]["model"] == "anthropic/claude-sonnet-4-6"
    assert openai_client.calls[0]["response_format"] == {"type": "json_object"}
    assert openai_client.calls[0]["extra_body"] == {"plugins": [{"id": "response-healing"}]}


def test_llm_client_uses_explicit_primary_model_override() -> None:
    async def run() -> tuple[str, DummyLogger, FakeOpenAIClient]:
        logger = DummyLogger()
        openai_client = FakeOpenAIClient([build_chat_response(content="Scoring body")])
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            primary_model="anthropic/claude-haiku-4.5",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=logger,
        )
        result = await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=150,
        )
        return result, logger, openai_client

    result, logger, openai_client = asyncio.run(run())

    assert result == "Scoring body"
    assert openai_client.calls[0]["model"] == "anthropic/claude-haiku-4.5"
    success_log = logger.find("info", "llm_completion_succeeded")
    assert success_log["requested_model"] == "anthropic/claude-haiku-4.5"
    assert success_log["active_model"] == "anthropic/claude-haiku-4.5"


def test_llm_client_with_primary_model_returns_shared_client_with_override() -> None:
    openai_client = FakeOpenAIClient([build_chat_response(content="Digest body")])
    metadata_client = FakeMetadataClient()
    client = LLMClient(
        settings=build_settings(),
        api_key="test-key",
        client=openai_client,
        metadata_client=metadata_client,
        logger=DummyLogger(),
    )

    cloned_client = client.with_primary_model("anthropic/claude-haiku-4.5")

    assert cloned_client is not client
    assert cloned_client.primary_model == "anthropic/claude-haiku-4.5"
    assert cloned_client.fallback_model == client.fallback_model
    assert cloned_client.client is client.client
    assert cloned_client.metadata_client is client.metadata_client
    assert cloned_client._owns_openai_client is False
    assert cloned_client._owns_metadata_client is False


def test_llm_client_configures_sdk_timeout_and_disables_internal_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.analyzer.llm_client.AsyncOpenAI", CapturingOpenAIClient)
    CapturingOpenAIClient.init_kwargs = None

    client = LLMClient(
        settings=build_settings(),
        api_key="test-key",
        metadata_client=FakeMetadataClient(),
        logger=DummyLogger(),
    )

    assert CapturingOpenAIClient.init_kwargs is not None
    assert CapturingOpenAIClient.init_kwargs["timeout"] == 15.0
    assert CapturingOpenAIClient.init_kwargs["max_retries"] == 0

    asyncio.run(client.aclose())


def test_llm_client_falls_back_after_primary_rate_limit() -> None:
    async def run() -> tuple[str, DummyLogger, FakeOpenAIClient, list[float]]:
        logger = DummyLogger()
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        openai_client = FakeOpenAIClient(
            [
                FakeAPIError(429, "rate limited"),
                build_chat_response(
                    content="Fallback response",
                    model="anthropic/claude-haiku-4.5",
                ),
            ]
        )
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=logger,
            backoff_base_seconds=0.25,
            sleep=fake_sleep,
        )
        result = await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=200,
        )
        return result, logger, openai_client, sleep_calls

    result, logger, openai_client, sleep_calls = asyncio.run(run())

    assert result == "Fallback response"
    assert [call["model"] for call in openai_client.calls] == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4.5",
    ]
    assert sleep_calls == [0.25]

    retry_log = logger.find("warning", "llm_completion_retrying")
    assert retry_log["status_code"] == 429
    assert retry_log["fallback_after_error"] is True
    assert retry_log["next_model"] == "anthropic/claude-haiku-4.5"

    success_log = logger.find("info", "llm_completion_succeeded")
    assert success_log["active_model"] == "anthropic/claude-haiku-4.5"
    assert success_log["fallback_used"] is True


def test_llm_client_retries_same_model_on_timeout_and_uses_usage_cost() -> None:
    async def run() -> tuple[str, DummyLogger, FakeOpenAIClient, list[float]]:
        logger = DummyLogger()
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        openai_client = FakeOpenAIClient(
            [
                httpx.ReadTimeout("request timed out"),
                build_chat_response(content="Retried response", cost=0.0024),
            ]
        )
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=logger,
            backoff_base_seconds=0.5,
            sleep=fake_sleep,
        )
        result = await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=150,
        )
        return result, logger, openai_client, sleep_calls

    result, logger, openai_client, sleep_calls = asyncio.run(run())

    assert result == "Retried response"
    assert [call["model"] for call in openai_client.calls] == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
    ]
    assert sleep_calls == [0.5]

    retry_log = logger.find("warning", "llm_completion_retrying")
    assert retry_log["fallback_after_error"] is False
    assert retry_log["status_code"] is None

    success_log = logger.find("info", "llm_completion_succeeded")
    assert success_log["estimated_cost"] == pytest.approx(0.0024)
    assert success_log["cost_source"] == "response_usage"


@pytest.mark.parametrize(
    ("exception_factory", "message"),
    [
        (
            lambda request: APITimeoutError(request=request),
            "Request timed out.",
        ),
        (
            lambda request: APIConnectionError(message="connection dropped", request=request),
            "connection dropped",
        ),
    ],
)
def test_llm_client_retries_openai_transport_errors(
    exception_factory,
    message: str,
) -> None:
    async def run() -> tuple[str, DummyLogger, FakeOpenAIClient, list[float]]:
        logger = DummyLogger()
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        openai_client = FakeOpenAIClient(
            [
                exception_factory(request),
                build_chat_response(content="Retried transport response"),
            ]
        )
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=logger,
            backoff_base_seconds=0.25,
            sleep=fake_sleep,
        )
        result = await client.complete(
            system_prompt="System prompt",
            user_prompt="User prompt",
            max_tokens=150,
        )
        return result, logger, openai_client, sleep_calls

    result, logger, openai_client, sleep_calls = asyncio.run(run())

    assert result == "Retried transport response"
    assert [call["model"] for call in openai_client.calls] == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
    ]
    assert sleep_calls == [0.25]

    retry_log = logger.find("warning", "llm_completion_retrying")
    assert retry_log["status_code"] is None
    assert retry_log["error"] == message


def test_llm_client_raises_non_retryable_errors_without_retrying() -> None:
    async def run() -> tuple[DummyLogger, FakeOpenAIClient, list[float]]:
        logger = DummyLogger()
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        openai_client = FakeOpenAIClient([FakeAPIError(400, "bad request")])
        client = LLMClient(
            settings=build_settings(),
            api_key="test-key",
            client=openai_client,
            metadata_client=FakeMetadataClient(),
            logger=logger,
            sleep=fake_sleep,
        )
        with pytest.raises(FakeAPIError, match="bad request"):
            await client.complete(
                system_prompt="System prompt",
                user_prompt="User prompt",
                max_tokens=100,
            )
        return logger, openai_client, sleep_calls

    logger, openai_client, sleep_calls = asyncio.run(run())

    assert len(openai_client.calls) == 1
    assert sleep_calls == []

    error_log = logger.find("error", "llm_completion_failed")
    assert error_log["status_code"] == 400
    assert error_log["active_model"] == "anthropic/claude-sonnet-4-6"
