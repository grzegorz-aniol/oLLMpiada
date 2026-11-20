"""What a player receives each turn and what it returns."""

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    """None when the server does not report it; never guessed as zero."""


@dataclass(frozen=True)
class TurnResponse:
    """A player's answer to one turn request, from one or more model calls.

    `answer` is None when the reply did not match the answer format.
    """

    raw: str
    answer: TurnAnswer | None
    elapsed_s: float
    usage: Usage | None
    format_error: str | None = None


class TransportError(Exception):
    """The model could not be reached or failed to answer; not the model's fault."""


class Player(Protocol):
    name: str

    async def play(self, request: TurnRequest) -> TurnResponse: ...
