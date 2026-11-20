import asyncio
import json

import httpx
import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ollmpiada.config import load_models
from ollmpiada.players import PlayerError, create_player, resolve_player
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, Usage
from ollmpiada.players.decision import DecisionPlayer
from ollmpiada.players.llm import LLMPlayer, create_llm_player
from tests.fakes import ScriptedPlayer, unreadable

REQUEST = TurnRequest(prompt="PROMPT", legal_moves={"e2e4": "e4", "g1f3": "Nf3"})


@pytest.fixture
def models(models_file):
    return load_models(models_file, {"TEST_HOSTED_KEY": "secret"})


def play(player, request=REQUEST):
    return asyncio.run(player.play(request))


# --- resolving names ---


def test_resolve_bare_name_and_effort(models):
    assert resolve_player("local-llm", models) == (models["local-llm"], None)
    assert resolve_player("local-llm@high", models) == (models["local-llm"], "high")


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("nope", "unknown model 'nope'"),
        ("local-llm@medium", "does not accept effort 'medium'"),
        ("basal-mini@low", "does not accept effort 'low' .no efforts listed"),
    ],
)
def test_resolve_rejects_bad_names(models, name, message):
    with pytest.raises(PlayerError, match=message):
        resolve_player(name, models)


def test_resolve_rejects_unavailable_model(models_file):
    models = load_models(models_file, {})
    with pytest.raises(PlayerError, match="unavailable: set TEST_HOSTED_KEY"):
        resolve_player("hosted-llm", models)


def test_create_player_picks_adapter_by_type(models):
    assert isinstance(create_player("local-llm@low", models, timeout_s=5), LLMPlayer)
    decision = create_player("basal-mini", models, timeout_s=5)
    assert isinstance(decision, DecisionPlayer) and decision.name == "basal-mini"


# --- LLM player ---


def function_player(reply, effort=None) -> tuple[LLMPlayer, list[AgentInfo]]:
    seen: list[AgentInfo] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return reply(messages, info)

    return LLMPlayer("p", FunctionModel(respond), effort=effort, timeout_s=7), seen


def tool_reply(args: dict, usage=RequestUsage()):
    return lambda messages, info: ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage
    )


def test_llm_answer_with_usage_and_settings():
    usage = RequestUsage(input_tokens=50, output_tokens=20, output_reasoning_tokens=12)
    player, seen = function_player(
        tool_reply({"action": "move", "move": "Nf3", "offer_draw": True}, usage), effort="high"
    )
    response = play(player)
    assert response.answer == TurnAnswer(action="move", move="Nf3", offer_draw=True)
    assert json.loads(response.raw) == {"action": "move", "move": "Nf3", "offer_draw": True}
    assert response.usage == Usage(50, 20, 12)
    assert response.format_error is None and response.elapsed_s >= 0
    settings = seen[0].model_settings
    assert settings["openai_reasoning_effort"] == "high"
    assert settings["request_timeout"] == 7 and settings["max_retries"] == 0


def test_llm_prompt_is_shared_prompt_plus_answer_format():
    prompts = []

    def capture(messages, info):
        prompts.append(messages[-1].parts[-1].content)
        return tool_reply({"action": "resign"})(messages, info)

    player = LLMPlayer("p", FunctionModel(capture), effort=None, timeout_s=7)
    play(player)
    assert prompts[0].startswith("PROMPT\n") and "ANSWER FORMAT" in prompts[0]


def test_llm_without_effort_sends_none():
    player, seen = function_player(tool_reply({"action": "resign"}))
    assert play(player).answer == TurnAnswer(action="resign")
    assert "openai_reasoning_effort" not in seen[0].model_settings


@pytest.mark.parametrize(
    ("reply", "raw"),
    [
        (tool_reply({"action": "castle"}), '{"action":"castle"}'),
        (lambda messages, info: ModelResponse(parts=[TextPart("I play e4")]), "I play e4"),
    ],
    ids=["invalid-fields", "plain-text"],
)
def test_llm_format_error_is_returned_once_not_retried(reply, raw):
    player, seen = function_player(reply)
    response = play(player)
    assert response.answer is None
    assert response.format_error and "errors.pydantic.dev" not in response.format_error
    assert response.raw == raw
    assert len(seen) == 1


def test_llm_unreported_reasoning_tokens_are_unknown():
    usage = RequestUsage(input_tokens=10, output_tokens=2)
    player, _ = function_player(tool_reply({"action": "resign"}, usage))
    assert play(player).usage == Usage(10, 2, None)


def test_llm_api_error_is_a_transport_error():
    def fail(messages, info):
        raise ModelHTTPError(503, "m", body="overloaded")

    player, _ = function_player(fail)
    with pytest.raises(TransportError):
        play(player)


CHAT_COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "qwen",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {
                            "name": "final_result",
                            "arguments": '{"action":"move","move":"e4"}',
                        },
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 300,
        "completion_tokens": 40,
        "total_tokens": 340,
        "completion_tokens_details": {"reasoning_tokens": 25},
    },
}


@pytest.mark.parametrize(
    ("model_id", "authorization"), [("local-llm", None), ("hosted-llm", "Bearer secret")]
)
def test_llm_over_openai_compatible_http(models, model_id, authorization):
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=CHAT_COMPLETION)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    effort = "low" if model_id == "local-llm" else None
    player = create_llm_player(
        "p", models[model_id], effort=effort, timeout_s=5, http_client=client
    )
    response = play(player)

    assert response.answer == TurnAnswer(action="move", move="e4")
    assert response.usage == Usage(300, 40, 25)
    request = sent[0]
    assert str(request.url) == f"{models[model_id].provider.url}/chat/completions"
    assert request.headers.get("authorization") == authorization
    body = json.loads(request.content)
    assert body["model"] == models[model_id].spec.model
    assert body.get("reasoning_effort") == effort


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (None, None),
        ({"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}, Usage(30, 4, None)),
    ],
    ids=["no-usage", "no-reasoning-details"],
)
def test_llm_usage_not_reported_is_unknown(models, usage, expected):
    completion = {k: v for k, v in CHAT_COMPLETION.items() if k != "usage"}
    if usage:
        completion["usage"] = usage
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=completion))
    client = httpx2.AsyncClient(transport=transport)
    player = create_llm_player(
        "p", models["local-llm"], effort=None, timeout_s=5, http_client=client
    )
    assert play(player).usage == expected


def test_llm_http_server_error_is_a_transport_error_without_client_retries(models):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(500, json={"error": "boom"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    player = create_llm_player(
        "p", models["local-llm"], effort=None, timeout_s=5, http_client=client
    )
    with pytest.raises(TransportError):
        play(player)
    assert len(sent) == 1


# --- decision player ---


def decision_player(models, handler, model_id="basal-mini", token=None):
    model = models[model_id]
    if token:
        model = type(model)(model.spec, model.provider, token, True)
    return DecisionPlayer("d", model, timeout_s=5, transport=httpx.MockTransport(handler))


def answering(body, sent=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return httpx.Response(200, json=body)

    return handler


def test_decision_request_shape_without_token(models):
    sent = []
    body = {
        "action": {"choice": "move"},
        "move": {"choice": "g1f3"},
        "offer_draw": {"choice": "no"},
    }
    response = play(decision_player(models, answering(body, sent)))
    assert response.answer == TurnAnswer(action="move", move="g1f3")
    assert response.usage is None

    request = sent[0]
    assert str(request.url) == "http://basal/v1/systemone"
    assert "authorization" not in request.headers
    payload = json.loads(request.content)
    assert payload["model"] == "basal" and payload["state"] == "PROMPT"
    questions = payload["questions"]
    assert set(questions) == {"action", "move", "offer_draw"}
    assert all(q["type"] == "choice" for q in questions.values())
    assert set(questions["action"]["criteria"]) == {"move", "resign"}
    assert questions["move"]["criteria"] == {"e2e4": "e4", "g1f3": "Nf3"}
    assert set(questions["offer_draw"]["criteria"]) == {"yes", "no"}


def test_decision_offers_accept_draw_only_while_pending_and_sends_token(models):
    sent = []
    request = TurnRequest("PROMPT", {"e2e4": "e4"}, draw_offered=True)
    body = {"action": "accept_draw"}
    response = play(decision_player(models, answering(body, sent), token="tok"), request)
    assert response.answer == TurnAnswer(action="accept_draw")
    assert sent[0].headers["authorization"] == "Bearer tok"
    assert "accept_draw" in json.loads(sent[0].content)["questions"]["action"]["criteria"]


@pytest.mark.parametrize(
    "body",
    [
        {"answers": {"action": "move", "move": "e2e4", "offer_draw": "yes"}},
        {
            "data": {
                "predictions": {
                    "action": {"value": "move"},
                    "move": {"answer": "e2e4"},
                    "offer_draw": {"selected": "yes"},
                }
            }
        },
        {
            "result": {
                "action": {"choice": "move", "probabilities": {"move": 0.9}},
                "move": {"choice": "e2e4"},
                "offer_draw": {"choice": "yes"},
            }
        },
    ],
    ids=["answers", "nested-wrappers", "result-with-probabilities"],
)
def test_decision_parses_nested_containers(models, body):
    response = play(decision_player(models, answering(body)))
    assert response.answer == TurnAnswer(action="move", move="e2e4", offer_draw=True)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"nothing": 1}, "no answers"),
        ({"action": "castle"}, "invalid answer to 'action'"),
        ({"action": "accept_draw"}, "invalid answer to 'action'"),
        ({"action": "move", "move": "e7e5", "offer_draw": "no"}, "invalid answer to 'move'"),
    ],
)
def test_decision_bad_answers_are_format_errors(models, body, error):
    response = play(decision_player(models, answering(body)))
    assert response.answer is None and error in response.format_error


def test_decision_reports_usage_when_present(models):
    body = {"action": "resign", "usage": {"prompt_tokens": 900, "completion_tokens": 3}}
    assert play(decision_player(models, answering(body))).usage == Usage(900, 3, None)
    body["usage"]["reasoning_tokens"] = 7
    assert play(decision_player(models, answering(body))).usage == Usage(900, 3, 7)


def test_decision_error_body_is_a_transport_error(models):
    with pytest.raises(TransportError, match="too many options"):
        play(decision_player(models, answering({"error": "too many options"})))


def test_decision_non_json_is_a_format_error(models):
    response = play(decision_player(models, lambda request: httpx.Response(200, text="oops")))
    assert response.answer is None and response.raw == "oops"


@pytest.mark.parametrize("status", [500, 503, 400])
def test_decision_http_errors_are_transport_errors(models, status):
    handler = lambda request: httpx.Response(status, text="too many options")  # noqa: E731
    with pytest.raises(TransportError, match=f"HTTP {status}: too many options"):
        play(decision_player(models, handler))


def test_decision_connection_error_is_a_transport_error(models):
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(TransportError, match="ConnectError"):
        play(decision_player(models, handler))


# --- scripted fake ---


def test_scripted_player_follows_its_script():
    script = ["e4", TurnAnswer(action="resign"), unreadable(), TimeoutError()]
    player = ScriptedPlayer("fake", script)
    assert play(player).answer == TurnAnswer(action="move", move="e4")
    assert play(player).answer == TurnAnswer(action="resign")
    assert play(player).format_error == "not a TurnAnswer"
    with pytest.raises(TimeoutError):
        play(player)
    with pytest.raises(AssertionError, match="script ended"):
        play(player)
    assert len(player.requests) == 5
