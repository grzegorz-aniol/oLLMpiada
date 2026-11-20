import json
import os
import sys

import pytest

import ollmpiada
from ollmpiada.cli import main


def models_rows(out: str) -> dict[str, list[str]]:
    """Table rows keyed by model id, as lists of stripped cells."""
    rows = {}
    for line in out.splitlines():
        cells = [c.strip() for c in line.split("│")[1:-1]]
        if cells and cells[0]:
            rows[cells[0]] = cells
    return rows


def test_models_lists_type_efforts_and_availability(models_file, monkeypatch, capsys):
    monkeypatch.delenv("TEST_HOSTED_KEY")
    assert main(["models"]) == 0
    rows = models_rows(capsys.readouterr().out)
    assert rows["local-llm"][1] == "llm"
    assert rows["local-llm"][3] == "low, high"
    assert rows["local-llm"][4] == "yes"
    assert rows["hosted-llm"][4] == "no"
    assert rows["basal-mini"][1:4] == ["decision", "basal", "-"]


def test_invalid_config_exits_with_message_naming_the_entry(models_file, capsys):
    models_file.write_text('{"providers": [], "models": [{"id": "x"}]}')
    assert main(["models"]) == 2
    out = capsys.readouterr().out
    assert "Invalid configuration" in out and "models.0.provider" in out


def test_unknown_command_exits_with_usage():
    with pytest.raises(SystemExit) as exit_info:
        main(["dance"])
    assert exit_info.value.code == 2


def test_entry_point_loads_dotenv_but_shell_values_win(
    models_file, models_data, tmp_path, monkeypatch, capsys
):
    models_data["models"].append(
        {"id": "from-dotenv", "provider": "local", "model": "m", "type": "llm"}
    )
    dotenv_config = tmp_path / "dotenv-models.json"
    dotenv_config.write_text(json.dumps(models_data))
    (tmp_path / ".env").write_text(
        f"OLLMPIADA_MODELS_CONFIG={dotenv_config}\nTEST_HOSTED_KEY=from-file\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OLLMPIADA_MODELS_CONFIG")
    monkeypatch.setattr(sys, "argv", ["ollmpiada", "models"])

    with pytest.raises(SystemExit) as exit_info:
        ollmpiada.main()

    assert exit_info.value.code == 0
    assert "from-dotenv" in models_rows(capsys.readouterr().out)
    assert os.environ["TEST_HOSTED_KEY"] == "secret"
