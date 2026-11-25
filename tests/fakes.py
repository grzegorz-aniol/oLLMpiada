"""Fakes for tests: scripted players (with a fixed script, or picking from the legal moves),
the only players game master and competition tests use,
chess with a fixed engine score, so no test needs Stockfish, and a game of moves alone."""

import asyncio

import chess
from chess.engine import Score
from rich.text import Text

from ollmpiada.games.base import (
    Game,
    IllegalMove,
    Outcome,
    PromptContext,
    Questions,
    Rejection,
    Side,
)
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import TurnAnswer, TurnRequest, TurnResponse, Usage

USAGE = Usage(input_tokens=100, output_tokens=10, reasoning_tokens=0)
Step = str | TurnAnswer | TurnResponse | Exception
WARM_UP: Step = "e4"
"""A script's first step in a competition: the answer to the warm-up call, which is discarded."""


class ScriptedPlayer:
    """Answers from a prepared script, one step per call.

    A step is a move text, a `TurnAnswer`, a complete `TurnResponse` (for example
    one with a format error), or an exception to raise.
    """

    def __init__(self, name: str, script: list[Step], *, elapsed_s: float = 1.0) -> None:
        self.name = name
        self.script = list(script)
        self.elapsed_s = elapsed_s
        self.requests: list[TurnRequest] = []

    async def play(self, request: TurnRequest) -> TurnResponse:
        self.requests.append(request)
        await asyncio.sleep(0)  # yield like a real model call, so a stop can cancel it
        if not self.script:
            raise AssertionError(f"{self.name} was asked to play after its script ended")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, TurnResponse):
            return step
        answer = TurnAnswer(action="move", move=step) if isinstance(step, str) else step
        return TurnResponse(answer.model_dump_json(), answer, self.elapsed_s, USAGE)


class PickingPlayer(ScriptedPlayer):
    """Answers every turn, the warm-up included, with the legal move at `index` in the
    request's list, so it plays a whole game whatever the start; in bridge 0 is the highest
    card of the first suit listed and -1 the lowest of the last."""

    def __init__(self, name: str, index: int) -> None:
        super().__init__(name, [])
        self.index = index

    async def play(self, request: TurnRequest) -> TurnResponse:
        self.script.append(list(request.legal_moves.values())[self.index])
        return await super().play(request)


def unreadable(raw: str = "I think e4 is best") -> TurnResponse:
    """A reply that does not match the answer format."""
    return TurnResponse(raw, None, 1.0, USAGE, format_error="not a TurnAnswer")


class SeededStart(ChessGame):
    """Chess whose start gives the first move to Black for an odd seed, so a test sees which
    seed a game started from and can make the second side move first."""

    def new_state(self, seed: int) -> chess.Board:
        board = chess.Board()
        board.turn = chess.WHITE if seed % 2 == 0 else chess.BLACK
        return board


class Judged(ChessGame):
    """Chess whose engine is replaced by prepared scores from White's view, one per
    evaluated position."""

    def __init__(self, *scores: Score) -> None:
        super().__init__()
        self.scores = list(scores)
        self.evaluated: list[chess.Board] = []

    def evaluate(self, state: chess.Board) -> Score:
        self.evaluated.append(state)
        return self.scores[len(self.evaluated) - 1]


class MovesOnly(Game[tuple[str, ...]]):
    """A game of moves alone, like a card game: no turn limit, every turn move-only, and a
    move mark every third move, which counting move pairs cannot produce. Each side in turn
    plays `a` or `b`; after `LENGTH` moves the first side wins if `a` was played more often.
    It words its prompt in letters and records every forfeit it is asked for."""

    LENGTH = 6
    name = "moves-only"
    side_names = ("North", "East")
    system_template = "You play $side against $opponent."
    user_template = "$position\nLEGAL MOVES\n$legal_moves\n$previous_attempt"
    record_extension = "txt"
    questions = Questions(move="Which letter?", piece="Which letters?")
    turn_limit = False

    def __init__(self) -> None:
        self.forfeits: list[tuple[tuple[str, ...], Side, str]] = []

    def prompt_values(self, ctx: PromptContext) -> dict[str, object]:
        return {"previous_attempt": f"Rejected: {ctx.rejection}" if ctx.rejection else ""}

    def answer_format(self, ctx: PromptContext) -> str:
        return "Answer with a letter.\n"

    def rejection(self, rule: Rejection) -> str:
        return f"{rule}: play a letter"

    def move_only(self, turn: int) -> bool:
        return True

    def new_state(self, seed: int) -> tuple[str, ...]:
        return ()

    def side_to_move(self, state: tuple[str, ...]) -> Side:
        return Side.FIRST if len(state) % 2 == 0 else Side.SECOND

    def legal_moves(self, state: tuple[str, ...]) -> dict[str, str]:
        return {"a": "a", "b": "b"}

    def parse_move(self, state: tuple[str, ...], text: str) -> str:
        if text not in self.legal_moves(state):
            raise IllegalMove(f"{text!r} is not a or b")
        return text

    def apply(self, state: tuple[str, ...], move_id: str) -> tuple[str, ...]:
        return (*state, move_id)

    def outcome(self, state: tuple[str, ...]) -> Outcome | None:
        if len(state) < self.LENGTH:
            return None
        return Outcome(Side.FIRST if state.count("a") > state.count("b") else Side.SECOND, "count")

    def forfeit(self, state: tuple[str, ...], side: Side, reason: str) -> Outcome:
        self.forfeits.append((state, side, reason))
        return super().forfeit(state, side, reason)

    def move_mark(self, state: tuple[str, ...]) -> str | None:
        return f"[{len(state)}]" if len(state) % 3 == 0 else None

    def describe(self, state: tuple[str, ...]) -> str:
        return f"Played: {' '.join(state) or 'nothing'}"

    def render(self, state: tuple[str, ...]) -> Text:
        return Text(f"board: {' '.join(state)}")

    def record(
        self, state: tuple[str, ...], outcome: Outcome | None, players: tuple[str, str], start: int
    ) -> str:
        return f"start {start}: {' '.join(state)}"
