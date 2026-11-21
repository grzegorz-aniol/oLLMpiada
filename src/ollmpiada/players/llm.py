"""LLM player: OpenAI chat completions or Anthropic messages, with structured output through
PydanticAI."""

import time

import anthropic
import httpx2
import openai
import pydantic_ai
from pydantic_ai import Agent, NativeOutput, PromptedOutput, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model as AIModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from ollmpiada.config import Effort, Model, OutputMode
from ollmpiada.games.base import Prompt
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, TurnResponse, Usage

# The first-run banner would print into the terminal UI.
pydantic_ai.BANNER_ENABLED = False

# Follows the game's system part, which ends with a newline.
ANSWER_FORMAT = """
ANSWER FORMAT

Answer with three fields: action (move, resign or accept_draw); move (your move
exactly as listed under LEGAL MOVES, required when action is move); offer_draw
(true to offer a draw together with your move, otherwise false).
"""

# How PydanticAI asks for the answer: an output tool, the server's JSON schema mode, or the
# schema in the instructions with the reply parsed from text (configuration docs).
_OUTPUT_TYPES = {
    "tool": TurnAnswer,
    "native": NativeOutput(TurnAnswer),
    "prompted": PromptedOutput(TurnAnswer),
}


def llm_message(prompt: Prompt) -> str:
    """The one user message an LLM player sends: rules, answer format, then the turn.

    No system message comes from ollmpiada, since some chat templates refuse a second one and
    PydanticAI adds its own for prompted output. The text that is the same every turn goes
    first, so a server can reuse its cached prefix.
    """
    return f"{prompt.system}{ANSWER_FORMAT}\n{prompt.user}"


class LLMPlayer:
    def __init__(
        self,
        name: str,
        model: AIModel,
        *,
        timeout_s: float,
        settings: ModelSettings | None = None,
        output_mode: OutputMode = "tool",
    ) -> None:
        # The game master owns every retry, so the provider client makes one attempt.
        # `request_timeout` only shortens a call; `timeout` replaces the SDK client's own
        # per-attempt limit (600 s), which a slow local server would otherwise hit.
        settings = ModelSettings(request_timeout=timeout_s, timeout=timeout_s, max_retries=0) | (
            settings or {}
        )
        self.name = name
        # Output retries are off so every invalid answer reaches the game master to be counted.
        self._agent = Agent(
            model,
            output_type=_OUTPUT_TYPES[output_mode],
            retries={"output": 0},
            model_settings=settings,
        )

    async def play(self, request: TurnRequest) -> TurnResponse:
        answer, format_error = None, None
        start = time.perf_counter()
        with capture_run_messages() as messages:
            try:
                run = await self._agent.run(llm_message(request.prompt))
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
    if model.spec.max_tokens:
        settings["max_tokens"] = model.spec.max_tokens
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
        # A partial profile is merged over the provider's default. The cap goes out as
        # `max_tokens`, the field local servers and OpenRouter read (see configuration docs).
        profile = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
        if model.spec.output_mode == "prompted":
            # Prompted output would send `response_format: json_object`, which some local
            # servers cannot turn into a grammar; without it nothing is constrained server-side.
            profile["supports_json_object_output"] = False
        ai_model = OpenAIChatModel(model.spec.model, provider=provider, profile=profile)
        if effort:
            settings["openai_reasoning_effort"] = effort
        if model.spec.reasoning_budget:
            # llama.cpp's field name; other servers ignore it or use their own (configuration docs).
            settings["extra_body"] = {"reasoning_budget_tokens": model.spec.reasoning_budget}
        omit = openai.Omit()
    settings["extra_headers"] = dict(model.provider.headers)
    if not token:
        # Without a token no Authorization header is sent (both SDKs send the key as a
        # bearer token): a proxy may handle security itself.
        settings["extra_headers"]["Authorization"] = omit
    return LLMPlayer(
        name, ai_model, timeout_s=timeout_s, settings=settings, output_mode=model.spec.output_mode
    )


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
