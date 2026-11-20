"""LLM player: OpenAI chat completions or Anthropic messages, with structured output through
PydanticAI."""

import time

import anthropic
import httpx2
import openai
import pydantic_ai
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model as AIModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from ollmpiada.config import Effort, Model
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, TurnResponse, Usage

# The first-run banner would print into the terminal UI.
pydantic_ai.BANNER_ENABLED = False

# Appended to the game's system prompt, which ends with a newline.
ANSWER_FORMAT = """
ANSWER FORMAT

Answer with three fields: action (move, resign or accept_draw); move (your move
exactly as listed under LEGAL MOVES, required when action is move); offer_draw
(true to offer a draw together with your move, otherwise false).
"""


class LLMPlayer:
    def __init__(
        self, name: str, model: AIModel, *, timeout_s: float, settings: ModelSettings | None = None
    ) -> None:
        # The game master owns every retry, so the provider client makes one attempt.
        settings = ModelSettings(request_timeout=timeout_s, max_retries=0) | (settings or {})
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
                run = await self._agent.run(
                    request.prompt.user, instructions=request.prompt.system + ANSWER_FORMAT
                )
                answer = run.output
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
    """Build the player for an `openai-completions` or `anthropic-messages` provider."""
    url, token = model.provider.url, model.token
    key = token or "unused"  # the clients refuse an empty key; the header is omitted below
    settings = ModelSettings()
    if model.provider.api == "anthropic-messages":
        provider = AnthropicProvider(base_url=url, api_key=key, http_client=http_client)
        # A proxy route prefix (`anthropic/claude-...`) would hide the Claude profile,
        # which decides how thinking and effort are sent.
        profile = provider.model_profile(model.spec.model.rpartition("/")[2])
        ai_model = AnthropicModel(model.spec.model, provider=provider, profile=profile)
        if effort:
            settings["thinking"] = False if effort == "none" else effort
        omit = anthropic.Omit()
    else:
        provider = OpenAIProvider(base_url=url, api_key=key, http_client=http_client)
        ai_model = OpenAIChatModel(model.spec.model, provider=provider)
        if effort:
            settings["openai_reasoning_effort"] = effort
        omit = openai.Omit()
    if not token:
        # Without a token no Authorization header is sent (both SDKs send the key as a
        # bearer token): a proxy may handle security itself.
        settings["extra_headers"] = {"Authorization": omit}
    return LLMPlayer(name, ai_model, timeout_s=timeout_s, settings=settings)


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
