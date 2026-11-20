"""Decision player: a SystemOne endpoint answering choice questions about the prompt."""

import time
from collections.abc import Mapping

import httpx

from ollmpiada.config import Model
from ollmpiada.players.base import TransportError, TurnAnswer, TurnRequest, TurnResponse, Usage

WRAPPERS = ("answers", "results", "result", "output", "data", "predictions")
CHOICE_FIELDS = ("choice", "value", "answer", "selected", "result")


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
        payload = {
            "model": self._model.spec.model,
            "state": request.prompt.text,
            "questions": questions(request),
        }
        headers = {"Authorization": f"Bearer {self._model.token}"} if self._model.token else {}
        url = self._model.provider.url
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise TransportError(f"{type(e).__name__}: {e}") from e
        elapsed_s = time.perf_counter() - start
        if not response.is_success:
            # Includes a server refusing a question with too many options: fail loudly.
            raise TransportError(f"HTTP {response.status_code}: {response.text[:300]}")

        body, answer, format_error = None, None, None
        try:
            body = response.json()
            if isinstance(body, Mapping) and body.get("error"):
                raise TransportError(f"server error: {str(body['error'])[:300]}")
            answer = _answer(_answers(body, set(payload["questions"])), request)
        except ValueError as e:
            format_error = str(e)
        return TurnResponse(
            raw=response.text,
            answer=answer,
            elapsed_s=elapsed_s,
            usage=_usage(body),
            format_error=format_error,
        )


def questions(request: TurnRequest) -> dict[str, dict]:
    actions = {"move": "Make a move", "resign": "Resign the game"}
    if request.draw_offered:
        actions["accept_draw"] = "Accept the opponent's draw offer"
    return {
        "action": _choice("What do you do this turn?", actions),
        "move": _choice("Which move do you play?", request.legal_moves),
        "offer_draw": _choice(
            "Do you offer a draw together with your move?",
            {"yes": "Offer a draw", "no": "Do not offer a draw"},
        ),
    }


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


def _answer(answers: Mapping, request: TurnRequest) -> TurnAnswer:
    asked = questions(request)
    action = _selected(answers, "action", asked["action"]["criteria"])
    if action != "move":
        return TurnAnswer(action=action)
    return TurnAnswer(
        action="move",
        move=_selected(answers, "move", request.legal_moves),
        offer_draw=_selected(answers, "offer_draw", asked["offer_draw"]["criteria"]) == "yes",
    )


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
