"""Observe native Responses payloads before Agno can reject or discard them."""
from __future__ import annotations

from typing import Any

from .agent_run_metrics import ProviderUsageAttempt


def _observe_event(attempt: ProviderUsageAttempt, event: Any) -> None:
    # Created events retain the response ID even if the stream later disconnects.
    try:
        response = getattr(event, "response", None)
        if response is not None:
            attempt.observe(response)
    except Exception:
        return


def _observe_request_id(attempt: ProviderUsageAttempt, stream: Any) -> None:
    try:
        attempt.request_id(stream.response.headers.get("x-request-id"))
    except Exception:
        return


class ObservedResponses:
    def __init__(self, resource: Any) -> None:
        self.resource = resource

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resource, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        attempt = ProviderUsageAttempt(kwargs.get("model", "unknown"))
        try:
            response = self.resource.create(*args, **kwargs)
        except BaseException as error:
            attempt.finish(error)
            raise
        if kwargs.get("stream"):
            _observe_request_id(attempt, response)
            return self._stream(response, attempt)
        attempt.observe(response)
        attempt.finish()
        return response

    @staticmethod
    def _stream(stream: Any, attempt: ProviderUsageAttempt) -> Any:
        try:
            for event in stream:
                _observe_event(attempt, event)
                yield event
        except BaseException as error:
            attempt.finish(error)
            raise
        finally:
            attempt.finish()
            try:
                stream.close()
            except Exception:
                pass


class ObservedAsyncResponses(ObservedResponses):
    async def create(self, *args: Any, **kwargs: Any) -> Any:
        attempt = ProviderUsageAttempt(kwargs.get("model", "unknown"))
        try:
            response = await self.resource.create(*args, **kwargs)
        except BaseException as error:
            attempt.finish(error)
            raise
        if kwargs.get("stream"):
            _observe_request_id(attempt, response)
            return self._async_stream(response, attempt)
        attempt.observe(response)
        attempt.finish()
        return response

    @staticmethod
    async def _async_stream(stream: Any, attempt: ProviderUsageAttempt) -> Any:
        try:
            async for event in stream:
                _observe_event(attempt, event)
                yield event
        except BaseException as error:
            attempt.finish(error)
            raise
        finally:
            attempt.finish()
            try:
                await stream.close()
            except Exception:
                pass


class UsageObservedClient:
    """A per-access facade, never a mutation of a shared SDK client."""
    def __init__(self, client: Any, *, asynchronous: bool = False) -> None:
        self.client = client
        self.responses = (ObservedAsyncResponses if asynchronous else ObservedResponses)(client.responses)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)
