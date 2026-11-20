import copy
import json
from pathlib import Path

import pytest

MODELS = {
    "providers": [
        {"name": "local", "url": "http://local/v1"},
        {"name": "hosted", "url": "https://hosted/v1", "token_env": "TEST_HOSTED_KEY"},
        {"name": "basal", "url": "http://basal/v1/systemone"},
    ],
    "models": [
        {
            "id": "local-llm",
            "provider": "local",
            "model": "qwen",
            "type": "llm",
            "reasoning_efforts": ["low", "high"],
        },
        {"id": "hosted-llm", "provider": "hosted", "model": "gpt", "type": "llm"},
        {"id": "basal-mini", "provider": "basal", "model": "basal", "type": "decision"},
    ],
}


@pytest.fixture
def models_data() -> dict:
    """A fresh copy of a valid model config: two LLM providers (one with a token) and SystemOne."""
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
