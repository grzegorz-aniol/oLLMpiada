import copy
import json
from pathlib import Path

import pytest

MODELS = {
    "providers": [
        {"name": "local", "url": "http://local/v1", "api": "openai-completions"},
        {
            "name": "hosted",
            "url": "https://hosted/v1",
            "api": "openai-completions",
            "token_env": "TEST_HOSTED_KEY",
        },
        {"name": "claude-proxy", "url": "http://proxy/anthropic", "api": "anthropic-messages"},
        {"name": "basal", "url": "http://basal/v1/systemone", "api": "systemone"},
    ],
    "models": [
        {
            "id": "local-llm",
            "provider": "local",
            "model": "qwen",
            "reasoning_efforts": ["low", "high"],
        },
        {"id": "hosted-llm", "provider": "hosted", "model": "gpt"},
        {
            "id": "claude",
            "provider": "claude-proxy",
            "model": "anthropic/claude-haiku-5-5",
            "reasoning_efforts": ["none", "low"],
        },
        {"id": "basal-mini", "provider": "basal", "model": "basal"},
    ],
}


@pytest.fixture
def models_data() -> dict:
    """A fresh copy of a valid model config: OpenAI (with and without a token), Anthropic
    and SystemOne providers."""
    return copy.deepcopy(MODELS)


@pytest.fixture
def models_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A model config file the CLI picks up through OLLMPIADA_MODELS_CONFIG."""
    path = tmp_path / "models.json"
    path.write_text(json.dumps(MODELS))
    monkeypatch.setenv("OLLMPIADA_MODELS_CONFIG", str(path))
    monkeypatch.setenv("OLLMPIADA_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TEST_HOSTED_KEY", "secret")
    return path


@pytest.fixture(autouse=True)
def no_opening(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the opening off, so a script can resign or offer a draw on its first move; tests
    of the opening rule itself are marked `opening`."""
    if request.node.get_closest_marker("opening") is None:
        monkeypatch.setattr("ollmpiada.games.base.OPENING_MOVES", 0)
