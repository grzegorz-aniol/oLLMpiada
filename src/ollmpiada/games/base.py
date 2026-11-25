"""The game interface the game master, players and UI work against."""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from string import Template

from rich.console import RenderableType

from ollmpiada.config import Settings

ADJUDICATION = "adjudication"
EVENT = "oLLMpiada"
"""The event every game record names."""
SAMPLE_SEED = 0
"""The start of every call made outside a game: the warm-up, `ollmpiada probe` and
`prompt_version` (docs/game-master/turns.md#turn-loop)."""
# Whitespace (any Unicode kind) and characters no move contains, so answers wrapped in
# markdown, quotes or punctuation still read as the move inside.
EDGE_CLASS = r"[\s*`\"'“”‘’()!?.,;…]"
EDGE = re.compile(rf"^{EDGE_CLASS}+|{EDGE_CLASS}+$")
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


def strip_answer(text: str) -> str:
    """`**e4**.` -> `e4`: a move text without the wrappers and punctuation around it, the first
    step of every game's `parse_move`."""
    return EDGE.sub("", text)


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
    points: tuple[int, int] | None = None
    """What each side scored, in side order (for card play the tricks it took); None for a game
    without points, such as chess."""

    @property
    def adjudicated(self) -> bool:
        return self.reason == ADJUDICATION


class Rejection(StrEnum):
    """A referee rule an answer breaks; the game words it (`Game.rejection`)."""

    MOVE_ONLY = "move_only"
    """A resignation or draw offer in a move-only turn."""
    NO_DRAW_OFFER = "no_draw_offer"
    """A draw accepted while no offer is pending."""
    NO_MOVE = "no_move"
    """The action `move` without a move."""


class IllegalMove(ValueError):
    """A model's move text is not a legal move; the message is the reason shown to it."""


class GameSetupError(Exception):
    """The game cannot start what it needs to referee; the message says how to fix it."""


class AdjudicationError(Exception):
    """The game cannot judge a position at the turn limit; the game master aborts the game."""


@dataclass(frozen=True)
class PromptContext:
    """Everything a game's prompt says about one turn."""

    side: Side
    position: str
    legal_moves: list[str]
    turn: int
    max_turns: int | None
    """None for a game without a turn limit (`Game.turn_limit`)."""
    move_only: bool
    """A move is the only answer allowed in this turn (`Game.move_only`)."""
    draw_offered: bool = False
    rejection: str | None = None


@dataclass(frozen=True)
class Questions:
    """What a decision player asks its model, in the game's words; each field words the question
    of the same name (docs/game-master/prompts.md#answer-formats)."""

    move: str
    """Picks the move from the legal moves, or from those of the chosen group."""
    piece: str
    """Picks a group of moves first, when the legal moves exceed a model's `max_options`."""
    action: str = ""
    """Picks the action in a turn that is not move-only. Empty, like `offer_draw` and
    `options`, in a game whose every turn is move-only."""
    offer_draw: str = ""
    """Asks whether to offer a draw together with the move."""
    options: Mapping[str, str] = field(default_factory=dict)
    """What each fixed option means: the actions (`move`, `resign`, `accept_draw`) and the draw
    offer's `yes` and `no`."""


@dataclass(frozen=True)
class Prompt:
    """Every word a player shows its model for one turn, all from the game: the game-wide rules
    (`system`), the turn at hand (`user`), the LLM answer format and the decision questions."""

    system: str
    user: str
    answer_format: str
    """The LLM answer-format section, which an LLM player puts between the two parts."""
    questions: Questions

    @property
    def text(self) -> str:
        """Both parts as one text, for players that take a single input."""
        return f"{self.system}\n{self.user}"


class Game[S](ABC):
    """A two-player, turn-based game. States are never mutated."""

    name: str
    side_names: tuple[str, str]
    system_template: str
    user_template: str
    record_extension: str
    questions: Questions
    turn_limit: bool = True
    """The game is adjudicated (`adjudicate`) once both sides made `max_turns` moves; a game
    without a limit ends only by its rules or a forfeit."""
    duplicate: bool = False
    """The game compares the players over duplicate pairs, each start played once from each
    side, and its prompt says so; a competition then plays an even number of games."""

    @abstractmethod
    def new_state(self, seed: int) -> S:
        """The starting state; a game with random starts (a card deal) derives it from `seed`
        alone, so one seed always gives the same start."""

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

    def adjudicate(self, state: S) -> Outcome:
        """The result of a game still running when both sides reached the turn limit, with
        reason `ADJUDICATION` and the `score` it was judged by.

        Raises:
            AdjudicationError: What judges the position failed (for chess the engine); the
                base class always raises it, since it has nothing to judge by.
        """
        raise AdjudicationError(f"{self.name} has no way to adjudicate a game")

    def move_only(self, turn: int) -> bool:
        """A side's `turn` (its move number) allows only a move: no resignation and no draw
        offer. The default is the opening (`in_opening`)."""
        return in_opening(turn)

    @abstractmethod
    def rejection(self, rule: Rejection) -> str:
        """The reason the game master gives a model whose answer breaks `rule`."""

    def forfeit(self, state: S, side: Side, reason: str) -> Outcome:
        """The result when `side` stops answering in `state`, by resignation or illegal
        answers; the default is a win for the opponent."""
        return Outcome(side.opponent, reason)

    def move_mark(self, state: S) -> str | None:
        """The label the move list shows before the move made in `state`, such as a move
        number; None for no label."""
        return None

    def progress(self, state: S, turn: int) -> str:
        """Where a game in `state` stands after `turn` moves, for the live view's status line.
        The default counts moves per side, the unit of `--max-turns`, so it suits a game whose
        sides alternate."""
        return f"move {turn // 2 + 1}"

    @contextmanager
    def session(self, settings: Settings) -> Iterator[None]:
        """Hold what `adjudicate` needs (for chess the engine) for one competition.

        Raises:
            GameSetupError: It cannot be started.
        """
        yield

    @abstractmethod
    def describe(self, state: S) -> str:
        """Position text for the prompt; in a game with hidden information only what the side
        to move may know."""

    @abstractmethod
    def render(self, state: S) -> RenderableType:
        """The board for the terminal UI."""

    @abstractmethod
    def record(
        self, state: S, outcome: Outcome | None, players: tuple[str, str], start: int
    ) -> str:
        """The game's native record; `players` are named in side order, and `start` numbers
        the game's start in its competition from 1, shared by a duplicate pair (the bridge
        board number)."""

    def side_name(self, side: Side) -> str:
        return self.side_names[0 if side is Side.FIRST else 1]

    def context(
        self,
        state: S,
        *,
        turn: int,
        max_turns: int | None,
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
            move_only=self.move_only(turn),
            draw_offered=draw_offered,
            rejection=rejection,
        )

    @abstractmethod
    def prompt_values(self, ctx: PromptContext) -> dict[str, object]:
        """Values for the placeholders only this game's templates use: every sentence worded per
        turn, the retry (`ctx.rejection`) included, since `string.Template` has no
        conditionals."""

    @abstractmethod
    def answer_format(self, ctx: PromptContext) -> str:
        """The LLM answer-format section of the turn: its heading and what to answer."""

    def prompt(self, ctx: PromptContext) -> Prompt:
        """Fill the game's templates with the values every game has and its `prompt_values`;
        every word comes from the game."""
        values = dict(
            side=self.side_name(ctx.side),
            opponent=self.side_name(ctx.side.opponent),
            position=ctx.position,
            legal_moves=", ".join(ctx.legal_moves),
            turn=ctx.turn,
            max_turns=ctx.max_turns,
            turns_left=None if ctx.max_turns is None else ctx.max_turns - ctx.turn + 1,
        )
        values |= self.prompt_values(ctx)
        return Prompt(
            _render(self.system_template, values),
            _render(self.user_template, values),
            self.answer_format(ctx),
            self.questions,
        )


def _render(template: str, values: Mapping[str, object]) -> str:
    text = Template(template).substitute(values)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
