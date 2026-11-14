"""Scripted stand-in for tests: no network, no quota. Each step of the script is either a
ModelTurn or a function(seen) -> ModelTurn, where `seen` is everything the session got so
far (the user text, then the tool results) — so a test can assert on what reached the model."""

from collections.abc import Callable
from typing import Any

from app.ai.providers.base import LLMUnavailable, ModelTurn, ToolCall, ToolSpec

Step = ModelTurn | Callable[[list[Any]], ModelTurn] | LLMUnavailable


class FakeSession:
    def __init__(self, provider: "FakeProvider"):
        self._provider = provider
        self.seen: list[Any] = []

    def _next(self) -> ModelTurn:
        if not self._provider.script:
            return ModelTurn(text="(fim do roteiro)")
        step = self._provider.script.pop(0)
        if isinstance(step, LLMUnavailable):
            raise step
        return step(self.seen) if callable(step) else step

    def send_user(self, text: str) -> ModelTurn:
        self.seen.append(text)
        return self._next()

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> ModelTurn:
        self.seen.append(results)
        return self._next()


class FakeProvider:
    name = "fake"

    def __init__(self, script: list[Step]):
        self.script = list(script)
        self.started: list[dict[str, Any]] = []  # what each start() received
        self.sessions: list[FakeSession] = []

    def start(self, system: str, history: list[tuple[str, str]], tools: list[ToolSpec]) -> FakeSession:
        self.started.append({"system": system, "history": history, "tools": [t.name for t in tools]})
        session = FakeSession(self)
        self.sessions.append(session)
        return session
