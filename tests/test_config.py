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
    assert settings.runs_dir == Path(".local/runs")
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
    assert list(models) == ["local-llm", "hosted-llm", "claude", "basal-mini"]
    local = models["local-llm"]
    assert local.provider.url == "http://local/v1"
    assert local.spec.reasoning_efforts == ("low", "high")
    assert local.token is None and local.available
    assert models["hosted-llm"].token == "secret"
    assert models["basal-mini"].spec.reasoning_efforts == ()
    assert models["basal-mini"].spec.max_options is None
    assert local.provider.timeout_s is None and local.spec.max_tokens is None


def test_provider_timeout_and_model_max_tokens(tmp_path, models_data):
    models_data["providers"][0]["timeout_s"] = 600
    models_data["models"][0]["max_tokens"] = 2048
    models = load_models(write(tmp_path, models_data), {})
    assert models["local-llm"].provider.timeout_s == 600
    assert models["local-llm"].spec.max_tokens == 2048


def test_openai_model_takes_reasoning_budget_below_max_tokens(tmp_path, models_data):
    models_data["models"][0].update(max_tokens=8192, reasoning_budget=4096)
    models_data["models"][1]["reasoning_budget"] = 100_000
    models = load_models(write(tmp_path, models_data), {"TEST_HOSTED_KEY": "k"})
    assert models["local-llm"].spec.reasoning_budget == 4096
    assert models["hosted-llm"].spec.reasoning_budget == 100_000
    assert models["claude"].spec.reasoning_budget is None


def test_systemone_model_takes_max_options(tmp_path, models_data):
    models_data["models"][3]["max_options"] = 26
    models = load_models(write(tmp_path, models_data), {})
    assert models["basal-mini"].spec.max_options == 26


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
        (lambda d: d["providers"][0].update(api="gemini"), r"providers\.0\.api\s+Input should"),
        (lambda d: d["providers"][0].pop("api"), r"providers\.0\.api\s+Field required"),
        (lambda d: d["models"][0].update(type="llm"), r"models\.0\.type\s+Extra inputs"),
        (lambda d: d["models"][3].update(reasoning_efforts=["low"]), "systemone model"),
        (lambda d: d["models"][0].update(reasoning_efforts=["huge"]), r"efforts\.0\s+Input should"),
        (lambda d: d["models"][0].update(max_options=26), "not a systemone model"),
        (lambda d: d["models"][3].update(max_options=1), r"max_options\s+Input should be greater"),
        (lambda d: d["models"][3].update(max_tokens=100), "it takes no max_tokens"),
        (lambda d: d["models"][0].update(max_tokens=0), r"max_tokens\s+Input should be greater"),
        (lambda d: d["providers"][0].update(timeout_s=0), r"timeout_s\s+Input should be greater"),
        (lambda d: d["models"][2].update(reasoning_budget=2048), "it takes no reasoning_budget"),
        (lambda d: d["models"][3].update(reasoning_budget=2048), "it takes no reasoning_budget"),
        (
            lambda d: d["models"][0].update(reasoning_budget=0),
            r"reasoning_budget\s+Input should be greater",
        ),
        (
            lambda d: d["models"][0].update(reasoning_budget=1024, max_tokens=1024),
            "reasoning_budget must be below max_tokens",
        ),
        (lambda d: d["models"][0].pop("model"), r"models\.0\.model\s+Field required"),
        (lambda d: d["providers"][0].update(token="leak"), r"providers\.0\.token\s+Extra inputs"),
    ],
    ids=[
        "duplicate-id",
        "duplicate-provider",
        "unknown-provider",
        "bad-api",
        "missing-api",
        "type-dropped",
        "systemone-with-efforts",
        "bad-effort",
        "max-options-on-llm",
        "max-options-below-two",
        "systemone-with-max-tokens",
        "max-tokens-below-one",
        "timeout-not-positive",
        "anthropic-with-reasoning-budget",
        "systemone-with-reasoning-budget",
        "reasoning-budget-below-one",
        "reasoning-budget-not-below-max-tokens",
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
