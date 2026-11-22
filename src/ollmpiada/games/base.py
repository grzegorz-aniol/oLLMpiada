"""The game interface the game master, players and UI work against."""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from string import Template

from rich.console import RenderableType

from ollmpiada.config import Settings

ADJUDICATION = "adjudication"
OPENING_MOVES = 5
"""Moves per side at the start of a game in which a player can only move: no resignation and
no draw offer (docs/game-master/turns.md#opening). Only this module reads it, at call time, so
tests can turn the opening off by patching it here."""


def first_full_move() -> int:
    """A side's first move after the opening, the first one in which every answer is allowed."""
    return OPENING_MOVES + 1


def in_opening(turn: int) -> bool:
    """A side's `turn` (its move number) falls in the opening, where it can only move."""
    return turn <= OPENING_MOVES


def opening_notice() -> str:
    """When resigning and draw offers become allowed, as the prompt and a rejection say it."""
    return f"resigning and draw offers start with your move {first_full_move()}"


class Side(StrEnum):
    FIRST = "first"
    SECOND = "second"

    @property
    def opponent(self) -> Side:
        return Side.SECOND if self is Side.FIRST else Side.FIRST


@dataclass(frozen=True)
class Outcome:
    """How a game ended: a winner, or a draw when `winner` is None."""

    winner: Side | None
    reason: str
    score: str | None = None
    """The evaluation an adjudicated game was judged by; None for a game decided on the board."""

    @property
    def adjudicated(self) -> bool:
        return self.reason == ADJUDICATION


class IllegalMove(ValueError):
    """A model's move text is not a legal move; the message is the reason shown to it."""


class GameSetupError(Exception):
    """The game cannot start what it needs to referee; the message says how to fix it."""


class AdjudicationError(Exception):
    """The game cannot judge a position at the turn limit; the game master aborts the game."""


@dataclass(frozen=True)
class PromptContext:
    """Everything the shared prompt says about one turn."""

    side: Side
    position: str
    legal_moves: list[str]
    turn: int
    max_turns: int
    draw_offered: bool = False
    rejection: str | None = None

    @property
    def move_only(self) -> bool:
        """The turn is in the opening, where a move is the only answer allowed."""
        return in_opening(self.turn)


@dataclass(frozen=True)
class Prompt:
    """The shared prompt: game-wide rules (`system`) and the turn at hand (`user`)."""

    system: str
    user: str

    @property
    def text(self) -> str:
        """Both parts as one text, for players that take a single input."""
        return f"{self.system}\n{self.user}"


class Game[S](ABC):
    """A two-player, turn-based, perfect-information game. States are never mutated."""

    name: str
    side_names: tuple[str, str]
    system_template: str
    user_template: str
    record_extension: str

    @abstractmethod
    def new_state(self) -> S: ...

    @abstractmethod
    def side_to_move(self, state: S) -> Side: ...

    @abstractmethod
    def legal_moves(self, state: S) -> dict[str, str]:
        """Legal moves as `{id: label}`."""

    def move_groups(self, state: S) -> dict[str, dict[str, str]]:
        """The legal moves grouped as `{group key: {id: label}}`, one group per piece where
        the game has pieces; keys are identifiers (letters, digits, `_`). Empty when the
        game does not group its moves: players then treat them as one group."""
        return {}

    @abstractmethod
    def parse_move(self, state: S, text: str) -> str:
        """Turn a model's move text into a legal move id.

        Raises:
            IllegalMove: The text names no legal move.
        """

    @abstractmethod
    def apply(self, state: S, move_id: str) -> S: ...

    @abstractmethod
    def outcome(self, state: S) -> Outcome | None:
        """The result once the rules end the game, else None."""

    @abstractmethod
    def adjudicate(self, state: S) -> Outcome:
        """The result of a game still running when both sides reached the turn limit, with
        reason `ADJUDICATION` and the `score` it was judged by.

        Raises:
            AdjudicationError: What judges the position failed (for chess the engine).
        """

    @contextmanager
    def session(self, settings: Settings) -> Iterator[None]:
        """Hold what `adjudicate` needs (for chess the engine) for one competition.

        Raises:
            GameSetupError: It cannot be started.
        """
        yield

    @abstractmethod
    def describe(self, state: S) -> str:
        """Position text for the prompt."""

    @abstractmethod
    def render(self, state: S) -> RenderableType:
        """The board for the terminal UI."""

    @abstractmethod
    def record(self, state: S, outcome: Outcome | None, players: tuple[str, str]) -> str:
        """The game's native record; `players` are named in side order."""

    def side_name(self, side: Side) -> str:
        return self.side_names[0 if side is Side.FIRST else 1]

    def context(
        self,
        state: S,
        *,
        turn: int,
        max_turns: int,
        draw_offered: bool = False,
        rejection: str | None = None,
    ) -> PromptContext:
        """The prompt context for the side to move in `state`."""
        return PromptContext(
            side=self.side_to_move(state),
            position=self.describe(state),
            legal_moves=list(self.legal_moves(state).values()),
            turn=turn,
            max_turns=max_turns,
            draw_offered=draw_offered,
            rejection=rejection,
        )

    def prompt(self, ctx: PromptContext) -> Prompt:
        """Render the shared prompt; players add their own answer format."""
        values = dict(
            side=self.side_name(ctx.side),
            opponent=self.side_name(ctx.side.opponent),
            position=ctx.position,
            legal_moves=", ".join(ctx.legal_moves),
            turn=ctx.turn,
            max_turns=ctx.max_turns,
            turns_left=ctx.max_turns - ctx.turn + 1,
            opening_moves=OPENING_MOVES,
            choose_move=(
                f"Choose exactly one move from this list; {opening_notice()}."
                if ctx.move_only
                else "Unless you resign or accept a draw, choose exactly one move from this list."
            ),
            draw_offer=(
                f"{self.side_name(ctx.side.opponent)} offers a draw. Answer accept_draw to "
                "accept it, or make a move to decline it."
                if ctx.draw_offered
                else "No draw offer is pending."
            ),
            previous_attempt=(
                f"PREVIOUS ATTEMPT\n\nYour previous answer this turn was rejected: "
                f"{ctx.rejection}\nAnswer again with a legal move."
                if ctx.rejection
                else ""
            ),
        )
        return Prompt(_render(self.system_template, values), _render(self.user_template, values))


def _render(template: str, values: Mapping[str, object]) -> str:
    text = Template(template).substitute(values)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
