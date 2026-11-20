"""LLM player: an OpenAI-compatible chat model with structured output through PydanticAI."""

import time

import httpx2
import pydantic_ai
from openai import Omit
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model as AIModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from ollmpiada.config import Effort, Model
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, TurnResponse, Usage

# The first-run banner would print into the terminal UI.
pydantic_ai.BANNER_ENABLED = False

ANSWER_FORMAT = """

ANSWER FORMAT

Answer with three fields: action (move, resign or accept_draw); move (your move
exactly as listed under LEGAL MOVES, required when action is move); offer_draw
(true to offer a draw together with your move, otherwise false).
"""


class LLMPlayer:
    def __init__(
        self, name: str, model: AIModel, *, effort: Effort | None, timeout_s: float
    ) -> None:
        settings = OpenAIChatModelSettings(request_timeout=timeout_s, max_retries=0)
        if effort:
            settings["openai_reasoning_effort"] = effort
        self.name = name
        # Output retries are off so every invalid answer reaches the game master to be counted.
        self._agent = Agent(
            model, output_type=TurnAnswer, retries={"output": 0}, model_settings=settings
        )

    async def play(self, request: TurnRequest) -> TurnResponse:
        answer, format_error = None, None
        start = time.perf_counter()
        with capture_run_messages() as messages:
            try:
                answer = (await self._agent.run(request.prompt + ANSWER_FORMAT)).output
            except UnexpectedModelBehavior as e:
                format_error = _describe(e)
            except ModelAPIError as e:
                raise TransportError(str(e)) from e
        elapsed_s = time.perf_counter() - start
        responses = [m for m in messages if isinstance(m, ModelResponse)]
        return TurnResponse(
            raw=_raw(responses[-1]) if responses else "",
            answer=answer,
            elapsed_s=elapsed_s,
            usage=_usage(responses),
            format_error=format_error,
        )


def create_llm_player(
    name: str,
    model: Model,
    *,
    effort: Effort | None,
    timeout_s: float,
    http_client: httpx2.AsyncClient | None = None,
) -> LLMPlayer:
    provider = OpenAIProvider(
        base_url=model.provider.url, api_key=model.token or "unused", http_client=http_client
    )
    # Without a token no Authorization header is sent: a proxy may handle security itself.
    no_auth = OpenAIChatModelSettings(extra_headers={"Authorization": Omit()})
    settings = None if model.token else no_auth
    chat_model = OpenAIChatModel(model.spec.model, provider=provider, settings=settings)
    return LLMPlayer(name, chat_model, effort=effort, timeout_s=timeout_s)


def _describe(error: UnexpectedModelBehavior) -> str:
    """The validation problem behind PydanticAI's generic "exceeded retries" error."""
    lines = str(error.__cause__ or error).splitlines()
    return "\n".join(line for line in lines if "errors.pydantic.dev" not in line)


def _raw(response: ModelResponse) -> str:
    texts = []
    for part in response.parts:
        if isinstance(part, ToolCallPart):
            texts.append(part.args_as_json_str())
        elif isinstance(part, TextPart):
            texts.append(part.content)
    return "\n".join(texts)


def _usage(responses: list[ModelResponse]) -> Usage | None:
    if not responses or any(r.usage.unmeasured_requests for r in responses):
        return None
    reasoning_known = all(
        r.usage.output_reasoning_tokens or "reasoning_tokens" in r.usage.details for r in responses
    )
    return Usage(
        input_tokens=sum(r.usage.input_tokens for r in responses),
        output_tokens=sum(r.usage.output_tokens for r in responses),
        reasoning_tokens=(
            sum(r.usage.output_reasoning_tokens for r in responses) if reasoning_known else None
        ),
    )
