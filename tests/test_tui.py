import asyncio
import json
import os
import signal

import chess
import pytest
from rich.console import Console

from ollmpiada.cli import main
from ollmpiada.competition import Competition
from ollmpiada.game_master import GameStarted, MoveMade
from ollmpiada.games.base import Side
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players import resolve_player
from ollmpiada.players.base import TransportError, TurnAnswer, TurnResponse
from ollmpiada.tui import LiveView, stats_table
from tests.fakes import ScriptedPlayer

RESIGN = TurnAnswer(action="resign")
# Game 1: the first player is White and gets mated; game 2: it plays Black and resigns.
FIRST_SCRIPT = ["f3", "g4", RESIGN]
SECOND_SCRIPT = ["e5", "Qh4#", "e4"]


@pytest.fixture
def scripted(monkeypatch, models_file):
    """Make `run` use scripted players, keyed by the name given on the command line."""
    scripts: dict[str, list] = {}

    def fake_create(name, models, *, timeout_s):
        resolve_player(name, models)
        return ScriptedPlayer(name, scripts[name])

    monkeypatch.setattr("ollmpiada.cli.create_player", fake_create)
    monkeypatch.setitem(Competition.__init__.__kwdefaults__, "backoff_s", (0, 0))
    return scripts


def saved_runs(models_file):
    runs_dir = models_file.parent / "runs"
    return sorted(runs_dir.iterdir()) if runs_dir.exists() else []


def render(renderable) -> str:
    console = Console(record=True, width=140)
    console.print(renderable)
    return console.export_text()


# --- run command; under capsys stdout is not a terminal, so output is plain ---


def test_run_prints_one_line_per_game_and_a_summary(scripted, models_file, capsys):
    scripted.update({"local-llm@high": FIRST_SCRIPT, "basal-mini": SECOND_SCRIPT})
    assert main(["run", "local-llm@high", "basal-mini", "--games", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "game 1: White local-llm@high vs Black basal-mini: 0-1 (checkmate), 4 moves",
        "game 2: White basal-mini vs Black local-llm@high: 1-0 (resignation), 1 move",
    ]
    assert "Competition finished: 2 of 2 games played." in lines
    assert "basal-mini: 2 wins, 0 losses, 0 draws" in lines
    assert not any("Odd number" in line for line in lines)
    assert lines[-1] == f"Saved to {saved_runs(models_file)[0]}"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["nope", "basal-mini"], "unknown model 'nope'"),
        (["local-llm@medium", "basal-mini"], "does not accept effort 'medium'"),
        (["hosted-llm", "basal-mini"], "unavailable"),
        (["basal-mini", "basal-mini"], "two different players"),
    ],
)
def test_run_rejects_bad_players_before_starting(
    scripted, models_file, monkeypatch, capsys, args, message
):
    monkeypatch.delenv("TEST_HOSTED_KEY")
    assert main(["run", *args]) == 2
    assert message in capsys.readouterr().out
    assert saved_runs(models_file) == []


@pytest.mark.parametrize("flag", ["--games", "--max-turns"])
def test_run_rejects_non_positive_numbers(scripted, flag):
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "local-llm", "basal-mini", flag, "0"])
    assert exit_info.value.code == 2


def test_run_exits_non_zero_when_aborted(scripted, capsys):
    error = TransportError("refused")
    scripted.update({"local-llm": ["e4", "Nf3"], "basal-mini": ["e5", error, error, error]})
    assert main(["run", "local-llm", "basal-mini"]) == 1
    out = capsys.readouterr().out
    assert "game 1: White local-llm vs Black basal-mini: aborted" in out
    assert "Competition aborted" in out and "Error: basal-mini: refused" in out


def test_ctrl_c_stops_the_competition_and_keeps_finished_games(
    scripted, models_file, monkeypatch, capsys
):
    scripted.update({"local-llm": FIRST_SCRIPT, "basal-mini": SECOND_SCRIPT})
    play = ScriptedPlayer.play

    async def press_ctrl_c_in_game_two(self, request):
        if self.name == "basal-mini" and len(self.requests) == 2:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(10)
        return await play(self, request)

    monkeypatch.setattr(ScriptedPlayer, "play", press_ctrl_c_in_game_two)
    assert main(["run", "local-llm", "basal-mini", "--games", "3"]) == 1
    out = capsys.readouterr().out
    assert "Competition stopped: 1 of 3 games played." in out
    assert "Odd number of counted games: local-llm played White one game more often." in out
    run_dir = saved_runs(models_file)[0]
    saved = json.loads((run_dir / "competition.json").read_text())
    assert saved["status"] == "stopped" and len(saved["results"]) == 1
    assert not (run_dir / "game-002.json").exists()


def test_run_in_a_terminal_uses_the_live_view(scripted, monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    scripted.update({"local-llm@high": FIRST_SCRIPT, "basal-mini": SECOND_SCRIPT})
    assert main(["run", "local-llm@high", "basal-mini"]) == 0
    out = capsys.readouterr().out
    assert "Statistics" in out and "game 1:" not in out


def test_run_states_the_side_imbalance_of_an_odd_number_of_games(scripted, capsys):
    scripted.update({"local-llm": [RESIGN, RESIGN, RESIGN], "basal-mini": ["e4"]})
    assert main(["run", "local-llm", "basal-mini", "--games", "3"]) == 0
    assert "local-llm played White one game more often." in capsys.readouterr().out


def test_live_run_prints_log_warnings_through_the_live_console(scripted, monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    retry = TransportError("connection reset")
    scripted.update({"local-llm": [retry, RESIGN], "basal-mini": [RESIGN]})
    assert main(["run", "local-llm", "basal-mini"]) == 0
    out = capsys.readouterr().out
    assert "connection reset; retrying" in out


# --- show command ---


def test_show_prints_a_saved_competition(scripted, models_file, capsys):
    scripted.update({"local-llm@high": FIRST_SCRIPT, "basal-mini": SECOND_SCRIPT})
    main(["run", "local-llm@high", "basal-mini"])
    capsys.readouterr()
    assert main(["show", str(saved_runs(models_file)[0])]) == 0
    out = capsys.readouterr().out
    assert "Results" in out and "Statistics" in out and "checkmate" in out
    assert "Competition finished: 2 of 2 games played." in out


def test_show_rejects_a_missing_run(tmp_path, capsys):
    assert main(["show", str(tmp_path / "nothing")]) == 2
    assert "Cannot read" in capsys.readouterr().out


# --- live view ---


def play_into(view, tmp_path, first, second, games=2):
    players = (ScriptedPlayer("model-a", first), ScriptedPlayer("model-b", second))
    competition = Competition(
        ChessGame(),
        players,
        games=games,
        max_turns=20,
        runs_dir=tmp_path / "runs",
        publish=view.handle,
    )
    return asyncio.run(competition.run())


def test_live_view_visual_mode_shows_board_moves_and_rejections(tmp_path):
    view = LiveView(ChessGame(), visual=True)
    assert render(view).strip() == "Starting..."
    play_into(view, tmp_path, ["f3", "Ke3", "g4"], ["e5", "Qh4#"], games=1)
    text = render(view)
    assert "Games 1 of 1 | finished" in text
    lines = text.splitlines()
    moves = next(i for i, line in enumerate(lines) if "(1) f3" in line)
    assert lines[moves].strip() == (
        "(1) f3 e5 (2) [✗ 1: 'Ke3' is not a legal move in this position] g4 Qh4#"
    )
    assert lines[moves + 1].startswith("8 ") and "♛" in text and "a  b  c" in text
    assert "White" in text and "Black" in text and "checkmate" in text and "Statistics" in text


def test_live_view_shows_the_latest_moves_and_keeps_the_board_in_place(tmp_path):
    view = LiveView(ChessGame(), visual=True)
    play_into(view, tmp_path, [RESIGN], [], games=1)
    view.handle(GameStarted(2, ("model-a", "model-b"), chess.Board()))

    def board_row():
        return next(i for i, line in enumerate(render(view).splitlines()) if line.startswith("8 "))

    empty_list_row = board_row()
    for turn in range(120):
        side, move = (Side.FIRST, "Nf3") if turn % 2 == 0 else (Side.SECOND, "Nc6")
        view.handle(MoveMade(2, side, "model", move, chess.Board(), draw_offered=False))
    text = render(view)
    assert board_row() == empty_list_row
    assert "(60) Nf3 Nc6" in text and "(1) " not in text


def test_live_view_background_mode_has_no_board(tmp_path):
    view = LiveView(ChessGame(), visual=False)
    play_into(view, tmp_path, FIRST_SCRIPT, SECOND_SCRIPT)
    text = render(view)
    assert "♛" not in text and "(1) f3" not in text
    assert "Games 2 of 2 | finished" in text and "resignation" in text


def test_live_view_status_shows_the_current_game_and_turn(tmp_path):
    view = LiveView(ChessGame(), visual=False)
    statuses = []
    handle = view.handle

    def handle_and_render(event):
        handle(event)
        statuses.append(render(view).splitlines()[0])

    view.handle = handle_and_render
    play_into(view, tmp_path, ["f3", "g4"], ["e5", "Qh4#"], games=1)
    assert "Games 0 of 1 | game 1, move 2 | running" in statuses


def test_statistics_never_show_unknown_tokens_as_zero(tmp_path):
    view = LiveView(ChessGame(), visual=False)
    no_usage = TurnResponse('{"action":"resign"}', RESIGN, 2.0, None)
    state = play_into(view, tmp_path, [no_usage], [], games=1)
    a_row = next(line for line in render(stats_table(state)).splitlines() if "model-a" in line)
    assert "unknown" in a_row and " 0 / " not in a_row


def test_unreported_reasoning_is_shown_as_unknown(tmp_path):
    view = LiveView(ChessGame(), visual=False)
    answer = TurnResponse('{"action":"resign"}', RESIGN, 2.0, None)
    state = play_into(view, tmp_path, [answer], [], games=1)
    stats = state.stats["model-a"]
    stats.calls_without_usage, stats.calls_without_reasoning = 0, 1
    stats.input_tokens, stats.output_tokens = 12_345, 67
    a_row = next(line for line in render(stats_table(state)).splitlines() if "model-a" in line)
    assert "12.3k / 67 / ?" in a_row
