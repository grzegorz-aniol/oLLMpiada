"""Settings from the environment and the model config file."""

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

log = logging.getLogger(__name__)

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ConfigError(Exception):
    """The model config file is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    models_config: Path
    runs_dir: Path
    request_timeout_s: float


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    """Read the `OLLMPIADA_*` variables; `.env` is loaded into the environment beforehand.

    Raises:
        ConfigError: The timeout is not a positive number.
    """
    timeout = env.get("OLLMPIADA_REQUEST_TIMEOUT_S") or "120"
    try:
        request_timeout_s = float(timeout)
    except ValueError:
        request_timeout_s = 0
    if request_timeout_s <= 0:
        raise ConfigError(f"OLLMPIADA_REQUEST_TIMEOUT_S must be a positive number, not {timeout!r}")
    return Settings(
        models_config=Path(env.get("OLLMPIADA_MODELS_CONFIG") or "config/models.json"),
        runs_dir=Path(env.get("OLLMPIADA_RUNS_DIR") or "runs"),
        request_timeout_s=request_timeout_s,
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provider(_Strict):
    name: str
    url: str
    token_env: str | None = None


class ModelSpec(_Strict):
    id: str
    provider: str
    model: str
    type: Literal["llm", "decision"]
    reasoning_efforts: tuple[Effort, ...] = ()


class ModelsFile(_Strict):
    providers: list[Provider]
    models: list[ModelSpec]

    @model_validator(mode="after")
    def _check_references(self) -> ModelsFile:
        names = [p.name for p in self.providers]
        if duplicate := _first_duplicate(names):
            raise ValueError(f"duplicate provider name {duplicate!r}")
        if duplicate := _first_duplicate([m.id for m in self.models]):
            raise ValueError(f"duplicate model id {duplicate!r}")
        for m in self.models:
            if m.provider not in names:
                raise ValueError(f"model {m.id!r} refers to unknown provider {m.provider!r}")
        return self


def _first_duplicate(values: list[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


@dataclass(frozen=True)
class Model:
    """A configured model with its provider resolved and its token looked up."""

    spec: ModelSpec
    provider: Provider
    token: str | None
    available: bool

    @property
    def id(self) -> str:
        return self.spec.id


def load_models(path: Path, env: Mapping[str, str] = os.environ) -> dict[str, Model]:
    """Load and validate the model config, keyed by model id.

    Raises:
        ConfigError: The file is missing, not JSON, or fails validation.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = ModelsFile.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as e:
        raise ConfigError(f"{path}: {e}") from e

    providers = {p.name: p for p in parsed.providers}
    models = {}
    for spec in parsed.models:
        provider = providers[spec.provider]
        token, available = None, True
        if provider.token_env:
            token = env.get(provider.token_env)
            if token is None:
                log.warning(
                    "model %s unavailable: environment variable %s is not set",
                    spec.id,
                    provider.token_env,
                )
                available = False
        models[spec.id] = Model(spec, provider, token or None, available)
    return models
