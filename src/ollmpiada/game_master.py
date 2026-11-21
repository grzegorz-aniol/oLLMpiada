"""The referee: plays one game between two players and publishes what happens."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ollmpiada.games.base import AdjudicationError, Game, IllegalMove, Outcome, Side
from ollmpiada.players.base import (
    PermanentError,
    Player,
    TransportError,
    TurnAnswer,
    TurnRequest,
    TurnResponse,
    Usage,
)

log = logging.getLogger(__name__)

ATTEMPTS_PER_TURN = 3
TRANSPORT_BACKOFF_S = (2.0, 8.0)
RAW_SHOWN = 300


@dataclass(frozen=True)
class Attempt:
    """One answer to a turn request; `rejection` is None for the answer that was accepted."""

    raw: str
    elapsed_s: float
    usage: Usage | None
    rejection: str | None = None


@dataclass(frozen=True)
class Turn:
    side: Side
    player: str
    action: str
    move: str | None
    attempts: list[Attempt]


@dataclass
class GameResult:
    number: int
    players: tuple[str, str]
    """Player names in side order."""
    outcome: Outcome | None = None
    """None when the game was aborted."""
    error: str | None = None
    turns: list[Turn] = field(default_factory=list)
    state: object = None


@dataclass(frozen=True)
class GameStarted:
    number: int
    players: tuple[str, str]
    state: object


@dataclass(frozen=True)
class AnswerReceived:
    """Every answered turn request, published before the answer is validated."""

    number: int
    side: Side
    player: str
    elapsed_s: float
    usage: Usage | None


@dataclass(frozen=True)
class MoveMade:
    number: int
    side: Side
    player: str
    move: str
    state: object
    draw_offered: bool


@dataclass(frozen=True)
class AnswerRejected:
    number: int
    side: Side
    player: str
    raw: str
    reason: str
    attempt: int


@dataclass(frozen=True)
class GameFinished:
    result: GameResult


Event = GameStarted | AnswerReceived | MoveMade | AnswerRejected | GameFinished


def _ignore(event: Event) -> None:
    pass


async def play_game[S](
    game: Game[S],
    players: tuple[Player, Player],
    *,
    max_turns: int,
    number: int = 1,
    publish: Callable[[Event], None] = _ignore,
    backoff_s: tuple[float, ...] = TRANSPORT_BACKOFF_S,
) -> GameResult:
    """Play one game; `players` are in side order (first side moves first)."""
    names = (players[0].name, players[1].name)
    state = game.new_state()
    result = GameResult(number, names, state=state)
    publish(GameStarted(number, names, state))
    moves_made = {Side.FIRST: 0, Side.SECOND: 0}
    draw_offer_by: Side | None = None

    while result.outcome is None:
        side = game.side_to_move(state)
        player = players[0 if side is Side.FIRST else 1]
        draw_offered = draw_offer_by is side.opponent
        attempts: list[Attempt] = []
        accepted: tuple[TurnAnswer, str | None] | None = None
        rejection = None
        while accepted is None and len(attempts) < ATTEMPTS_PER_TURN:
            ctx = game.context(
                state,
                turn=moves_made[side] + 1,
                max_turns=max_turns,
                draw_offered=draw_offered,
                rejection=rejection,
            )
            request = TurnRequest(
                game.prompt(ctx), game.legal_moves(state), draw_offered, game.move_groups(state)
            )
            try:
                response = await _call(player, request, backoff_s)
            except TransportError as e:
                if attempts:
                    result.turns.append(Turn(side, player.name, "aborted", None, attempts))
                result.error = f"{player.name}: {e}"
                result.state = state
                publish(GameFinished(result))
                return result
            publish(AnswerReceived(number, side, player.name, response.elapsed_s, response.usage))
            try:
                accepted = _validate(game, state, response, draw_offered)
                attempts.append(Attempt(response.raw, response.elapsed_s, response.usage))
            except IllegalMove as e:
                reason = str(e)
                attempts.append(Attempt(response.raw, response.elapsed_s, response.usage, reason))
                rejection = f"you answered {_shorten(response.raw)!r}; {reason}"
                publish(
                    AnswerRejected(number, side, player.name, response.raw, reason, len(attempts))
                )

        if accepted is None:
            result.turns.append(Turn(side, player.name, "illegal", None, attempts))
            result.outcome = Outcome(side.opponent, "illegal answers")
            break
        answer, move_id = accepted
        if answer.action == "resign":
            result.turns.append(Turn(side, player.name, "resign", None, attempts))
            result.outcome = Outcome(side.opponent, "resignation")
            break
        if answer.action == "accept_draw":
            result.turns.append(Turn(side, player.name, "accept_draw", None, attempts))
            result.outcome = Outcome(None, "draw agreed")
            break

        assert move_id is not None
        label = game.legal_moves(state)[move_id]
        state = game.apply(state, move_id)
        moves_made[side] += 1
        draw_offer_by = side if answer.offer_draw else None
        result.turns.append(Turn(side, player.name, "move", label, attempts))
        publish(MoveMade(number, side, player.name, label, state, answer.offer_draw))
        result.outcome = game.outcome(state)
        if result.outcome is None and min(moves_made.values()) >= max_turns:
            try:
                # A worker thread keeps live timers and Ctrl+C responsive while the engine thinks.
                result.outcome = await asyncio.to_thread(game.adjudicate, state)
            except AdjudicationError as e:
                result.error = str(e)
                break

    result.state = state
    publish(GameFinished(result))
    return result


def opening_request[S](game: Game[S], *, max_turns: int) -> TurnRequest:
    """The first turn of a new game, for calls made outside a game."""
    state = game.new_state()
    prompt = game.prompt(game.context(state, turn=1, max_turns=max_turns))
    return TurnRequest(prompt, game.legal_moves(state), move_groups=game.move_groups(state))


async def warm_up[S](
    game: Game[S], player: Player, *, max_turns: int, backoff_s: tuple[float, ...]
) -> None:
    """Send the player one opening turn and discard the answer, so a server that loads models
    on demand has the model ready before the first game.

    Raises:
        TransportError: The player still failed after the transport retries.
    """
    await _call(player, opening_request(game, max_turns=max_turns), backoff_s)


async def _call(player: Player, request: TurnRequest, backoff_s: tuple[float, ...]) -> TurnResponse:
    """Call the player, retrying transport failures after each backoff delay; a
    `PermanentError` is raised at once."""
    for delay in backoff_s:
        try:
            return await player.play(request)
        except PermanentError:
            raise
        except TransportError as e:
            log.warning("%s: %s; retrying in %.0f s", player.name, e, delay)
            await asyncio.sleep(delay)
    return await player.play(request)


def _validate[S](
    game: Game[S], state: S, response: TurnResponse, draw_offered: bool
) -> tuple[TurnAnswer, str | None]:
    """The accepted answer and its move id.

    Raises:
        IllegalMove: The answer is rejected; the message is the reason.
    """
    answer = response.answer
    if answer is None:
        raise IllegalMove(f"the answer does not match the answer format: {response.format_error}")
    if answer.action == "accept_draw" and not draw_offered:
        raise IllegalMove("there is no draw offer to accept")
    if answer.action != "move":
        return answer, None
    if not answer.move:
        raise IllegalMove("the action is move but no move was given")
    return answer, game.parse_move(state, answer.move)


def _shorten(text: str) -> str:
    return text if len(text) <= RAW_SHOWN else text[:RAW_SHOWN] + "..."
