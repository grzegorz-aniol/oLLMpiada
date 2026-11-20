import asyncio
import dataclasses
import json
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ollmpiada.config import load_models
from ollmpiada.games.base import Prompt
from ollmpiada.players import PlayerError, create_player, decision, resolve_player
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, Usage
from ollmpiada.players.decision import DecisionPlayer
from ollmpiada.players.llm import ANSWER_FORMAT, LLMPlayer, create_llm_player
from tests.fakes import ScriptedPlayer, unreadable

PROMPT = Prompt(system="RULES\n", user="POSITION\n")
REQUEST = TurnRequest(prompt=PROMPT, legal_moves={"e2e4": "e4", "g1f3": "Nf3"})
SYSTEM_SENT = (PROMPT.system + ANSWER_FORMAT).strip()


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


def test_create_player_picks_adapter_by_provider_api(models):
    assert isinstance(create_player("local-llm@low", models, timeout_s=5), LLMPlayer)
    assert isinstance(create_player("claude@none", models, timeout_s=5), LLMPlayer)
    decision = create_player("basal-mini", models, timeout_s=5)
    assert isinstance(decision, DecisionPlayer) and decision.name == "basal-mini"


# --- LLM player ---


def function_player(reply, settings=None) -> tuple[LLMPlayer, list[AgentInfo]]:
    seen: list[AgentInfo] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return reply(messages, info)

    return LLMPlayer("p", FunctionModel(respond), timeout_s=7, settings=settings), seen


def tool_reply(args: dict, usage=RequestUsage()):
    return lambda messages, info: ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage
    )


def test_llm_answer_with_usage_and_settings():
    usage = RequestUsage(input_tokens=50, output_tokens=20, output_reasoning_tokens=12)
    player, seen = function_player(
        tool_reply({"action": "move", "move": "Nf3", "offer_draw": True}, usage),
        settings={"openai_reasoning_effort": "high"},
    )
    response = play(player)
    assert response.answer == TurnAnswer(action="move", move="Nf3", offer_draw=True)
    assert json.loads(response.raw) == {"action": "move", "move": "Nf3", "offer_draw": True}
    assert response.usage == Usage(50, 20, 12)
    assert response.format_error is None and response.elapsed_s >= 0
    settings = seen[0].model_settings
    assert settings["openai_reasoning_effort"] == "high"
    assert settings["request_timeout"] == 7 and settings["max_retries"] == 0


def test_llm_sends_rules_and_answer_format_as_instructions_and_the_turn_as_user_prompt():
    requests = []

    def capture(messages, info):
        requests.append(messages[-1])
        return tool_reply({"action": "resign"})(messages, info)

    player = LLMPlayer("p", FunctionModel(capture), timeout_s=7)
    play(player)
    request = requests[0]
    assert request.instructions == SYSTEM_SENT
    assert SYSTEM_SENT.startswith("RULES\n\nANSWER FORMAT\n")
    assert [part.content for part in request.parts] == ["POSITION\n"]


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
    assert body["messages"] == [
        {"role": "system", "content": SYSTEM_SENT},
        {"role": "user", "content": "POSITION\n"},
    ]


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


ANTHROPIC_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "anthropic/claude-haiku-5-5",
    "content": [
        {"type": "tool_use", "id": "t1", "name": "final_result", "input": {"action": "resign"}}
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 300, "output_tokens": 40},
}


def anthropic_reply(request: httpx2.Request) -> httpx2.Response:
    """Answer like the Messages API, as server-sent events: JSON text for native output,
    else the output tool. PydanticAI streams because a Claude model's default
    `max_tokens` is its maximum output, which the SDK refuses to wait for unstreamed."""
    body = json.loads(request.content)
    answer = '{"action": "resign"}'
    if "format" in body.get("output_config", {}):
        block, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": answer}
    else:
        block = {"type": "tool_use", "id": "t1", "name": "final_result", "input": {}}
        delta = {"type": "input_json_delta", "partial_json": answer}
    assert body["stream"], "current Claude models are always streamed"
    start = {**ANTHROPIC_MESSAGE, "content": [], "stop_reason": None}
    start["usage"] = {"input_tokens": 300, "output_tokens": 0}
    events = [
        ("message_start", {"message": start}),
        ("content_block_start", {"index": 0, "content_block": block}),
        ("content_block_delta", {"index": 0, "delta": delta}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}}),
        ("message_stop", {}),
    ]
    sse = "".join(
        f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events
    )
    return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize(
    ("effort", "token", "thinking", "output_effort", "authorization"),
    [
        (None, None, None, None, None),
        ("none", "tok", {"type": "disabled"}, None, "Bearer tok"),
        ("low", None, {"type": "adaptive"}, "low", None),
    ],
    ids=["model-default", "thinking-off", "adaptive-low"],
)
def test_llm_over_anthropic_messages_http(
    models, effort, token, thinking, output_effort, authorization
):
    """The fixture's model name carries a proxy prefix (`anthropic/claude-haiku-5-5`)."""
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return anthropic_reply(request)

    model = models["claude"]
    model = dataclasses.replace(model, token=token)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    player = create_llm_player("c", model, effort=effort, timeout_s=5, http_client=client)
    response = play(player)

    assert response.answer == TurnAnswer(action="resign")
    assert response.usage == Usage(300, 40, None)
    request = sent[0]
    assert str(request.url) == "http://proxy/anthropic/v1/messages"
    assert request.headers.get("authorization") == authorization
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-haiku-5-5"
    assert body["system"] == [{"type": "text", "text": SYSTEM_SENT}]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "POSITION\n"}]}
    ]
    assert body.get("thinking") == thinking
    assert body.get("output_config", {}).get("effort") == output_effort


def test_llm_anthropic_http_error_is_a_transport_error(models):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    player = create_llm_player("c", models["claude"], effort=None, timeout_s=5, http_client=client)
    with pytest.raises(TransportError):
        play(player)
    assert len(sent) == 1


# --- decision player ---


def decision_player(models, handler, model_id="basal-mini", token=None, max_options=None):
    model = models[model_id]
    if token:
        model = dataclasses.replace(model, token=token)
    if max_options:
        model = dataclasses.replace(
            model, spec=model.spec.model_copy(update={"max_options": max_options})
        )
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
    assert payload["model"] == "basal" and payload["state"] == "RULES\n\nPOSITION\n"
    questions = payload["questions"]
    assert set(questions) == {"action", "move", "offer_draw"}
    assert all(q["type"] == "choice" for q in questions.values())
    assert set(questions["action"]["criteria"]) == {"move", "resign"}
    assert questions["move"]["criteria"] == {"e2e4": "e4", "g1f3": "Nf3"}
    assert set(questions["offer_draw"]["criteria"]) == {"yes", "no"}


def test_decision_offers_accept_draw_only_while_pending_and_sends_token(models):
    sent = []
    request = TurnRequest(PROMPT, {"e2e4": "e4"}, draw_offered=True)
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


# --- decision player: moves split over several questions ---

# 30 moves from six origin squares, inserted out of order.
MANY_MOVES = {f"{f}1{f}{r}": f"{f.upper()}{r}" for f in "fedcba" for r in range(2, 7)}


def scripted(bodies, sent):
    """Answers each query with the next body and records the questions it asked."""
    replies = iter(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["questions"])
        return httpx.Response(200, json=next(replies))

    return handler


def many_request(moves=MANY_MOVES):
    return TurnRequest(PROMPT, moves)


def test_decision_splits_moves_equally_and_other_lists_the_unlisted_moves(models):
    sent = []
    bodies = [
        {"action": "move", "move": "other", "offer_draw": "yes"},
        {"move": "f1f6"},
    ]
    player = decision_player(models, scripted(bodies, sent), max_options=26)
    response = play(player, many_request())
    assert response.answer == TurnAnswer(action="move", move="f1f6", offer_draw=True)

    first, second = sent
    assert set(first) == {"action", "move", "offer_draw"} and set(second) == {"move"}
    first_moves = first["move"]["criteria"]
    second_moves = second["move"]["criteria"]
    other = first_moves.pop("other")
    assert "other" not in second_moves
    assert len(first_moves) == len(second_moves) == 15
    # Sorted ids keep each origin square's moves adjacent.
    assert list(first_moves) + list(second_moves) == sorted(MANY_MOVES)
    assert other.endswith(", ".join(second_moves.values()))
    assert not any(label in other for label in first_moves.values())


def test_decision_fitting_moves_are_one_question(models):
    sent = []
    body = {"action": "move", "move": "a1a2", "offer_draw": "no"}
    player = decision_player(models, scripted([body], sent), max_options=30)
    response = play(player, many_request())
    assert response.answer == TurnAnswer(action="move", move="a1a2")
    assert len(sent) == 1 and sent[0]["move"]["criteria"] == MANY_MOVES


def test_decision_split_rejects_a_move_id_equal_to_other(models):
    moves = {**MANY_MOVES, "other": "Other"}
    player = decision_player(models, scripted([], []), max_options=26)
    with pytest.raises(ValueError, match="'other'"):
        play(player, many_request(moves))


@pytest.mark.parametrize("max_options", [2, 3, 7, 26])
def test_decision_other_chain_reaches_the_last_group_within_limits(models, max_options):
    for count in range(max_options + 1, 4 * max_options + 2):
        moves = {f"m{i:03}": f"move {i}" for i in range(count)}
        sent = []

        def handler(request: httpx.Request) -> httpx.Response:
            questions = json.loads(request.content)["questions"]
            sent.append(questions["move"]["criteria"])
            choice = "other" if "other" in sent[-1] else next(iter(sent[-1]))
            return httpx.Response(200, json={"action": "move", "move": choice, "offer_draw": "no"})

        player = decision_player(models, handler, max_options=max_options)
        response = play(player, many_request(moves))

        groups = [[m for m in criteria if m != "other"] for criteria in sent]
        assert response.answer == TurnAnswer(action="move", move=groups[-1][0])
        assert [m for group in groups for m in group] == sorted(moves)
        assert all(2 <= len(criteria) <= max_options for criteria in sent)
        assert ["other" in criteria for criteria in sent] == [True] * (len(sent) - 1) + [False]
        # `other` lists every move outside its group, earlier and later ones alike.
        for criteria, group in zip(sent[:-1], groups, strict=False):
            outside = [moves[m] for m in sorted(moves) if m not in group]
            expected = "A move not listed in this question: " + ", ".join(outside)
            assert criteria["other"] == expected
        assert max(map(len, groups)) - min(map(len, groups)) <= 1
        # The fewest queries: one group less could not hold every move.
        assert (len(sent) - 2) * (max_options - 1) + max_options < count


def test_decision_resign_asks_no_further_move_question(models):
    sent = []
    player = decision_player(models, scripted([{"action": "resign"}], sent), max_options=26)
    response = play(player, many_request())
    assert response.answer == TurnAnswer(action="resign")
    assert len(sent) == 1


@pytest.mark.parametrize("max_options", [None, 26])
def test_decision_single_legal_move_is_played_without_a_move_question(models, max_options):
    sent = []
    body = {"action": "move", "offer_draw": "yes"}
    player = decision_player(models, scripted([body], sent), max_options=max_options)
    response = play(player, TurnRequest(PROMPT, {"e1f1": "Kf1"}))
    assert response.answer == TurnAnswer(action="move", move="e1f1", offer_draw=True)
    assert set(sent[0]) == {"action", "offer_draw"}


def test_decision_split_sums_time_usage_and_raw_text(models, monkeypatch):
    clock = iter([0.0, 1.5, 10.0, 10.25])
    # Only the decision module's clock: httpx reads the global one.
    monkeypatch.setattr(decision, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    bodies = [
        {
            "action": "move",
            "move": "other",
            "offer_draw": "no",
            "usage": {"prompt_tokens": 900, "completion_tokens": 3, "reasoning_tokens": 7},
        },
        {"move": "f1f6", "usage": {"input_tokens": 800, "output_tokens": 2}},
    ]
    player = decision_player(models, scripted(bodies, []), max_options=26)
    response = play(player, many_request())
    assert response.answer == TurnAnswer(action="move", move="f1f6")
    assert response.elapsed_s == 1.75
    assert response.usage == Usage(1700, 5, None)
    assert [json.loads(line) for line in response.raw.splitlines()] == bodies


def test_decision_split_usage_is_unknown_when_a_query_reports_none(models):
    usage = {"input_tokens": 9, "output_tokens": 1}
    first = {"action": "move", "move": "other", "offer_draw": "no", "usage": usage}
    bodies = [first, {"move": "f1f6"}]
    response = play(decision_player(models, scripted(bodies, []), max_options=26), many_request())
    assert response.usage is None


def test_decision_other_in_the_last_group_is_a_format_error(models):
    bodies = [{"action": "move", "move": "other", "offer_draw": "no"}, {"move": "other"}]
    response = play(decision_player(models, scripted(bodies, []), max_options=26), many_request())
    assert response.answer is None
    assert "invalid answer to 'move': 'other'" in response.format_error


def test_decision_server_error_on_a_later_query_fails_loudly(models):
    replies = iter(
        [
            httpx.Response(200, json={"action": "move", "move": "other", "offer_draw": "no"}),
            httpx.Response(500, text="a question has 2 to 26 options"),
        ]
    )
    player = decision_player(models, lambda request: next(replies), max_options=26)
    with pytest.raises(TransportError, match="HTTP 500: a question has 2 to 26 options"):
        play(player, many_request())


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
