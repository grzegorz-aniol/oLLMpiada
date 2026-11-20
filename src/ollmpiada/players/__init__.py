"""Players by provider API, named `<id>[@effort]` on the command line."""

from typing import cast

from ollmpiada.config import Effort, Model
from ollmpiada.players.base import Player
from ollmpiada.players.decision import DecisionPlayer
from ollmpiada.players.llm import create_llm_player


class PlayerError(ValueError):
    """A player name that cannot be used in a competition."""


def resolve_player(name: str, models: dict[str, Model]) -> tuple[Model, Effort | None]:
    """Find the model and effort a `<id>[@effort]` name refers to.

    Raises:
        PlayerError: Unknown or unavailable model, or an effort the model does not list.
    """
    model_id, _, effort = name.partition("@")
    model = models.get(model_id)
    if model is None:
        raise PlayerError(f"unknown model {model_id!r}; known: {', '.join(models)}")
    if not model.available:
        raise PlayerError(
            f"model {model_id!r} is unavailable: set {model.provider.token_env} in the environment"
        )
    if not effort:
        return model, None
    if effort not in model.spec.reasoning_efforts:
        listed = ", ".join(model.spec.reasoning_efforts) or "no efforts"
        raise PlayerError(f"model {model_id!r} does not accept effort {effort!r} ({listed} listed)")
    return model, cast(Effort, effort)


def create_player(name: str, models: dict[str, Model], *, timeout_s: float) -> Player:
    """Build the player a `<id>[@effort]` name refers to.

    `timeout_s` is the default per-call timeout; the model's provider `timeout_s`
    replaces it.

    Raises:
        PlayerError: See `resolve_player`.
    """
    model, effort = resolve_player(name, models)
    timeout_s = model.provider.timeout_s or timeout_s
    if model.provider.api == "systemone":
        return DecisionPlayer(name, model, timeout_s=timeout_s)
    return create_llm_player(name, model, effort=effort, timeout_s=timeout_s)
