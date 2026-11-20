"""Decision player: a SystemOne endpoint answering choice questions about the prompt."""

import math
import time
from collections.abc import Mapping

import httpx

from ollmpiada.config import Model
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, TurnResponse, Usage

WRAPPERS = ("answers", "results", "result", "output", "data", "predictions")
CHOICE_FIELDS = ("choice", "value", "answer", "selected", "result")
OTHER = "other"


class DecisionPlayer:
    def __init__(
        self,
        name: str,
        model: Model,
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._model = model
        self._timeout_s = timeout_s
        self._transport = transport

    async def play(self, request: TurnRequest) -> TurnResponse:
        """Asks one query, or several when the moves are split; returns them as one response."""
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            queries = _Queries(client, self._model, request.prompt.text)
            groups = _move_groups(request.legal_moves, self._model.spec.max_options)
            answer, format_error = None, None
            try:
                answer = await _decide(queries, request, groups)
            except ValueError as e:
                format_error = str(e)
        return queries.response(answer, format_error)


class _Queries:
    """The SystemOne calls of one turn, kept so their raw text, time and usage add up."""

    def __init__(self, client: httpx.AsyncClient, model: Model, state: str) -> None:
        self._client = client
        self._model = model
        self._state = state
        self._raws: list[str] = []
        self._usages: list[Usage | None] = []
        self._elapsed_s = 0.0

    async def ask(self, questions: dict[str, dict]) -> Mapping:
        """Sends one query and returns its answers.

        Raises:
            TransportError: The call failed or the server reported an error.
            ValueError: The reply does not match the answer format.
        """
        payload = {"model": self._model.spec.model, "state": self._state, "questions": questions}
        headers = {"Authorization": f"Bearer {self._model.token}"} if self._model.token else {}
        start = time.perf_counter()
        try:
            response = await self._client.post(
                self._model.provider.url, json=payload, headers=headers
            )
        except httpx.HTTPError as e:
            raise TransportError(f"{type(e).__name__}: {e}") from e
        self._elapsed_s += time.perf_counter() - start
        if not response.is_success:
            # Includes a server refusing a question with too many options: fail loudly.
            raise TransportError(f"HTTP {response.status_code}: {response.text[:300]}")
        self._raws.append(response.text)
        body = response.json()
        self._usages.append(_usage(body))
        if isinstance(body, Mapping) and body.get("error"):
            raise TransportError(f"server error: {str(body['error'])[:300]}")
        return _answers(body, set(questions))

    def response(self, answer: TurnAnswer | None, format_error: str | None) -> TurnResponse:
        return TurnResponse(
            raw="\n".join(self._raws),
            answer=answer,
            elapsed_s=self._elapsed_s,
            usage=_total(self._usages, calls=len(self._raws)),
            format_error=format_error,
        )


async def _decide(
    queries: _Queries, request: TurnRequest, groups: list[dict[str, str]]
) -> TurnAnswer:
    """Raises ValueError for an answer that does not match the questions asked."""
    asked = _turn_questions(request)
    if groups:
        asked["move"] = _move_question(groups[0])
    answers = await queries.ask(asked)
    action = _selected(answers, "action", asked["action"]["criteria"])
    if action != "move":
        return TurnAnswer(action=action)
    offer_draw = _selected(answers, "offer_draw", asked["offer_draw"]["criteria"]) == "yes"
    move = next(iter(request.legal_moves))  # the only legal move when no group is asked
    for i, group in enumerate(groups):
        if i > 0:
            answers = await queries.ask({"move": _move_question(group)})
        move = _selected(answers, "move", group)
        if move != OTHER:
            break
    return TurnAnswer(action="move", move=move, offer_draw=offer_draw)


def _move_groups(legal_moves: Mapping[str, str], max_options: int | None) -> list[dict[str, str]]:
    """Criteria of the move questions, asked in order until one is not answered `other`.

    One group when the moves fit `max_options`, none when a single move is legal (a
    one-option question is meaningless and SystemOne templates refuse it).

    Raises:
        ValueError: The moves need splitting and one of their ids is `other`.
    """
    count = len(legal_moves)
    if count == 1:
        return []
    if max_options is None or count <= max_options:
        return [dict(legal_moves)]
    if OTHER in legal_moves:
        raise ValueError(f"a legal move id is {OTHER!r}, the option reserved for split moves")
    # Every group but the last spends one option on `other`.
    groups = 1 + math.ceil((count - max_options) / (max_options - 1))
    small, larger = divmod(count, groups)
    # Sorted ids keep moves sharing a prefix adjacent (in chess, UCI, one origin
    # square); a group boundary may still split them, since equal sizes win.
    ids = sorted(legal_moves)
    # The larger groups go last, where no `other` takes an option.
    sizes = [small] * (groups - larger) + [small + 1] * larger
    criteria, start = [], 0
    for i, size in enumerate(sizes):
        choices = {move: legal_moves[move] for move in ids[start : start + size]}
        start += size
        if i < groups - 1:
            unlisted = [legal_moves[move] for move in ids if move not in choices]
            choices[OTHER] = "A move not listed in this question: " + ", ".join(unlisted)
        criteria.append(choices)
    return criteria


def _turn_questions(request: TurnRequest) -> dict[str, dict]:
    actions = {"move": "Make a move", "resign": "Resign the game"}
    if request.draw_offered:
        actions["accept_draw"] = "Accept the opponent's draw offer"
    return {
        "action": _choice("What do you do this turn?", actions),
        "offer_draw": _choice(
            "Do you offer a draw together with your move?",
            {"yes": "Offer a draw", "no": "Do not offer a draw"},
        ),
    }


def _move_question(criteria: Mapping[str, str]) -> dict:
    return _choice("Which move do you play?", criteria)


def _choice(instructions: str, criteria: Mapping[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def _answers(body: object, names: set[str]) -> Mapping:
    for _ in range(4):
        if not isinstance(body, Mapping):
            break
        if names & body.keys():
            return body
        wrapper = next((w for w in WRAPPERS if w in body), None)
        if wrapper is None:
            break
        body = body[wrapper]
    raise ValueError("the response has no answers to the questions")


def _selected(answers: Mapping, question: str, allowed: Mapping[str, str]) -> str:
    value = answers.get(question)
    for _ in range(3):
        if not isinstance(value, Mapping):
            break
        field = next((f for f in CHOICE_FIELDS if f in value), None)
        if field is None:
            break
        value = value[field]
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid answer to {question!r}: {value!r}")
    return value


def _usage(body: object) -> Usage | None:
    usage = body.get("usage") if isinstance(body, Mapping) else None
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    reasoning_tokens = usage.get("reasoning_tokens")
    if not isinstance(reasoning_tokens, int):
        reasoning_tokens = None
    return Usage(input_tokens, output_tokens, reasoning_tokens)


def _total(usages: list[Usage | None], calls: int) -> Usage | None:
    """The sum of every call's usage; unknown when any call reported none."""
    known = [u for u in usages if u is not None]
    if len(known) < calls:
        return None
    reasoning = [u.reasoning_tokens for u in known]
    return Usage(
        sum(u.input_tokens for u in known),
        sum(u.output_tokens for u in known),
        None if None in reasoning else sum(r for r in reasoning if r is not None),
    )
