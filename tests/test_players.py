import asyncio
import dataclasses
import json
import logging
import math
import random

import httpx
import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from ollmpiada.config import load_models
from ollmpiada.games.base import Prompt
from ollmpiada.players import PlayerError, create_player, resolve_player
from ollmpiada.players.base import PermanentError, TransportError, TurnAnswer, TurnRequest, Usage
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


# --- decision player: moves past max_options asked per piece ---

QUEEN = {f"d1{square}": f"Q{square}" for square in ("d2", "d3", "d4", "d5", "d6", "d7", "d8")}
KNIGHT = {"g1f3": "Nf3", "g1h3": "Nh3"}
KING = {"e1f1": "Kf1"}
GROUPS = {"queen_d1": QUEEN, "knight_g1": KNIGHT, "king_e1": KING}
GROUPED = TurnRequest(PROMPT, {**QUEEN, **KNIGHT, **KING}, move_groups=GROUPS)


def scripted(body, sent):
    """Answers with `body` and records the questions asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["questions"])
        return httpx.Response(200, json=body)

    return handler


def test_decision_asks_the_piece_and_each_pieces_moves_in_one_query(models):
    sent = []
    body = {
        "action": "move",
        "offer_draw": "yes",
        "piece": "knight_g1",
        "move_knight_g1": "g1f3",
        "move_queen_d1": "d1d8",
    }
    response = play(decision_player(models, scripted(body, sent), max_options=8), GROUPED)
    # The chosen piece's move is played; the other piece's answer is ignored.
    assert response.answer == TurnAnswer(action="move", move="g1f3", offer_draw=True)

    (questions,) = sent
    assert set(questions) == {"action", "offer_draw", "piece", "move_queen_d1", "move_knight_g1"}
    assert questions["piece"]["criteria"] == {
        "queen_d1": "Qd2, Qd3, Qd4, Qd5, Qd6, Qd7, Qd8",
        "knight_g1": "Nf3, Nh3",
        "king_e1": "Kf1",
    }
    assert questions["move_queen_d1"]["criteria"] == QUEEN
    assert questions["move_knight_g1"]["criteria"] == KNIGHT
    assert "other" not in json.dumps(questions)


def test_decision_piece_with_a_single_move_plays_it_without_a_move_question(models):
    sent = []
    body = {"action": "move", "offer_draw": "no", "piece": "king_e1"}
    response = play(decision_player(models, scripted(body, sent), max_options=8), GROUPED)
    assert response.answer == TurnAnswer(action="move", move="e1f1")
    assert "move_king_e1" not in sent[0]


def test_decision_splits_a_piece_with_more_moves_than_max_options(models):
    sent = []
    body = {"action": "move", "offer_draw": "no", "piece": "queen_d1_2", "move_queen_d1_2": "d1d7"}
    response = play(decision_player(models, scripted(body, sent), max_options=4), GROUPED)
    assert response.answer == TurnAnswer(action="move", move="d1d7")
    questions = sent[0]
    pieces = ["queen_d1_1", "queen_d1_2", "knight_g1", "king_e1"]
    assert list(questions["piece"]["criteria"]) == pieces
    assert list(questions["move_queen_d1_1"]["criteria"]) == ["d1d2", "d1d3", "d1d4", "d1d5"]
    assert list(questions["move_queen_d1_2"]["criteria"]) == ["d1d6", "d1d7", "d1d8"]
    assert "move_queen_d1" not in questions


def test_decision_moves_that_fit_are_one_move_question_without_a_piece_question(models, caplog):
    sent = []
    body = {"action": "move", "offer_draw": "no", "move": "e1f1"}
    with caplog.at_level(logging.WARNING):
        response = play(decision_player(models, scripted(body, sent), max_options=10), GROUPED)
    assert response.answer == TurnAnswer(action="move", move="e1f1")
    assert set(sent[0]) == {"action", "offer_draw", "move"}
    assert sent[0]["move"]["criteria"] == GROUPED.legal_moves
    assert not caplog.records


def test_decision_ungrouped_moves_past_max_options_are_split_into_groups(models):
    sent = []
    request = TurnRequest(PROMPT, GROUPED.legal_moves)
    body = {"action": "move", "offer_draw": "no", "piece": "all_2", "move_all_2": "g1h3"}
    response = play(decision_player(models, scripted(body, sent), max_options=8), request)
    assert response.answer == TurnAnswer(action="move", move="g1h3")
    assert list(sent[0]["piece"]["criteria"]) == ["all_1", "all_2"]
    assert len(sent[0]["move_all_1"]["criteria"]) == len(sent[0]["move_all_2"]["criteria"]) == 5


def test_decision_splitting_logs_a_warning(models, caplog):
    body = {"action": "resign"}
    with caplog.at_level(logging.WARNING, logger="ollmpiada.players.decision"):
        play(decision_player(models, scripted(body, []), max_options=4), GROUPED)
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "basal-mini: 10 legal moves exceed max_options 4; asking for the piece first in 4 groups;"
        " pieces split further: 1"
    )


def test_decision_resign_needs_no_piece_or_move_answer(models):
    sent = []
    player = decision_player(models, scripted({"action": "resign"}, sent), max_options=8)
    assert play(player, GROUPED).answer == TurnAnswer(action="resign")
    assert len(sent) == 1


@pytest.mark.parametrize("move_groups", [None, {"king_e1": KING}], ids=["ungrouped", "grouped"])
@pytest.mark.parametrize("max_options", [None, 26])
def test_decision_single_legal_move_is_played_without_a_move_question(
    models, max_options, move_groups
):
    sent = []
    body = {"action": "move", "offer_draw": "yes"}
    player = decision_player(models, scripted(body, sent), max_options=max_options)
    response = play(player, TurnRequest(PROMPT, KING, move_groups=move_groups))
    assert response.answer == TurnAnswer(action="move", move="e1f1", offer_draw=True)
    assert set(sent[0]) == {"action", "offer_draw"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"piece": "knight_g1", "move_knight_g1": "d1d2"}, "invalid answer to 'move_knight_g1'"),
        ({"piece": "knight_g1"}, "invalid answer to 'move_knight_g1': None"),
        ({"piece": "rook_a1", "move_knight_g1": "g1f3"}, "invalid answer to 'piece'"),
        ({"move_knight_g1": "g1f3"}, "invalid answer to 'piece': None"),
    ],
    ids=["move-of-another-piece", "no-move", "unknown-piece", "no-piece"],
)
def test_decision_bad_piece_or_move_answers_are_format_errors(models, body, error):
    body = {"action": "move", "offer_draw": "no", **body}
    response = play(decision_player(models, scripted(body, []), max_options=8), GROUPED)
    assert response.answer is None and error in response.format_error


def test_decision_more_pieces_than_max_options_fails_loudly_without_a_query(models):
    sent = []
    player = decision_player(models, scripted({}, sent), max_options=2)
    with pytest.raises(PermanentError, match="pieces to choose from exceed max_options 2"):
        play(player, GROUPED)
    assert not sent


def test_decision_server_error_on_a_grouped_query_fails_loudly(models):
    handler = lambda request: httpx.Response(500, text="a question has 2 to 26 options")  # noqa: E731
    player = decision_player(models, handler, max_options=8)
    with pytest.raises(TransportError, match="HTTP 500: a question has 2 to 26 options"):
        play(player, GROUPED)


@pytest.mark.parametrize("max_options", [2, 3, 7, 26])
def test_decision_every_question_fits_max_options_and_every_move_is_offered_once(
    models, max_options
):
    rng = random.Random(max_options)
    for _ in range(100):
        sizes = [rng.choice([1, 2, 3, max_options, max_options + 1, 2 * max_options + 3])
                 for _ in range(rng.randint(1, 16))]  # fmt: skip
        groups = {
            f"piece{p}": {f"p{p}m{m}": f"P{p}-{m}" for m in range(size)}
            for p, size in enumerate(sizes)
        }
        legal = {move: label for moves in groups.values() for move, label in moves.items()}
        by_label = {label: move for move, label in legal.items()}
        request = TurnRequest(PROMPT, legal, move_groups=groups)
        sent = []
        handler = scripted({"action": "resign"}, sent)
        player = decision_player(models, handler, max_options=max_options)
        options_needed = sum(math.ceil(size / max_options) for size in sizes)
        if len(legal) > max_options and options_needed > max_options:
            with pytest.raises(PermanentError):
                play(player, request)
            continue
        play(player, request)
        questions = sent[0]
        assert all(2 <= len(q["criteria"]) <= max_options for q in questions.values())
        if len(legal) <= max_options:
            assert "piece" not in questions
            continue
        # Each option's moves: its own move question, or the single move it names.
        offered = [
            list(questions[f"move_{key}"]["criteria"])
            if f"move_{key}" in questions
            else [by_label[description]]
            for key, description in questions["piece"]["criteria"].items()
        ]
        assert [move for moves in offered for move in moves] == list(legal)
        assert len(questions) == 3 + sum(len(moves) > 1 for moves in offered)
        for p, size in enumerate(sizes):
            parts = [len(m) for m in offered if m[0].startswith(f"p{p}m")]
            assert len(parts) == math.ceil(size / max_options)
            assert max(parts) - min(parts) <= 1


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
