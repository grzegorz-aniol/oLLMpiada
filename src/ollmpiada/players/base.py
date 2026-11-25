"""What a player receives each turn and what it returns."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ollmpiada.games.base import Prompt

Action = Literal["move", "resign", "accept_draw"]

# These constants and `TurnAnswer` are the JSON schema an LLM reads in every output mode, so they
# address the model and name no code; every instruction sits in a field description, since
# Anthropic's native output drops the class one (docs/game-master/prompts.md#answer-formats).
ANSWER_TITLE = "answer"
MOVE_DESCRIPTION = (
    "Exactly one entry from the legal list in the message (the list of everything you may "
    "play this turn), copied unchanged, character for character: no other notation, no "
    "numbering, no added or removed symbols or spaces, no quotes, no explanation, no Markdown "
    "and no other text."
)


class TurnAnswer(BaseModel):
    """Your answer for this turn: play, resign or accept a draw offer, as the fields say."""

    model_config = ConfigDict(title=ANSWER_TITLE)

    action: Action = Field(
        description=(
            'What you do this turn, exactly one of "move", "resign" or "accept_draw". '
            '"move": you play the entry given in move. '
            '"resign": you give up and lose the game; move is null. '
            '"accept_draw": you accept the draw your opponent offered, and the game ends in a '
            "draw; only allowed while the message says a draw offer is pending; move is null."
        )
    )
    move: str | None = Field(
        default=None,
        description=f'Required when action is "move", otherwise null. {MOVE_DESCRIPTION}',
    )
    offer_draw: bool = Field(
        default=False,
        description=(
            'true only together with action "move": you play the move and offer your opponent '
            "a draw, which they may accept on their turn. false in every other case."
        ),
    )


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
