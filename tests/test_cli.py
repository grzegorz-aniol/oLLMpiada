import json
import os
import sys

import pytest

import ollmpiada
from ollmpiada.cli import main
from ollmpiada.players.base import TransportError
from tests.fakes import ScriptedPlayer, unreadable


def models_rows(out: str) -> dict[str, list[str]]:
    """Table rows keyed by model id, as lists of stripped cells."""
    rows = {}
    for line in out.splitlines():
        cells = [c.strip() for c in line.split("│")[1:-1]]
        if cells and cells[0]:
            rows[cells[0]] = cells
    return rows


def test_models_lists_api_efforts_and_availability(models_file, monkeypatch, capsys):
    monkeypatch.delenv("TEST_HOSTED_KEY")
    assert main(["models"]) == 0
    rows = models_rows(capsys.readouterr().out)
    assert rows["local-llm"][1] == "openai-completions"
    assert rows["claude"][1] == "anthropic-messages"
    assert rows["local-llm"][3] == "low, high"
    assert rows["local-llm"][4] == "yes"
    assert rows["hosted-llm"][4] == "no"
    assert rows["basal-mini"][1:4] == ["systemone", "basal", "-"]


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
    models_data["models"].append({"id": "from-dotenv", "provider": "local", "model": "m"})
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


@pytest.fixture
def probe_with(monkeypatch, models_file):
    """Run `probe` against a scripted player instead of a real model."""

    def run(*script, name="local-llm@high"):
        player = ScriptedPlayer(name, list(script))
        created = []

        def fake_create(player_name, models, *, timeout_s):
            created.append((player_name, timeout_s))
            return player

        monkeypatch.setattr("ollmpiada.cli.create_player", fake_create)
        code = main(["probe", name])
        return code, player, created

    return run


@pytest.mark.opening
def test_probe_checks_the_opening_and_the_full_answer_format(probe_with, capsys):
    code, player, created = probe_with("Nf3", "e4")
    out = capsys.readouterr().out
    assert code == 0
    assert created == [("local-llm@high", 120)]
    assert "Opening turn (move only)" in out and "Move 6 (full answer)" in out
    assert "legal (Nf3)" in out and "legal (e4)" in out
    assert "1.00 s" in out and "in 100, out 10" in out
    opening, later = player.requests
    assert opening.move_only and not later.move_only
    for request in (opening, later):
        assert len(request.legal_moves) == 20 and not request.draw_offered
        assert "as White" in request.prompt.system
    assert "move 1 of 50" in opening.prompt.user and "move 6 of 50" in later.prompt.user


def test_probe_reports_an_illegal_move(probe_with, capsys):
    code, _, _ = probe_with("e5", "e4")
    assert code == 0
    assert "illegal: 'e5' is not a legal move" in capsys.readouterr().out


@pytest.mark.opening
@pytest.mark.parametrize(
    "script",
    [(unreadable("I like e4"), "e4"), ("e4", unreadable("I like e4"))],
    ids=["move-only-turn", "full-turn"],
)
def test_probe_fails_on_a_format_error_in_either_turn(probe_with, capsys, script):
    code, player, _ = probe_with(*script)
    out = capsys.readouterr().out
    assert code == 1 and "format error" in out and "I like e4" in out
    assert [r.move_only for r in player.requests] == [True, False]


def test_probe_fails_on_a_transport_error(probe_with, capsys):
    code, _, _ = probe_with(TransportError("connection refused"))
    assert code == 1 and "connection refused" in capsys.readouterr().out


@pytest.mark.opening
def test_probe_keeps_the_first_turn_when_the_second_fails_to_connect(probe_with, capsys):
    code, _, _ = probe_with("Nf3", TransportError("connection refused"))
    out = capsys.readouterr().out
    assert code == 1 and "legal (Nf3)" in out and "connection refused" in out
    assert "Move 6 (full answer)" not in out


def test_probe_rejects_an_unknown_model(models_file, capsys):
    assert main(["probe", "nope@high"]) == 2
    assert "unknown model 'nope'" in capsys.readouterr().out
