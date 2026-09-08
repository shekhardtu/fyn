"""Normalize provider failures without exposing upstream diagnostics to clients."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from agno.exceptions import InputCheckError, ModelAuthenticationError, ModelProviderError, OutputCheckError
from agno.models.openai.responses import OpenAIResponses
from agno.run.agent import RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from openai import OpenAIError

from .agent_run_metrics import provider_usage_stage
from .provider_usage import UsageObservedClient


_NOTICES = {
    "provider_quota_exhausted": (
        "Fyn’s AI service has reached its usage limit. Requests that need AI are unavailable "
        "until service capacity is restored. You can still use the app’s transaction forms."
    ),
    "provider_authentication_failed": (
        "Fyn’s AI service needs a configuration update. Please try again later. "
        "You can still use the app’s transaction forms."
    ),
    "provider_rate_limited": (
        "Fyn’s AI service is receiving too many requests. Please try again shortly. "
        "You can still use the app’s transaction forms."
    ),
    "provider_response_incomplete": (
        "Fyn’s AI service did not finish its response. Please try again. "
        "You can still use the app’s transaction forms."
    ),
    "provider_unavailable": (
        "Fyn couldn’t reach its AI service or receive a complete response. Please try again later. "
        "You can still use the app’s transaction forms."
    ),
}
_QUOTA_CODES = {
    "credit_balance_exhausted", "insufficient_quota", "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded", "organization_usage_limit_exceeded",
}
_PROVIDER_ERROR_TYPES = {
    "model_provider_error", "model_authentication_error", "model_rate_limit_error",
    "context_window_exceeded_error", "ModelProviderError", "ModelAuthenticationError",
    "ModelRateLimitError", "ContextWindowExceededError", "ProviderUnavailableError",
}
_VALIDATION_ERROR_TYPES = {"input_check_error", "output_check_error", "InputCheckError", "OutputCheckError"}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class AgentExecutionError(RuntimeError):
    """A framework/guardrail failure is not an outage or a valid empty answer."""

    def __init__(self, code: str = "agent_execution_failed"):
        self.code = code
        self.failure_stage = "agent_validation" if code == "agent_validation_failed" else "execution"
        super().__init__("Fyn couldn’t complete this response safely. Please try again.")


class ProviderUnavailableError(RuntimeError):
    def __init__(self, code: str = "provider_unavailable"):
        self.code = code if code in _NOTICES else "provider_unavailable"
        # Agno retains .type as RunErrorEvent.error_type, even when it catches
        # the exception and replaces RunOutput.content with a plain string.
        self.type = self.code
        self.title = "AI service unavailable"
        self.notice = _NOTICES[self.code]
        self.retryable = self.code in {"provider_rate_limited", "provider_unavailable"}
        super().__init__(f"{self.title}. {self.notice}")

    @classmethod
    def from_error(cls, error: Any) -> ProviderUnavailableError:
        # Follow Agno's exception cause to the SDK's structured error before
        # falling back to text. Never publish or persist upstream diagnostics.
        chain: list[Any] = []
        seen: set[int] = set()
        current = error
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(current)
            if isinstance(current, cls):
                return current
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        codes = set()
        for item in chain:
            body = _field(item, "body")
            for value in (item, body, _field(body, "error")):
                for field in ("code", "type", "error_type"):
                    code = _field(value, field)
                    if isinstance(code, str):
                        codes.add(code)
        for code in _NOTICES:
            if code in codes:
                return cls(code)
        if codes & _QUOTA_CODES:
            return cls("provider_quota_exhausted")
        if codes & {"invalid_api_key", "authentication_error", "model_authentication_error"}:
            return cls("provider_authentication_failed")
        if "rate_limit_exceeded" in codes:
            return cls("provider_rate_limited")
        diagnostic = " ".join(
            str(_field(item, "content") or _field(item, "message") or item)[:3000]
            for item in chain
        ).casefold()
        if any(value in diagnostic for value in (
            "credit_balance_exhausted", "insufficient_quota", "no credits remaining",
            "exceeded your current quota", "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        )):
            return cls("provider_quota_exhausted")
        if any(_field(item, "status_code") in {401, 403} for item in chain):
            return cls("provider_authentication_failed")
        if any(_field(item, "status_code") == 429 for item in chain) or any(value in diagnostic for value in (
            "rate_limit_exceeded", "rate limit", "too many requests",
        )):
            return cls("provider_rate_limited")
        return cls()


_call_failure: ContextVar[list[ProviderUnavailableError] | None] = ContextVar("provider_call_failure", default=None)


@contextmanager
def _normalize_model_errors() -> Iterator[None]:
    try:
        yield
    except (ModelProviderError, ModelAuthenticationError, OpenAIError, ProviderUnavailableError) as error:
        failure = ProviderUnavailableError.from_error(error)
        observed = _call_failure.get()
        if observed is not None:
            observed[:] = [failure]
        if failure is error:
            raise
        raise failure from error


def _check_error_event(event: RunErrorEvent) -> None:
    if event.error_type in _VALIDATION_ERROR_TYPES:
        raise AgentExecutionError("agent_validation_failed")
    failure = ProviderUnavailableError.from_error(event)
    if event.error_type in _PROVIDER_ERROR_TYPES or event.error_type in _NOTICES or failure.code != "provider_unavailable":
        raise failure
    raise AgentExecutionError()


def _check_run_output(output: Any) -> None:
    if getattr(output, "status", None) == RunStatus.error:
        for event in reversed(getattr(output, "events", None) or []):
            if isinstance(event, RunErrorEvent):
                _check_error_event(event)
        # Non-streaming Agno runs may omit events entirely and retain only
        # content. Recover the typed failure from this call, never from prose
        # left by another turn or an independently checked delegate.
        observed = _call_failure.get()
        if observed:
            raise observed[-1]
        failure = ProviderUnavailableError.from_error(output)
        if failure.code != "provider_unavailable":
            raise failure
        raise AgentExecutionError()
    if isinstance(output, RunOutput) and output.status != RunStatus.completed:
        raise AgentExecutionError("agent_response_incomplete")


def checked_provider_call(run: Callable[[], Any], *, on_output: Callable[[Any], None] | None = None, stage: str = "model") -> Any:
    token = _call_failure.set([])
    try:
        with provider_usage_stage(stage):
            output = run()
        if on_output is not None:
            on_output(output)
        _check_run_output(output)
        return output
    except (InputCheckError, OutputCheckError) as error:
        raise AgentExecutionError("agent_validation_failed") from error
    except (ModelProviderError, ModelAuthenticationError, OpenAIError) as error:
        raise ProviderUnavailableError.from_error(error) from error
    finally:
        _call_failure.reset(token)


def checked_provider_stream(run: Callable[[], Iterable[Any]], *, stage: str = "model") -> Iterator[Any]:
    """Agno may yield a failure instead of raising; never accept it as a reply."""
    token = _call_failure.set([])
    try:
        with provider_usage_stage(stage):
            for event in run():
                if isinstance(event, RunErrorEvent):
                    _check_error_event(event)
                if isinstance(event, RunOutput):
                    _check_run_output(event)
                yield event
    except (InputCheckError, OutputCheckError) as error:
        raise AgentExecutionError("agent_validation_failed") from error
    except (ModelProviderError, ModelAuthenticationError, OpenAIError) as error:
        raise ProviderUnavailableError.from_error(error) from error
    finally:
        _call_failure.reset(token)


_stream_completed: ContextVar[list[bool] | None] = ContextVar("provider_stream_completed", default=None)


class CheckedOpenAIResponses(OpenAIResponses):
    """Own the native response boundary that Agno 2.x does not fully check.

    Request-local terminal state supports concurrent/nested calls. Transport
    exceptions retain their SDK causes until normalized, and explicit provider
    failures cannot be mistaken for successful empty/partial model output.
    """

    def get_client(self) -> Any:
        client = super().get_client()
        if client.max_retries != 0:
            client = client.with_options(max_retries=0)
        return UsageObservedClient(client)

    def get_async_client(self) -> Any:
        client = super().get_async_client()
        if client.max_retries != 0:
            client = client.with_options(max_retries=0)
        return UsageObservedClient(client, asynchronous=True)

    def _check_response(self, response: Any) -> None:
        status = _field(response, "status")
        if status not in {"completed", "failed"}:
            raise ProviderUnavailableError("provider_response_incomplete")
        if status == "failed" or _field(response, "error"):
            raise ProviderUnavailableError.from_error(_field(response, "error"))

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> Any:
        self._check_response(response)
        return super()._parse_provider_response(response, **kwargs)

    # Matches OpenAIResponses' hook, which itself extends Model's one-argument
    # signature. This adapter is tested against the pinned provider version.
    def _parse_provider_response_delta(self, stream_event: Any, assistant_message: Any, tool_use: Any) -> Any:  # type: ignore[override]
        if stream_event.type == "error":
            raise ProviderUnavailableError.from_error(stream_event)
        if stream_event.type in {"response.failed", "response.incomplete", "response.completed"}:
            self._check_response(stream_event.response)
            if stream_event.type != "response.completed":
                raise ProviderUnavailableError("provider_response_incomplete")
            completed = _stream_completed.get()
            if completed is not None:
                completed[0] = True
        return super()._parse_provider_response_delta(stream_event, assistant_message, tool_use)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        with _normalize_model_errors():
            return super().invoke(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        with _normalize_model_errors():
            return await super().ainvoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        completed = [False]
        token = _stream_completed.set(completed)
        try:
            with _normalize_model_errors():
                yield from super().invoke_stream(*args, **kwargs)
                if not completed[0]:
                    raise ProviderUnavailableError("provider_response_incomplete")
        finally:
            _stream_completed.reset(token)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> Any:
        completed = [False]
        token = _stream_completed.set(completed)
        try:
            with _normalize_model_errors():
                async for event in super().ainvoke_stream(*args, **kwargs):
                    yield event
                if not completed[0]:
                    raise ProviderUnavailableError("provider_response_incomplete")
        finally:
            _stream_completed.reset(token)
