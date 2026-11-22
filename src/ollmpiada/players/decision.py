"""Decision player: a SystemOne endpoint answering choice questions about the prompt."""

import logging
import math
import time
from collections.abc import Collection, Mapping

import httpx

from ollmpiada.config import Model, ModelSpec
from ollmpiada.players.base import (
    PermanentError,
    TransportError,
    TurnAnswer,
    TurnRequest,
    TurnResponse,
    Usage,
)

log = logging.getLogger(__name__)

WRAPPERS = ("answers", "results", "result", "output", "data", "predictions")
CHOICE_FIELDS = ("choice", "value", "answer", "selected", "result")
PIECE = "piece"


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
        """Asks one query; moves past `max_options` are asked per piece within it."""
        groups = _piece_groups(request, self._model.spec)
        questions = _questions(request, groups)
        raw, body, elapsed_s = await self._ask(request.prompt.text, questions)
        answer, format_error = None, None
        try:
            answer = _decide(_answers(body, set(questions)), questions, request, groups)
        except ValueError as e:
            format_error = str(e)
        return TurnResponse(raw, answer, elapsed_s, _usage(body), format_error)

    async def _ask(self, state: str, questions: dict[str, dict]) -> tuple[str, object, float]:
        """The reply's raw text, its JSON body (None when it is not JSON) and the time taken.

        Raises:
            TransportError: The call failed or the server reported an error.
        """
        payload = {"model": self._model.spec.model, "state": state, "questions": questions}
        headers = dict(self._model.provider.headers)
        if self._model.token:
            headers["Authorization"] = f"Bearer {self._model.token}"
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            start = time.perf_counter()
            try:
                response = await client.post(
                    self._model.provider.url, json=payload, headers=headers
                )
            except httpx.HTTPError as e:
                raise TransportError(f"{type(e).__name__}: {e}") from e
            elapsed_s = time.perf_counter() - start
        if not response.is_success:
            # Includes a server refusing a question with too many options: fail loudly.
            raise TransportError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping) and body.get("error"):
            raise TransportError(f"server error: {str(body['error'])[:300]}")
        return response.text, body, elapsed_s


def _piece_groups(request: TurnRequest, spec: ModelSpec) -> dict[str, dict[str, str]]:
    """The options of the piece question, `{key: {id: label}}`; empty when every legal
    move fits one move question.

    A piece with more moves than `max_options` becomes several groups of near-equal size.
    More legal moves than `max_options` always give at least two groups.

    Raises:
        PermanentError: More groups than `max_options`; the piece question cannot be asked.
    """
    limit = spec.max_options
    count = len(request.legal_moves)
    if limit is None or count <= limit:
        return {}
    groups: dict[str, dict[str, str]] = {}
    split = 0
    for key, moves in (request.move_groups or {"all": request.legal_moves}).items():
        chunks = _chunks(moves, limit)
        if len(chunks) == 1:
            groups[key] = moves
        else:
            split += 1
            groups.update({f"{key}_{i}": chunk for i, chunk in enumerate(chunks, 1)})
    if len(groups) > limit:
        raise PermanentError(
            f"{len(groups)} pieces to choose from exceed max_options {limit} of {spec.id!r}"
        )
    log.warning(
        "%s: %d legal moves exceed max_options %d; asking for the piece first in %d groups;"
        " pieces split further: %d",
        spec.id,
        count,
        limit,
        len(groups),
        split,
    )
    return groups


def _chunks(moves: dict[str, str], limit: int) -> list[dict[str, str]]:
    """`moves` in the fewest chunks of at most `limit`, sizes differing by at most one."""
    items = list(moves.items())
    count = math.ceil(len(items) / limit)
    size, larger = divmod(len(items), count)
    chunks, start = [], 0
    for i in range(count):
        end = start + size + (i < larger)
        chunks.append(dict(items[start:end]))
        start = end
    return chunks


def _questions(request: TurnRequest, groups: dict[str, dict[str, str]]) -> dict[str, dict]:
    """`action` and `offer_draw` (not in a move-only turn), then either one `move` question
    or the piece question with one move question per group; a single move needs no question
    of its own, since a one-option question tells nothing and SystemOne templates refuse it.
    A move-only turn always has several legal moves: the game master plays a single one itself
    (docs/game-master/turns.md#opening)."""
    questions = _turn_questions(request)
    if not groups:
        if len(request.legal_moves) > 1:
            questions["move"] = _move_question(request.legal_moves)
        return questions
    pieces = {key: ", ".join(moves.values()) for key, moves in groups.items()}
    questions[PIECE] = _choice("Which piece do you move? Each option lists its moves.", pieces)
    for key, moves in groups.items():
        if len(moves) > 1:
            questions[_move_name(key)] = _move_question(moves)
    return questions


def _decide(
    answers: Mapping,
    questions: dict[str, dict],
    request: TurnRequest,
    groups: dict[str, dict[str, str]],
) -> TurnAnswer:
    """Raises ValueError for an answer that does not match the questions asked."""
    offer_draw = False
    if not request.move_only:
        action = _selected(answers, "action", questions["action"]["criteria"])
        if action != "move":
            return TurnAnswer(action=action)
        offer_draw = _selected(answers, "offer_draw", questions["offer_draw"]["criteria"]) == "yes"
    if groups:
        piece = _selected(answers, PIECE, groups)
        name, moves = _move_name(piece), groups[piece]
    else:
        name, moves = "move", request.legal_moves
    # The answers to the move questions of other pieces are ignored.
    move = next(iter(moves)) if len(moves) == 1 else _selected(answers, name, moves)
    return TurnAnswer(action="move", move=move, offer_draw=offer_draw)


def _move_name(piece: str) -> str:
    return f"move_{piece}"


def _turn_questions(request: TurnRequest) -> dict[str, dict]:
    if request.move_only:
        return {}
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


def _selected(answers: Mapping, question: str, allowed: Collection[str]) -> str:
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
    if not isinstance(input_tokens, int):
        return None
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    if output_tokens is None and isinstance(total_tokens, int):
        # SystemOne reports only prompt and total: a decision model scores options
        # and generates no text, so the difference is a measured output count.
        output_tokens = total_tokens - input_tokens
    if not isinstance(output_tokens, int) or output_tokens < 0:
        return None
    reasoning_tokens = usage.get("reasoning_tokens")
    if not isinstance(reasoning_tokens, int):
        reasoning_tokens = None
    return Usage(input_tokens, output_tokens, reasoning_tokens)
