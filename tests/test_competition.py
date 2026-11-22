import asyncio
import dataclasses
import json

import pytest
from chess.engine import Cp, Mate

from ollmpiada.competition import (
    Competition,
    CompetitionFinished,
    CompetitionProgress,
    load_state,
    prompt_version,
)
from ollmpiada.game_master import AnswerReceived, GameStarted, MoveMade
from ollmpiada.games.base import Prompt
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import TransportError, TurnAnswer, TurnResponse, Usage
from tests.fakes import WARM_UP, Judged, ScriptedPlayer

RESIGN = TurnAnswer(action="resign")


def compete(a_script, b_script, tmp_path, **kwargs):
    """Run a competition of scripted players, each answering the warm-up call first."""
    a = ScriptedPlayer("model-a@high", [WARM_UP, *a_script])
    b = ScriptedPlayer("model-b", [WARM_UP, *b_script])
    return run((a, b), tmp_path, **kwargs)


def run(players, tmp_path, *, games=2, on_event=None, game=None, max_turns=20):
    events = []
    competition = None

    def publish(event):
        events.append(event)
        if on_event:
            on_event(competition, event)

    competition = Competition(
        game or ChessGame(),
        players,
        games=games,
        max_turns=max_turns,
        runs_dir=tmp_path / ".local" / "runs",
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
    assert run_dir.parent == tmp_path / ".local" / "runs"
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


def test_adjudicated_results_are_counted_apart_from_results_on_the_board(tmp_path):
    # One move per side: a wins game 1 as White (+6 pawns) and game 2 as Black (mated
    # White), game 3 is under the threshold; in game 4 b resigns as White on the board.
    game = Judged(Cp(600), Mate(-2), Cp(120))
    state, competition, _ = compete(
        ["e4", "e5", "e4"], ["e5", "e4", "e5", RESIGN], tmp_path, games=4, game=game, max_turns=1
    )
    assert [(r.result, r.reason, r.score) for r in state.results] == [
        ("1-0", "adjudication", "+600 cp"),
        ("0-1", "adjudication", "#-2"),
        ("1/2-1/2", "adjudication", "+120 cp"),
        ("0-1", "resignation", None),
    ]
    buckets = ("wins", "losses", "draws", "adjudicated_wins", "adjudicated_losses",
               "adjudicated_draws")  # fmt: skip
    a, b = state.stats["model-a@high"], state.stats["model-b"]
    assert [getattr(a, k) for k in buckets] == [1, 0, 0, 2, 0, 1]
    assert [getattr(b, k) for k in buckets] == [0, 1, 0, 0, 2, 1]
    assert a.games == b.games == 4

    run_dir = competition.run_dir
    saved = read_json(run_dir / "game-001.json")
    assert (saved["reason"], saved["score"]) == ("adjudication", "+600 cp")
    assert read_json(run_dir / "competition.json")["stats"]["model-b"]["adjudicated_losses"] == 2
    pgn = (run_dir / "game-002.pgn").read_text()
    assert '[Termination "adjudication"]' in pgn
    assert pgn.strip().endswith("1. e4 e5 { Stockfish: #-2 } 0-1")
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


@pytest.mark.opening
def test_forced_opening_move_counts_as_a_move_without_a_call_and_is_saved(tmp_path):
    # 1. e4 f6 2. Qh5+ g6: the game master plays Black's only legal move, g6.
    state, competition, _ = compete(
        ["e4", "Qh5+"], ["f6"], tmp_path, games=1, game=Judged(Cp(0)), max_turns=2
    )
    b = state.stats["model-b"]
    assert (b.moves, b.calls, b.thinking_s, b.input_tokens) == (2, 1, 1.0, 100)
    assert b.calls_without_usage == 0 and b.tokens_known
    run_dir = competition.run_dir
    forced = read_json(run_dir / "game-001.json")["turns"][3]
    assert (forced["action"], forced["move"], forced["attempts"]) == ("move", "g6", [])
    assert "2. Qh5+ g6" in (run_dir / "game-001.pgn").read_text()
    assert load_state(run_dir) == state


def test_unreported_reasoning_tokens_are_counted_as_unknown(tmp_path):
    answer = TurnResponse('{"action":"resign"}', RESIGN, 2.0, Usage(40, 5, None))
    state, _, _ = compete([answer], [], tmp_path, games=1)
    a = state.stats["model-a@high"]
    assert (a.input_tokens, a.output_tokens, a.reasoning_tokens) == (40, 5, 0)
    assert a.tokens_known and a.calls_without_reasoning == 1


def test_an_existing_run_folder_is_not_reused(tmp_path):
    _, competition, _ = compete([RESIGN], [], tmp_path, games=1)
    with pytest.raises(FileExistsError):
        asyncio.run(competition.run())


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


def test_each_player_is_warmed_up_once_before_the_first_game(tmp_path):
    a = ScriptedPlayer("model-a", [WARM_UP, RESIGN, RESIGN])
    b = ScriptedPlayer("model-b", [WARM_UP, "e4"])
    seen = []

    def record(competition, event):
        status = competition.state.status
        seen.append((type(event).__name__, status, len(a.requests), len(b.requests)))

    state, _, _ = run((a, b), tmp_path, on_event=record)
    assert state.status == "finished"
    assert seen[:3] == [
        ("CompetitionProgress", "warming up", 0, 0),
        ("CompetitionProgress", "running", 1, 1),
        ("GameStarted", "running", 1, 1),
    ]
    assert (len(a.requests), len(b.requests)) == (3, 2)
    warm_up = a.requests[0]
    assert len(warm_up.legal_moves) == 20 and "move 1 of 20" in warm_up.prompt.user


def test_warm_up_retries_a_transport_failure_and_the_competition_proceeds(tmp_path):
    a = ScriptedPlayer("model-a", [TransportError("loading model"), WARM_UP, RESIGN])
    b = ScriptedPlayer("model-b", [WARM_UP])
    state, _, _ = run((a, b), tmp_path, games=1)
    assert state.status == "finished" and state.error is None
    assert state.results[0].reason == "resignation"


def test_a_failed_warm_up_stops_the_competition_before_any_game(tmp_path):
    error = TransportError("connection refused")
    a = ScriptedPlayer("model-a", [WARM_UP, RESIGN])
    b = ScriptedPlayer("model-b", [error, error, error])
    state, competition, events = run((a, b), tmp_path)
    assert state.status == "aborted"
    assert state.error == "warm-up call to model-b failed: connection refused"
    assert state.results == [] and state.stats["model-b"].games == 0
    assert not any(isinstance(e, GameStarted) for e in events)
    assert isinstance(events[-1], CompetitionFinished)
    assert read_json(competition.run_dir / "competition.json")["status"] == "aborted"
    # The second player warms up first, so the first one is never called.
    assert (len(a.requests), len(b.requests)) == (0, 3)


def test_warm_up_is_not_counted_in_statistics_or_events(tmp_path):
    slow = TurnResponse('{"action":"move","move":"e4"}', None, 60.0, Usage(5000, 900, 800))
    a = ScriptedPlayer("model-a", [slow, RESIGN])
    b = ScriptedPlayer("model-b", [slow])
    state, _, events = run((a, b), tmp_path, games=1)
    stats_a, stats_b = state.stats["model-a"], state.stats["model-b"]
    assert (stats_a.calls, stats_a.thinking_s, stats_a.input_tokens) == (1, 1.0, 100)
    assert (stats_a.output_tokens, stats_a.illegal_attempts) == (10, 0)
    assert (stats_b.calls, stats_b.thinking_s, stats_b.input_tokens) == (0, 0.0, 0)
    assert sum(isinstance(e, AnswerReceived) for e in events) == 1


def test_stop_during_warm_up_plays_no_game(tmp_path):
    competition = None

    class StopsWhenCalled(ScriptedPlayer):
        async def play(self, request):
            competition.stop()
            return await super().play(request)

    def keep(c, event):
        nonlocal competition
        competition = c

    players = (ScriptedPlayer("model-a", []), StopsWhenCalled("model-b", [WARM_UP]))
    state, _, events = run(players, tmp_path, on_event=keep)
    assert state.status == "stopped" and state.results == []
    assert not any(isinstance(e, GameStarted) for e in events)


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
    assert [len(e.state.results) for e in progress] == [2, 2, 2, 2]  # same live object
    assert isinstance(events[0], CompetitionProgress)
    assert isinstance(events[-1], CompetitionFinished)
    assert sum(isinstance(e, MoveMade) for e in events) == 5


@pytest.mark.parametrize(
    ("template", "old", "new"),
    [("system_template", "win", "triumph"), ("user_template", "Before answering", "First")],
)
def test_prompt_version_follows_the_wording_of_both_parts(template, old, new):
    game = ChessGame()
    version = prompt_version(game)
    assert len(version) == 12 and version == prompt_version(ChessGame())
    setattr(game, template, getattr(game, template).replace(old, new))
    assert prompt_version(game) != version


def test_prompt_version_covers_the_no_offer_wording(monkeypatch):
    version = prompt_version(ChessGame())
    original = ChessGame.prompt

    def reworded(self, ctx):
        prompt = original(self, ctx)
        user = prompt.user.replace("No draw offer is pending.", "No offer.")
        return dataclasses.replace(prompt, user=user)

    monkeypatch.setattr(ChessGame, "prompt", reworded)
    assert prompt_version(ChessGame()) != version


def test_prompt_version_tells_the_system_and_user_parts_apart(monkeypatch):
    original = ChessGame.prompt

    def version_with(extend):
        monkeypatch.setattr(ChessGame, "prompt", lambda self, ctx: extend(original(self, ctx)))
        return prompt_version(ChessGame())

    in_system = version_with(lambda p: Prompt(p.system + "Extra.\n", p.user))
    in_user = version_with(lambda p: Prompt(p.system, "Extra.\n" + p.user))
    assert in_system != in_user


def test_prompt_version_covers_the_llm_answer_format(monkeypatch):
    version = prompt_version(ChessGame())
    monkeypatch.setattr("ollmpiada.players.llm.ANSWER_FORMAT", "ANSWER IN JSON")
    assert prompt_version(ChessGame()) != version


@pytest.mark.opening
def test_prompt_version_covers_the_move_only_answer_format(monkeypatch):
    version = prompt_version(ChessGame())
    monkeypatch.setattr("ollmpiada.players.llm.MOVE_ONLY_ANSWER_FORMAT", "ANSWER WITH A MOVE")
    assert prompt_version(ChessGame()) != version


def test_first_selected_model_plays_first_side_in_odd_games(tmp_path):
    state, _, _ = compete([RESIGN, RESIGN, RESIGN], ["e4"], tmp_path, games=3)
    assert [r.players[0] for r in state.results] == ["model-a@high", "model-b", "model-a@high"]
