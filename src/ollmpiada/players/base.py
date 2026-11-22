"""What a player receives each turn and what it returns."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from ollmpiada.games.base import Prompt

Action = Literal["move", "resign", "accept_draw"]


class TurnAnswer(BaseModel):
    """A player's decision for one turn; the game master validates it."""

    action: Action
    move: str | None = None
    offer_draw: bool = False


@dataclass(frozen=True)
class TurnRequest:
    prompt: Prompt
    legal_moves: dict[str, str]
    draw_offered: bool = False
    move_groups: dict[str, dict[str, str]] = field(default_factory=dict)
    """The legal moves as the game groups them (`Game.move_groups`); empty means one group."""
    move_only: bool = False
    """Only a move is allowed (the opening, `PromptContext.move_only`): the answer format offers
    no resignation and no draw offer."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    """None when the server does not report it; never guessed as zero."""


@dataclass(frozen=True)
class TurnResponse:
    """One model call. `answer` is None when the reply did not match the answer format."""

    raw: str
    answer: TurnAnswer | None
    elapsed_s: float
    usage: Usage | None
    format_error: str | None = None


class TransportError(Exception):
    """The model could not be reached or failed to answer; not the model's fault."""


class PermanentError(TransportError):
    """A player failure that depends only on the request and the configuration, so a retry
    cannot fix it."""


class Player(Protocol):
    name: str

    async def play(self, request: TurnRequest) -> TurnResponse: ...
