"""Scripted fake players: the only players game master and competition tests use."""

import asyncio

from ollmpiada.players.base import TurnAnswer, TurnRequest, TurnResponse, Usage

USAGE = Usage(input_tokens=100, output_tokens=10, reasoning_tokens=0)
Step = str | TurnAnswer | TurnResponse | Exception


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


def unreadable(raw: str = "I think e4 is best") -> TurnResponse:
    """A reply that does not match the answer format."""
    return TurnResponse(raw, None, 1.0, USAGE, format_error="not a TurnAnswer")
