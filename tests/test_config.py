import json
import logging
from pathlib import Path

import pytest

from ollmpiada.config import ConfigError, load_models, load_settings


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "models.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_settings_defaults():
    settings = load_settings({})
    assert settings.models_config == Path("config/models.json")
    assert settings.runs_dir == Path("runs")
    assert settings.request_timeout_s == 120


def test_settings_from_environment():
    settings = load_settings(
        {
            "OLLMPIADA_MODELS_CONFIG": "x.json",
            "OLLMPIADA_RUNS_DIR": "out",
            "OLLMPIADA_REQUEST_TIMEOUT_S": "7.5",
        }
    )
    assert settings == type(settings)(Path("x.json"), Path("out"), 7.5)


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_invalid_timeout_is_rejected(value):
    with pytest.raises(ConfigError, match="OLLMPIADA_REQUEST_TIMEOUT_S"):
        load_settings({"OLLMPIADA_REQUEST_TIMEOUT_S": value})


def test_loads_models_with_providers_and_tokens(tmp_path, models_data):
    models = load_models(write(tmp_path, models_data), {"TEST_HOSTED_KEY": "secret"})
    assert list(models) == ["local-llm", "hosted-llm", "basal-mini"]
    local = models["local-llm"]
    assert local.provider.url == "http://local/v1"
    assert local.spec.reasoning_efforts == ("low", "high")
    assert local.token is None and local.available
    assert models["hosted-llm"].token == "secret"
    assert models["basal-mini"].spec.reasoning_efforts == ()


def test_missing_token_variable_warns_and_marks_unavailable(tmp_path, models_data, caplog):
    with caplog.at_level(logging.WARNING):
        models = load_models(write(tmp_path, models_data), {})
    assert not models["hosted-llm"].available
    assert models["local-llm"].available
    assert "hosted-llm" in caplog.text and "TEST_HOSTED_KEY" in caplog.text


def test_empty_token_variable_means_no_token_but_available(tmp_path, models_data):
    models = load_models(write(tmp_path, models_data), {"TEST_HOSTED_KEY": ""})
    assert models["hosted-llm"].available
    assert models["hosted-llm"].token is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["models"].append(dict(d["models"][0])), "duplicate model id"),
        (lambda d: d["providers"].append(dict(d["providers"][0])), "duplicate provider name"),
        (lambda d: d["models"][0].update(provider="nope"), "unknown provider 'nope'"),
        (lambda d: d["models"][0].update(type="engine"), "should be .llm. or .decision."),
        (lambda d: d["models"][0].update(reasoning_efforts=["huge"]), r"efforts\.0\s+Input should"),
        (lambda d: d["models"][0].pop("model"), r"models\.0\.model\s+Field required"),
        (lambda d: d["providers"][0].update(token="leak"), r"providers\.0\.token\s+Extra inputs"),
    ],
    ids=[
        "duplicate-id",
        "duplicate-provider",
        "unknown-provider",
        "bad-type",
        "bad-effort",
        "missing-field",
        "unknown-field",
    ],
)
def test_invalid_config_is_rejected(tmp_path, models_data, change, message):
    change(models_data)
    with pytest.raises(ConfigError, match=message):
        load_models(write(tmp_path, models_data), {})


def test_invalid_json_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Expecting property name"):
        load_models(write(tmp_path, "{not json"), {})


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="nope.json"):
        load_models(tmp_path / "nope.json", {})
