"""Provider-neutral interface the orchestrator talks to. Only app/ai/providers/gemini.py
knows Gemini exists; tests use providers/fake.py. Swapping the LLM = another provider here.

A provider opens one `LLMSession` per user question: the session keeps the provider's own
message format (Gemini needs its raw function-call parts sent back verbatim), the
orchestrator only sees `ModelTurn`s.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMUnavailable(Exception):
    """The model can't answer right now (no key, quota exhausted, timeout, outage).
    Surfaces as 503 — never a 500."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason  # "not_configured" | "rate_limited" | "unavailable"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the arguments


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class LLMSession(Protocol):
    def send_user(self, text: str) -> ModelTurn: ...

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> ModelTurn: ...


class LLMProvider(Protocol):
    name: str

    def start(self, system: str, history: list[tuple[str, str]], tools: list[ToolSpec]) -> LLMSession:
        """history: earlier (role, text) pairs of the conversation, role 'user'|'assistant'."""
        ...
