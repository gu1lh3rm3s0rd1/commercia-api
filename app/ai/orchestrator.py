"""One user question → (model ↔ tools)* → answer.

The model detects the intent and fills the arguments through function calling; this loop
runs each requested tool with the user's access and hands the results back, for at most
MAX_ROUNDS rounds (so a confused model can't loop forever or burn the quota).
"""

from dataclasses import dataclass, field
from typing import Any

from app.ai import tools
from app.ai.providers.base import LLMProvider, ToolCall

MAX_ROUNDS = 4
MAX_CALLS_PER_ROUND = 6

FALLBACK = ("Não consegui chegar a uma resposta para isso. Pode reformular a pergunta, "
            "dizendo o período e, se tiver mais de uma loja, qual delas?")


@dataclass
class ToolRun:
    call: ToolCall
    result: dict[str, Any]


@dataclass
class Answer:
    text: str
    tool_runs: list[ToolRun] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def answer(provider: LLMProvider, ctx: tools.ToolContext, system: str,
           history: list[tuple[str, str]], question: str) -> Answer:
    session = provider.start(system, history, tools.specs())
    turn = session.send_user(question)
    result = Answer(text="", input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)

    rounds = 0
    while turn.tool_calls and rounds < MAX_ROUNDS:
        rounds += 1
        executed = []
        for i, call in enumerate(turn.tool_calls):
            outcome = (tools.run(ctx, call) if i < MAX_CALLS_PER_ROUND
                       else {"erro": "muitas chamadas de uma vez; peça menos dados por rodada"})
            executed.append((call, outcome))
            result.tool_runs.append(ToolRun(call, outcome))
        turn = session.send_tool_results(executed)
        result.input_tokens += turn.input_tokens
        result.output_tokens += turn.output_tokens

    result.text = (turn.text or "").strip() if not turn.tool_calls else ""
    if not result.text:
        result.text = FALLBACK
    return result
