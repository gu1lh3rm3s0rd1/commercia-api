"""Gemini via the official google-genai SDK. Function calling is driven manually (the SDK's
automatic mode is off): the orchestrator decides what runs, with the user's permissions."""

import logging
from typing import Any

from google import genai
from google.genai import errors, types

from app.ai.providers.base import LLMUnavailable, ModelTurn, ToolCall, ToolSpec

log = logging.getLogger(__name__)

TIMEOUT_MS = 30_000


def _turn(response: types.GenerateContentResponse) -> ModelTurn:
    usage = response.usage_metadata
    calls = [ToolCall(name=c.name, args=dict(c.args or {}), id=c.id) for c in (response.function_calls or [])]
    text = None
    if not calls:
        try:
            text = response.text
        except ValueError:  # blocked / empty candidate
            text = None
    return ModelTurn(
        text=text,
        tool_calls=calls,
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
    )


_OVERLOADED = {500, 502, 503, 504}


class GeminiSession:
    """One question. The first call may walk down the model list (primary overloaded →
    next); once a model answered, the rest of the question stays on it — its function-call
    parts are sent back verbatim and belong to that model."""

    def __init__(self, provider: "GeminiProvider", config: types.GenerateContentConfig, contents: list[types.Content]):
        self._provider = provider
        self._config = config
        self._contents = contents
        self.model: str | None = None  # chosen on the first successful call

    def _call(self, model: str) -> types.GenerateContentResponse:
        return self._provider.client.models.generate_content(model=model, contents=self._contents, config=self._config)

    def _generate(self) -> ModelTurn:
        candidates = [self.model] if self.model else self._provider.models
        last_exc: Exception | None = None
        quota_hit = False
        for model in candidates:
            try:
                response = self._call(model)
                self.model = model
                break
            except errors.APIError as exc:
                log.warning("gemini %s error %s: %s", model, exc.code, (exc.message or "")[:200])
                if exc.code == 429:  # quotas are per model: the next one may still have room
                    quota_hit = True
                elif exc.code not in _OVERLOADED:
                    raise LLMUnavailable("unavailable", f"Gemini error {exc.code}") from exc
                last_exc = exc
            except Exception as exc:  # network errors, timeouts
                log.warning("gemini %s call failed: %r", model, exc)
                last_exc = exc
        else:
            if quota_hit:
                raise LLMUnavailable("rate_limited", "Gemini quota exhausted") from last_exc
            raise LLMUnavailable("unavailable", "Gemini overloaded or unreachable") from last_exc
        if response.candidates and response.candidates[0].content:
            # sent back verbatim on the next call (keeps Gemini's function-call signatures)
            self._contents.append(response.candidates[0].content)
        return _turn(response)

    def send_user(self, text: str) -> ModelTurn:
        self._contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        return self._generate()

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> ModelTurn:
        parts = [types.Part.from_function_response(name=call.name, response={"result": result})
                 for call, result in results]
        self._contents.append(types.Content(role="user", parts=parts))
        return self._generate()


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, models: str | list[str]):
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
        self.models = [models] if isinstance(models, str) else list(models)
        self.model = self.models[0]

    def start(self, system: str, history: list[tuple[str, str]], tools: list[ToolSpec]) -> GeminiSession:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [
            types.Content(role="user" if role == "user" else "model", parts=[types.Part.from_text(text=text)])
            for role, text in history
        ]
        return GeminiSession(self, config, contents)
