import asyncio
import json

import pytest

from ollmpiada.competition import (
    Competition,
    CompetitionFinished,
    CompetitionProgress,
    load_state,
    prompt_version,
)
from ollmpiada.game_master import GameStarted, MoveMade
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import TransportError, TurnAnswer, TurnResponse, Usage
from tests.fakes import ScriptedPlayer

RESIGN = TurnAnswer(action="resign")


def compete(a_script, b_script, tmp_path, *, games=2, on_event=None):
    a = ScriptedPlayer("model-a@high", a_script)
    b = ScriptedPlayer("model-b", b_script)
    events = []
    competition = None

    def publish(event):
        events.append(event)
        if on_event:
            on_event(competition, event)

    competition = Competition(
        ChessGame(),
        (a, b),
        games=games,
        max_turns=20,
        runs_dir=tmp_path / "runs",
        publish=publish,
        backoff_s=(0, 0),
    )
    state = asyncio.run(competition.run())
    return state, competition, events


def read_json(path):
    return json.loads(path.read_text())


def test_sides_alternate_and_statistics_add_up(tmp_path):
    # Game 1: a is White and gets mated; game 2: b is White, a resigns at once.
    state, _, _ = compete(["f3", "g4", RESIGN], ["e5", "Qh4#", "e4"], tmp_path)

    assert state.status == "finished"
    assert [r.players for r in state.results] == [
        ("model-a@high", "model-b"),
        ("model-b", "model-a@high"),
    ]
    assert [(r.result, r.winner, r.reason, r.moves) for r in state.results] == [
        ("0-1", "model-b", "checkmate", 4),
        ("1-0", "model-b", "resignation", 1),
    ]
    a, b = state.stats["model-a@high"], state.stats["model-b"]
    assert (a.games, a.wins, a.losses, a.draws) == (2, 0, 2, 0)
    assert (b.games, b.wins, b.losses, b.draws) == (2, 2, 0, 0)
    assert (a.moves, a.calls, a.thinking_s) == (2, 3, 3.0)
    assert (b.moves, b.calls, b.input_tokens, b.output_tokens) == (3, 3, 300, 30)
    assert a.tokens_known and a.illegal_attempts == 0


def test_results_are_saved_after_every_game(tmp_path):
    saved = []

    def check_files(competition, event):
        if isinstance(event, GameStarted) and event.number == 2:
            saved.append(read_json(competition.run_dir / "competition.json"))

    state, competition, _ = compete(
        ["f3", "g4", RESIGN], ["e5", "Qh4#", "e4"], tmp_path, on_event=check_files
    )
    run_dir = competition.run_dir
    assert run_dir.parent == tmp_path / "runs"
    assert run_dir.name.endswith("_model-a@high_vs_model-b")
    assert len(saved[0]["results"]) == 1 and saved[0]["status"] == "running"

    summary = read_json(run_dir / "competition.json")
    assert summary["status"] == "finished"
    assert summary["game"] == "chess" and summary["games"] == 2 and summary["max_turns"] == 20
    assert summary["prompt_version"] == prompt_version(ChessGame())
    assert summary["stats"]["model-b"]["wins"] == 2

    pgn = (run_dir / "game-001.pgn").read_text()
    assert '[White "model-a@high"]' in pgn and "Qh4# 0-1" in pgn
    game = read_json(run_dir / "game-001.json")
    assert game["reason"] == "checkmate" and game["side_names"] == ["White", "Black"]
    first = game["turns"][0]
    assert first["move"] == "f3" and first["attempts"][0]["elapsed_s"] == 1.0
    assert first["attempts"][0]["usage"]["input_tokens"] == 100
    assert (run_dir / "game-002.pgn").exists()
    assert load_state(run_dir) == state


def test_illegal_attempts_and_illegal_answer_losses_are_counted(tmp_path):
    state, _, _ = compete(["e5", "Ke2", "Qh5"], [], tmp_path, games=1)
    a = state.stats["model-a@high"]
    assert state.results[0].reason == "illegal answers"
    assert (a.losses, a.illegal_attempts, a.calls, a.moves) == (1, 3, 3, 0)
    assert a.thinking_s == 3.0


def test_unknown_usage_is_not_shown_as_zero(tmp_path):
    no_usage = TurnResponse('{"action":"resign"}', RESIGN, 2.0, None)
    state, _, _ = compete([no_usage], [], tmp_path, games=1)
    a = state.stats["model-a@high"]
    assert not a.tokens_known and a.calls_without_usage == 1


def test_unreported_reasoning_tokens_are_counted_as_unknown(tmp_path):
    answer = TurnResponse('{"action":"resign"}', RESIGN, 2.0, Usage(40, 5, None))
    state, _, _ = compete([answer], [], tmp_path, games=1)
    a = state.stats["model-a@high"]
    assert (a.input_tokens, a.output_tokens, a.reasoning_tokens) == (40, 5, 0)
    assert a.tokens_known and a.calls_without_reasoning == 1


def test_players_must_have_different_names(tmp_path):
    same = (ScriptedPlayer("x", []), ScriptedPlayer("x", []))
    with pytest.raises(ValueError, match="both players are named 'x'"):
        Competition(ChessGame(), same, games=1, max_turns=1, runs_dir=tmp_path)


def test_transport_failure_aborts_the_competition(tmp_path):
    error = TransportError("refused")
    state, competition, events = compete(["e4", "Nf3"], ["e5", error, error, error], tmp_path)
    assert state.status == "aborted" and state.error == "model-b: refused"
    assert [r.result for r in state.results] == ["aborted"]
    assert state.stats["model-a@high"].games == 0
    assert read_json(competition.run_dir / "competition.json")["status"] == "aborted"
    assert (competition.run_dir / "game-001.pgn").exists()
    assert isinstance(events[-1], CompetitionFinished)


def test_stop_aborts_the_game_in_progress_and_keeps_finished_games(tmp_path):
    def stop_in_game_two(competition, event):
        if isinstance(event, MoveMade) and event.number == 2:
            competition.stop()

    state, competition, events = compete(
        ["f3", "g4", "e5", "Nc6"], ["e5", "Qh4#", "e4", "Nf3"], tmp_path, on_event=stop_in_game_two
    )
    assert state.status == "stopped"
    assert len(state.results) == 1
    assert state.stats["model-b"].games == 1
    assert read_json(competition.run_dir / "competition.json")["status"] == "stopped"
    assert not (competition.run_dir / "game-002.json").exists()


def test_stop_between_games_plays_no_more(tmp_path):
    def stop_after_first(competition, event):
        if isinstance(event, CompetitionProgress) and event.state.results:
            competition.stop()

    state, _, _ = compete(
        ["f3", "g4"], ["e5", "Qh4#"], tmp_path, games=3, on_event=stop_after_first
    )
    assert state.status == "stopped" and len(state.results) == 1


def test_progress_events_carry_the_state(tmp_path):
    _, _, events = compete(["f3", "g4", RESIGN], ["e5", "Qh4#", "e4"], tmp_path)
    progress = [e for e in events if isinstance(e, CompetitionProgress)]
    assert [len(e.state.results) for e in progress] == [2, 2, 2]  # same live object
    assert isinstance(events[0], CompetitionProgress)
    assert isinstance(events[-1], CompetitionFinished)
    assert sum(isinstance(e, MoveMade) for e in events) == 5


def test_prompt_version_follows_the_wording():
    game = ChessGame()
    version = prompt_version(game)
    assert len(version) == 12 and version == prompt_version(ChessGame())
    game.prompt_template = game.prompt_template.replace("win", "triumph")
    assert prompt_version(game) != version


def test_prompt_version_covers_the_no_offer_wording(monkeypatch):
    version = prompt_version(ChessGame())
    original = ChessGame.prompt

    def reworded(self, ctx):
        return original(self, ctx).replace("No draw offer is pending.", "No offer.")

    monkeypatch.setattr(ChessGame, "prompt", reworded)
    assert prompt_version(ChessGame()) != version


def test_first_selected_model_plays_first_side_in_odd_games(tmp_path):
    state, _, _ = compete([RESIGN, RESIGN, RESIGN], ["e4"], tmp_path, games=3)
    assert [r.players[0] for r in state.results] == ["model-a@high", "model-b", "model-a@high"]
