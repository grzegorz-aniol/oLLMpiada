import asyncio
import json
import os
import signal
from contextlib import nullcontext
from datetime import datetime, timedelta

import chess
import pytest
from chess.engine import Cp
from rich.console import Console
from rich.live import Live

from ollmpiada.cli import main
from ollmpiada.competition import (
    Competition,
    CompetitionFinished,
    CompetitionProgress,
    CompetitionState,
    GameSummary,
    PlayerStats,
    load_state,
)
from ollmpiada.game_master import AnswerReceived, GameStarted, MoveMade, TurnBasedMaster
from ollmpiada.games.base import Side
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players import resolve_player
from ollmpiada.players.base import TransportError, TurnAnswer, TurnResponse, Usage
from ollmpiada.registry import MASTERS
from ollmpiada.tui import (
    LiveView,
    board_table,
    game_line,
    results_table,
    stats_table,
    summary_lines,
)
from tests.fakes import WARM_UP, Judged, MovesOnly, PickingPlayer, ScriptedPlayer

RESIGN = TurnAnswer(action="resign")
CHESS = TurnBasedMaster(ChessGame())
REAL_SESSION = ChessGame.session
# Game 1: the first player is White and gets mated; game 2: it plays Black and resigns.
FIRST_SCRIPT = ["f3", "g4", RESIGN]
SECOND_SCRIPT = ["e5", "Qh4#", "e4"]


@pytest.fixture
def scripted(monkeypatch, models_file):
    """Make `run` use scripted players, keyed by the name given on the command line and
    answering the warm-up call before their script, and start no engine."""
    scripts: dict[str, list] = {}

    def fake_create(name, models, *, timeout_s):
        resolve_player(name, models)
        return ScriptedPlayer(name, [WARM_UP, *scripts[name]])

    monkeypatch.setattr("ollmpiada.cli.create_player", fake_create)
    monkeypatch.setattr(ChessGame, "session", lambda self, settings: nullcontext())
    monkeypatch.setitem(Competition.__init__.__kwdefaults__, "backoff_s", (0, 0))
    return scripts


def saved_runs(models_file):
    runs_dir = models_file.parent / "runs"
    return sorted(runs_dir.iterdir()) if runs_dir.exists() else []


def render(renderable) -> str:
    console = Console(record=True, width=140)
    console.print(renderable)
    return console.export_text()


def squeezed(text: str) -> list[str]:
    """The lines of `text` with each run of spaces as one, to compare table rows."""
    return [" ".join(line.split()) for line in text.splitlines()]


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
    assert "basal-mini: 2 wins, 0 losses, 0 draws; adjudicated 0 wins, 0 losses, 0 draws" in lines
    assert not any("Odd number" in line for line in lines)
    assert lines[-1] == f"Saved to {saved_runs(models_file)[0]}"


def test_run_presents_adjudicated_results_apart(scripted, models_file, monkeypatch, capsys):
    monkeypatch.setitem(MASTERS, "chess", TurnBasedMaster(Judged(Cp(-650))))
    scripted.update({"local-llm": ["e4"], "basal-mini": ["e5"]})
    assert main(["run", "local-llm", "basal-mini", "--games", "1", "--max-turns", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "game 1: White local-llm vs Black basal-mini: 0-1 (adjudication: -650 cp), 2 moves"
    )
    assert "basal-mini: 0 wins, 0 losses, 0 draws; adjudicated 1 wins, 0 losses, 0 draws" in lines

    state = load_state(saved_runs(models_file)[0])
    assert "adjudication: -650 cp" in render(results_table(state))
    stats = render(stats_table(state)).splitlines()
    header = next(line for line in stats if "Games" in line)
    assert "Adj. W/L/D" in header and "Points" not in header
    basal = next(line for line in stats if line.split()[:1] == ["basal-mini"])
    assert basal.split()[1:6] == ["1", "0", "0", "0", "1/0/0"]


def test_points_get_a_column_and_a_summary_entry_when_a_counted_game_has_them():
    a = PlayerStats("model-a", games=2, wins=1, losses=1, points=15)
    b = PlayerStats("model-b", games=2, wins=1, losses=1, points=11)
    made = GameSummary(1, ("model-a", "model-b"), "model-a", "9-4", "made", 13, points=(9, 4))
    state = CompetitionState(
        game="cards",
        side_names=("North-South", "East-West"),
        players=("model-a", "model-b"),
        games=2,
        max_turns=None,
        seed=1,
        stats={"model-a": a, "model-b": b},
        results=[made],
    )
    stats = render(stats_table(state)).splitlines()
    assert "Points" in next(line for line in stats if "Games" in line)
    a_row = next(line for line in stats if line.split()[:1] == ["model-a"])
    assert a_row.split()[1:7] == ["2", "1", "1", "0", "0/0/0", "15"]
    assert (
        "model-b: 1 wins, 1 losses, 0 draws; adjudicated 0 wins, 0 losses, 0 draws; 11 points"
    ) in summary_lines(state)
    assert game_line(made, state.side_names) == (
        "game 1: North-South model-a vs East-West model-b: 9-4 (made), 13 moves"
    )


def board_state(*tables: tuple[int, int]) -> CompetitionState:
    """A duplicate competition whose games took these points in side order, model-a playing
    the first side in odd games."""
    names = ("model-a", "model-b")
    results = [
        GameSummary(n, names if n % 2 else names[::-1], None, "-", "made", 52, points=points)
        for n, points in enumerate(tables, start=1)
    ]
    return CompetitionState(
        game="cards",
        side_names=("North-South", "East-West"),
        players=names,
        games=len(tables),
        max_turns=None,
        seed=1,
        stats={name: PlayerStats(name) for name in names},
        results=results,
    )


def test_the_board_table_shows_both_tables_and_a_tie_when_they_took_the_same_points():
    # Board 1: model-a takes 9 as North-South, model-b 7; board 2: both take 6.
    state = board_state((9, 4), (7, 6), (6, 7), (6, 7), (5, 8))
    rows = squeezed(render(board_table(state)))
    assert "Board model-a model-b Result" in rows
    assert "1 9 + 6 4 + 7 model-a by 4" in rows
    assert "2 6 + 7 7 + 6 tie" in rows
    # Board 3 has one table so far, so it has no result yet.
    assert not any(row.startswith("3 ") for row in rows)


def test_the_board_table_names_the_second_player_when_it_wins_the_board():
    state = board_state((8, 5), (10, 3))
    assert "1 8 + 3 5 + 10 model-b by 4" in squeezed(render(board_table(state)))


def test_there_is_no_board_table_before_a_board_has_both_tables():
    assert board_table(board_state()) is None
    assert board_table(board_state((9, 4))) is None


def test_run_stops_before_any_model_call_when_the_engine_cannot_start(
    scripted, models_file, monkeypatch, capsys
):
    monkeypatch.setattr(ChessGame, "session", REAL_SESSION)
    monkeypatch.setenv("OLLMPIADA_STOCKFISH", str(models_file.parent / "no-stockfish"))
    played = []
    monkeypatch.setattr(ScriptedPlayer, "play", lambda self, request: played.append(request))
    scripted.update({"local-llm": [], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini"]) == 2
    out = " ".join(capsys.readouterr().out.split())
    assert "Cannot start chess" in out and "OLLMPIADA_STOCKFISH" in out and "no-stockfish" in out
    assert played == [] and saved_runs(models_file) == []


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


def test_run_names_the_model_that_fails_its_warm_up(scripted, monkeypatch, capsys):
    async def refuse(self, request):
        raise TransportError("connection refused")

    monkeypatch.setattr(ScriptedPlayer, "play", refuse)
    scripted.update({"local-llm": [], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini"]) == 1
    out = capsys.readouterr().out
    assert "Competition aborted: 0 of 2 games played." in out
    assert "Error: warm-up call to basal-mini failed: connection refused" in out


def test_ctrl_c_stops_the_competition_and_keeps_finished_games(
    scripted, models_file, monkeypatch, capsys
):
    scripted.update({"local-llm": FIRST_SCRIPT, "basal-mini": SECOND_SCRIPT})
    play = ScriptedPlayer.play

    async def press_ctrl_c_in_game_two(self, request):
        # Its warm-up and two game-1 answers come first.
        if self.name == "basal-mini" and len(self.requests) == 3:
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


def test_live_view_refreshes_while_a_model_thinks(scripted, monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr("ollmpiada.cli.REFRESH_S", 0)
    thinking = False
    refreshed_while_thinking = []
    play, refresh = ScriptedPlayer.play, Live.refresh

    async def slow_play(self, request):
        nonlocal thinking
        thinking = True
        for _ in range(3):
            await asyncio.sleep(0)
        thinking = False
        return await play(self, request)

    def recording_refresh(self):
        refreshed_while_thinking.append(thinking)
        refresh(self)

    monkeypatch.setattr(ScriptedPlayer, "play", slow_play)
    monkeypatch.setattr(Live, "refresh", recording_refresh)
    scripted.update({"local-llm": [RESIGN], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini", "--games", "1"]) == 0
    assert any(refreshed_while_thinking)


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
    assert "Boards" not in out


def test_run_saves_the_given_seed_and_show_prints_it(scripted, models_file, capsys):
    scripted.update({"local-llm": [RESIGN], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini", "--games", "1", "--seed", "7"]) == 0
    run_dir = saved_runs(models_file)[0]
    assert json.loads((run_dir / "competition.json").read_text())["seed"] == 7
    capsys.readouterr()
    assert main(["show", str(run_dir)]) == 0
    assert "Seed: 7" in capsys.readouterr().out


def test_run_without_a_seed_saves_a_random_31_bit_one(scripted, models_file, monkeypatch):
    bounds = []
    monkeypatch.setattr("ollmpiada.cli.secrets.randbelow", lambda n: bounds.append(n) or n - 1)
    scripted.update({"local-llm": [RESIGN], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini", "--games", "1"]) == 0
    saved = json.loads((saved_runs(models_file)[0] / "competition.json").read_text())
    assert bounds == [2**31] and saved["seed"] == 2**31 - 1


@pytest.mark.parametrize(
    ("game", "flags", "saved"),
    [("chess", [], 50), ("chess", ["--max-turns", "7"], 7), ("moves-only", [], None)],
)
def test_run_saves_the_turn_limit_of_its_game(
    scripted, models_file, monkeypatch, game, flags, saved
):
    monkeypatch.setitem(MASTERS, "moves-only", TurnBasedMaster(MovesOnly()))
    scripted.update({"local-llm": [RESIGN, "a", "a", "a"], "basal-mini": ["b", "b", "b"]})
    args = ["run", "local-llm", "basal-mini", "--games", "1", "--game", game, *flags]
    assert main(args) == 0
    run_dir = saved_runs(models_file)[0]
    assert json.loads((run_dir / "competition.json").read_text())["max_turns"] == saved


def test_run_rejects_a_turn_limit_for_a_game_without_one(
    scripted, models_file, monkeypatch, capsys
):
    monkeypatch.setitem(MASTERS, "moves-only", TurnBasedMaster(MovesOnly()))
    scripted.update({"local-llm": [], "basal-mini": []})
    assert main(["run", "local-llm", "basal-mini", "--game", "moves-only", "--max-turns", "9"]) == 2
    assert "moves-only has no turn limit" in capsys.readouterr().out
    assert saved_runs(models_file) == []


@pytest.fixture
def card_players(monkeypatch, models_file):
    """Make `run` use players that pick a card from the legal list: local-llm the first
    listed, basal-mini the last."""

    def fake_create(name, models, *, timeout_s):
        resolve_player(name, models)
        return PickingPlayer(name, 0 if name == "local-llm" else -1)

    monkeypatch.setattr("ollmpiada.cli.create_player", fake_create)
    monkeypatch.setitem(Competition.__init__.__kwdefaults__, "backoff_s", (0, 0))


def test_run_plays_bridge_and_show_prints_its_points(card_players, models_file, capsys):
    args = ["run", "local-llm", "basal-mini", "--game", "bridge", "--seed", "2"]
    assert main(args) == 0
    lines = squeezed(capsys.readouterr().out)
    assert lines[1:3] == [
        "game 1: North-South local-llm vs East-West basal-mini: 9-4 (made), 52 moves",
        "game 2: North-South basal-mini vs East-West local-llm: 7-6 (down), 52 moves",
    ]
    # local-llm makes its contract and beats basal-mini's: two wins, 9 + (13 - 7) tricks.
    totals = "adjudicated 0 wins, 0 losses, 0 draws"
    assert f"local-llm: 2 wins, 0 losses, 0 draws; {totals}; 15 points" in lines
    assert f"basal-mini: 0 wins, 2 losses, 0 draws; {totals}; 11 points" in lines
    # The board: local-llm took 9 tricks at the first table and 13 - 7 at the second.
    board = "1 9 + 6 4 + 7 local-llm by 4"
    assert lines.index(board) < lines.index("Competition finished: 2 of 2 games played.")
    run_dir = saved_runs(models_file)[0]
    assert sorted(path.name for path in run_dir.glob("game-*")) == [
        "game-001.json", "game-001.pbn", "game-002.json", "game-002.pbn",
    ]  # fmt: skip
    saved = json.loads((run_dir / "competition.json").read_text())
    assert saved["game"] == "bridge" and saved["max_turns"] is None
    capsys.readouterr()
    assert main(["show", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "9-4" in out and "Seed: 2" in out and "15 points" in out
    assert board in squeezed(out)


def test_run_shows_bridge_in_the_live_view(card_players, monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert main(["run", "local-llm", "basal-mini", "--game", "bridge", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "Statistics" in out and "North-South" in out and "game 1:" not in out
    assert "Boards" in out


@pytest.mark.parametrize(("games", "warned"), [("2", True), ("14", True), ("16", False)])
def test_run_warns_before_a_bridge_run_of_fewer_than_eight_boards(
    card_players, capsys, games, warned
):
    assert main(["run", "local-llm", "basal-mini", "--game", "bridge", "--games", games]) == 0
    lines = squeezed(capsys.readouterr().out)
    warning = (
        f"Only {int(games) // 2} of the 8 boards recommended: "
        "a few boards say little about skill (--games 16 plays 8)."
    )
    assert (lines[0] == warning) is warned
    assert lines[1 if warned else 0].startswith("game 1:")


def test_run_warns_of_a_short_bridge_run_in_yellow_before_the_live_view(
    card_players, monkeypatch, capsys
):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert main(["run", "local-llm", "basal-mini", "--game", "bridge", "--games", "2"]) == 0
    out = capsys.readouterr().out
    yellow, reset = "\x1b[33m", "\x1b[0m"
    warning = f"{yellow}Only 1 of the 8 boards recommended:{reset} a few boards say little"
    assert warning in out
    assert out.index(warning) < out.index("Starting...") < out.index("Statistics")


@pytest.mark.parametrize("live", [False, True], ids=["plain", "live"])
def test_run_rejects_an_odd_number_of_bridge_games(
    card_players, models_file, monkeypatch, capsys, live
):
    if live:
        monkeypatch.setenv("FORCE_COLOR", "1")
    assert main(["run", "local-llm", "basal-mini", "--game", "bridge", "--games", "3"]) == 2
    out = capsys.readouterr().out
    assert "bridge plays every start twice" in out and "Starting..." not in out
    assert saved_runs(models_file) == []


def test_run_lets_any_other_error_building_the_competition_surface(scripted, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("a bug, not a setup rule")

    monkeypatch.setattr("ollmpiada.cli.Competition", broken)
    scripted.update({"local-llm": [], "basal-mini": []})
    with pytest.raises(ValueError, match="a bug, not a setup rule"):
        main(["run", "local-llm", "basal-mini"])


def test_show_rejects_a_missing_run(tmp_path, capsys):
    assert main(["show", str(tmp_path / "nothing")]) == 2
    assert "Cannot read" in capsys.readouterr().out


# --- live view ---


def play_into(view, tmp_path, first, second, games=2):
    players = (
        ScriptedPlayer("model-a", [WARM_UP, *first]),
        ScriptedPlayer("model-b", [WARM_UP, *second]),
    )
    competition = Competition(
        CHESS,
        players,
        games=games,
        max_turns=20,
        runs_dir=tmp_path / "runs",
        publish=view.handle,
    )
    return asyncio.run(competition.run())


def test_live_view_visual_mode_shows_board_moves_and_rejections(tmp_path):
    view = LiveView(CHESS, visual=True)
    assert render(view).strip() == "Starting..."
    play_into(view, tmp_path, ["f3", "Ke3", "g4"], ["e5", "Nxf7", "Qh4#"], games=1)
    text = render(view)
    assert "Games 1 of 1 | finished" in text
    lines = text.splitlines()
    moves = next(i for i, line in enumerate(lines) if "(1) f3" in line)
    assert lines[moves].strip() == (
        "(1) f3 e5 (2) [✗ White 1: 'Ke3' is not a legal move in this position] g4 "
        "[✗ Black 1: 'Nxf7' is not a legal move in this position] Qh4#"
    )
    assert lines[moves + 1].startswith("8 ") and "♛" in text and "a  b  c" in text
    assert "White" in text and "Black" in text and "checkmate" in text and "Statistics" in text


def test_live_view_shows_the_latest_moves_and_keeps_the_board_in_place(tmp_path):
    view = LiveView(CHESS, visual=True)
    play_into(view, tmp_path, [RESIGN], [], games=1)
    view.handle(GameStarted(2, ("model-a", "model-b"), chess.Board()))

    def board_row():
        return next(i for i, line in enumerate(render(view).splitlines()) if line.startswith("8 "))

    empty_list_row = board_row()
    board = chess.Board()
    for turn in range(120):
        side, move = (Side.FIRST, "Nf3") if turn % 2 == 0 else (Side.SECOND, "Nc6")
        # Null moves advance the side to move and the move number, which the move marks follow.
        board.push(chess.Move.null())
        view.handle(MoveMade(2, side, "model", move, board.copy(), draw_offered=False))
    text = render(view)
    assert board_row() == empty_list_row
    assert "(60) Nf3 Nc6" in text and "(1) " not in text


def test_live_view_marks_the_move_list_where_the_game_says(tmp_path):
    view = LiveView(TurnBasedMaster(MovesOnly()), visual=True)
    players = (
        ScriptedPlayer("model-a", [WARM_UP, "a", "a", "b"]),
        ScriptedPlayer("model-b", [WARM_UP, "b", "x", "a", "b"]),
    )
    competition = Competition(
        view.master, players, games=1, max_turns=None, runs_dir=tmp_path, publish=view.handle
    )
    asyncio.run(competition.run())
    assert "[0] a b a [3] [✗ East 1: 'x' is not a or b] a b b" in render(view)


def test_live_view_background_mode_has_no_board(tmp_path):
    view = LiveView(CHESS, visual=False)
    play_into(view, tmp_path, FIRST_SCRIPT, SECOND_SCRIPT)
    text = render(view)
    assert "♛" not in text and "(1) f3" not in text and "Requests" not in text
    assert "Games 2 of 2 | finished" in text and "resignation" in text


def test_live_view_status_shows_the_current_game_and_turn(tmp_path):
    view = LiveView(CHESS, visual=False)
    statuses = []
    handle = view.handle

    def handle_and_render(event):
        handle(event)
        statuses.append(render(view).splitlines()[0])

    view.handle = handle_and_render
    play_into(view, tmp_path, ["f3", "g4"], ["e5", "Qh4#"], games=1)
    assert "Games 0 of 1 | game 1, move 2 | running" in statuses


def test_statistics_never_show_unknown_tokens_as_zero(tmp_path):
    view = LiveView(CHESS, visual=False)
    no_usage = TurnResponse('{"action":"resign"}', RESIGN, 2.0, None)
    state = play_into(view, tmp_path, [no_usage], [], games=1)
    a_row = next(line for line in render(stats_table(state)).splitlines() if "model-a" in line)
    assert "unknown" in a_row and " 0 / " not in a_row


def test_unreported_reasoning_is_shown_as_unknown(tmp_path):
    view = LiveView(CHESS, visual=False)
    answer = TurnResponse('{"action":"resign"}', RESIGN, 2.0, None)
    state = play_into(view, tmp_path, [answer], [], games=1)
    stats = state.stats["model-a"]
    stats.calls_without_usage, stats.calls_without_reasoning = 0, 1
    stats.input_tokens, stats.output_tokens = 12_345, 67
    a_row = next(line for line in render(stats_table(state)).splitlines() if "model-a" in line)
    assert "12.3k / 67 / ?" in a_row


# --- game statistics under the board ---


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 14, 3, 12)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def row(lines: list[str], player: str) -> list[str]:
    """The player's row in the game statistics from its side name on, without the marker."""
    return next(
        cells for line in lines if (cells := line.replace("▶", "").split())[1:2] == [player]
    )


def thinker(lines: list[str]) -> str | None:
    """The player whose game statistics row carries the thinking marker."""
    return next((line.split()[2] for line in lines if line.split()[:1] == ["▶"]), None)


def started_game(tmp_path, clock):
    """A visual live view whose game 1 has just started, driven by events, not a competition."""
    players = (ScriptedPlayer("model-a", []), ScriptedPlayer("model-b", []))
    state = Competition(CHESS, players, games=1, max_turns=20, runs_dir=tmp_path / "runs").state
    view = LiveView(CHESS, visual=True, clock=clock)
    view.handle(CompetitionProgress(state))
    view.handle(GameStarted(1, ("model-a", "model-b"), chess.Board()))
    return view, state


def after(*moves: str) -> chess.Board:
    board = chess.Board()
    for move in moves:
        board.push_san(move)
    return board


def test_game_stats_show_timers_moves_thinker_times_legal_moves_and_usage(tmp_path):
    clock = FakeClock()
    view, _ = started_game(tmp_path, clock)
    clock.advance(5)
    view.handle(AnswerReceived(1, Side.FIRST, "model-a", 5.0, Usage(120, 30, 7)))
    view.handle(MoveMade(1, Side.FIRST, "model-a", "e4", after("e4"), draw_offered=False))
    clock.advance(7)
    lines = render(view).splitlines()
    board_bottom = next(i for i, line in enumerate(lines) if line.startswith("1 "))
    stats = next(i for i, line in enumerate(lines) if line.startswith("Started"))
    results = next(i for i, line in enumerate(lines) if "Results" in line)
    assert board_bottom < stats < results
    assert lines[stats] == "Started 14:03:12, running 0:12 | 1 move completed"
    assert lines[stats + 2].split()[:5] == ["Side", "Player", "Now", "Legal", "Total"]
    assert thinker(lines) == "model-b"
    # Only the thinking player shows its turn time and legal moves (20 for Black after 1. e4).
    assert row(lines, "model-a") == ["White", "model-a", "0:05", "1", "120", "30", "7"]
    assert row(lines, "model-b") == [
        "Black", "model-b", "0:07", "20", "0:07", "0", "–", "–", "–"
    ]  # fmt: skip

    clock.advance(60)
    lines = render(view).splitlines()
    assert "Started 14:03:12, running 1:12 | 1 move completed" in lines
    assert row(lines, "model-b")[2:5] == ["1:07", "20", "1:07"]


def test_game_stats_turn_timer_spans_retries_and_restarts_with_each_move(tmp_path):
    clock = FakeClock()
    view, _ = started_game(tmp_path, clock)
    view.handle(AnswerReceived(1, Side.FIRST, "model-a", 3.0, None))
    clock.advance(3)
    view.handle(AnswerReceived(1, Side.FIRST, "model-a", 4.0, Usage(100, 10, 0)))
    clock.advance(4)
    lines = render(view).splitlines()
    assert thinker(lines) == "model-a" and row(lines, "model-a")[2:5] == ["0:07", "20", "0:07"]
    view.handle(MoveMade(1, Side.FIRST, "model-a", "e4", after("e4"), draw_offered=False))
    clock.advance(2)
    view.handle(MoveMade(1, Side.SECOND, "model-b", "e5", after("e4", "e5"), draw_offered=False))
    clock.advance(1)
    lines = render(view).splitlines()
    assert any("2 moves completed" in line for line in lines)
    assert thinker(lines) == "model-a"
    # The total adds the running turn to the finished ones; the idle player has no turn time.
    assert row(lines, "model-a")[2:5] == ["0:01", "29", "0:08"]
    assert row(lines, "model-b")[2:3] == ["0:02"]
    # One of the two answers reported no usage, so the totals are lower bounds.
    assert row(lines, "model-a")[5:] == ["2", "≥", "100", "≥", "10", "≥", "0"]


def test_forced_move_without_a_request_is_listed_and_counted_as_a_move(tmp_path):
    view, _ = started_game(tmp_path, FakeClock())
    played = []
    for side, player, move in [(Side.FIRST, "model-a", "e4"), (Side.SECOND, "model-b", "f6"),
                               (Side.FIRST, "model-a", "Qh5+")]:  # fmt: skip
        played.append(move)
        view.handle(AnswerReceived(1, side, player, 1.0, Usage(100, 10, 0)))
        view.handle(MoveMade(1, side, player, move, after(*played), draw_offered=False))
    # The game master plays Black's only legal move itself: no AnswerReceived comes first.
    view.handle(MoveMade(1, Side.SECOND, "model-b", "g6", after(*played, "g6"), False))
    text = render(view)
    assert "(1) e4 f6 (2) Qh5+ g6" in text and "4 moves completed" in text
    assert row(text.splitlines(), "model-b")[3:5] == ["1", "100"]


def test_game_stats_never_show_unreported_reasoning_as_zero(tmp_path):
    view, _ = started_game(tmp_path, FakeClock())
    view.handle(AnswerReceived(1, Side.FIRST, "model-a", 1.0, Usage(100, 10, None)))
    assert row(render(view).splitlines(), "model-a")[-4:] == ["1", "100", "10", "–"]


def test_game_stats_freeze_when_the_game_ends(tmp_path):
    clock = FakeClock()
    view = LiveView(CHESS, visual=True, clock=clock)
    play_into(view, tmp_path, ["f3", "Ke3", "g4"], ["e5", "Qh4#"], games=1)
    clock.advance(30)
    lines = render(view).splitlines()
    assert "Started 14:03:12, ended after 0:00 | 4 moves completed" in lines
    assert thinker(lines) is None
    assert row(lines, "model-a") == ["White", "model-a", "0:00", "3", "300", "30", "0"]
    assert row(lines, "model-b") == ["Black", "model-b", "0:00", "2", "200", "20", "0"]


def test_game_stats_follow_the_side_swap_in_the_next_game(tmp_path):
    view = LiveView(CHESS, visual=True, clock=FakeClock())
    play_into(view, tmp_path, FIRST_SCRIPT, SECOND_SCRIPT)
    lines = render(view).splitlines()
    assert any("1 move completed" in line for line in lines)
    assert row(lines, "model-b")[:2] == ["White", "model-b"]
    assert row(lines, "model-a")[:2] == ["Black", "model-a"]


def test_game_stats_of_a_stopped_game_stop_ticking(tmp_path):
    clock = FakeClock()
    view, state = started_game(tmp_path, clock)
    clock.advance(9)
    state.status = "stopped"
    view.handle(CompetitionFinished(state))
    clock.advance(30)
    lines = render(view).splitlines()
    assert "Started 14:03:12, ended after 0:09 | 0 moves completed" in lines
    # The interrupted turn still counts towards the total of the player who was thinking.
    assert thinker(lines) is None and row(lines, "model-a")[2:3] == ["0:09"]
