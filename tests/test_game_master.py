import asyncio

import chess
import pytest
from chess.engine import Cp, EngineTerminatedError, Mate

from ollmpiada.game_master import (
    AnswerReceived,
    AnswerRejected,
    GameFinished,
    GameStarted,
    MoveMade,
    TurnBasedMaster,
)
from ollmpiada.games.base import OPENING_MOVES, Outcome, Side
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import PermanentError, TransportError, TurnAnswer
from tests.fakes import USAGE, Judged, MovesOnly, ScriptedPlayer, SeededStart, unreadable

RESIGN = TurnAnswer(action="resign")
ACCEPT = TurnAnswer(action="accept_draw")


class ChessFrom(ChessGame):
    """Chess starting from a given position, to reach rare endings in a move or two."""

    def __init__(self, fen: str) -> None:
        super().__init__()
        self.fen = fen

    def new_state(self, seed: int) -> chess.Board:
        return chess.Board(self.fen)


def offer(move: str) -> TurnAnswer:
    return TurnAnswer(action="move", move=move, offer_draw=True)


def run(white, black, *, game=None, max_turns=50, events=None):
    white = ScriptedPlayer("white-model", white)
    black = ScriptedPlayer("black-model", black)
    result = asyncio.run(
        TurnBasedMaster(game or ChessGame()).play(
            (white, black),
            max_turns=max_turns,
            number=3,
            publish=events.append if events is not None else lambda e: None,
            backoff_s=(0, 0),
        )
    )
    return result, white, black


def test_turn_request_carries_the_legal_moves_and_their_groups():
    _, white, _ = run([RESIGN], [])
    request = white.requests[0]
    state = chess.Board()
    assert request.legal_moves == ChessGame().legal_moves(state)
    assert request.move_groups == ChessGame().move_groups(state)


def test_a_game_starts_from_the_state_of_its_seed():
    white = ScriptedPlayer("white-model", [])
    black = ScriptedPlayer("black-model", [RESIGN])
    master = TurnBasedMaster(SeededStart())
    result = asyncio.run(master.play((white, black), max_turns=50, seed=3, backoff_s=(0,)))
    assert result.outcome == Outcome(Side.FIRST, "resignation")
    assert (len(white.requests), len(black.requests)) == (0, 1)


def test_start_requests_use_the_start_of_seed_0():
    request = TurnBasedMaster(SeededStart()).start_request(max_turns=50)
    assert "as White" in request.prompt.system


# --- end of game: one test per row of docs/game-master/results.md#end-of-game ---


def test_checkmate_wins_for_the_mating_side():
    result, _, _ = run(["f3", "g4"], ["e5", "Qh4#"])
    assert result.outcome == Outcome(Side.SECOND, "checkmate")
    assert [t.move for t in result.turns] == ["f3", "e5", "g4", "Qh4#"]


def test_resignation_wins_for_the_opponent():
    result, _, black = run(["e4", RESIGN], ["e5"])
    assert result.outcome == Outcome(Side.SECOND, "resignation")
    assert result.turns[-1].action == "resign"


def test_accepted_draw_offer_is_a_draw():
    events = []
    result, _, black = run([offer("e4")], [ACCEPT], events=events)
    moves = [e for e in events if isinstance(e, MoveMade)]
    assert [m.draw_offered for m in moves] == [True]
    assert result.outcome == Outcome(None, "draw agreed")
    assert black.requests[0].draw_offered
    assert "White offers a draw" in black.requests[0].prompt.user


@pytest.mark.parametrize(
    ("fen", "move", "reason"),
    [
        ("7k/8/6K1/5Q2/8/8/8/8 w - - 0 1", "Qf7", "stalemate"),
        ("8/8/8/4k3/8/8/3p4/4K3 w - - 0 1", "Kxd2", "insufficient material"),
        ("8/8/8/4k3/8/8/8/R3K3 w - - 149 120", "Ra2", "75-move rule"),
    ],
)
def test_automatic_draws(fen, move, reason):
    result, _, _ = run([move], [], game=ChessFrom(fen))
    assert result.outcome == Outcome(None, reason)


def test_threefold_repetition_is_claimed():
    result, _, _ = run(["Nf3", "Ng1", "Nf3", "Ng1"], ["Nf6", "Ng8", "Nf6", "Ng8"])
    assert result.outcome == Outcome(None, "threefold repetition")
    assert len(result.turns) == 8


def test_fifty_move_rule_is_claimed():
    result, _, _ = run(["Ra2"], [], game=ChessFrom("8/8/8/4k3/8/8/8/R3K3 w - - 99 80"))
    assert result.outcome == Outcome(None, "50-move rule")


@pytest.mark.parametrize(
    ("score", "outcome"),
    [
        (Cp(500), Outcome(Side.FIRST, "adjudication", "+500 cp")),
        (Cp(-731), Outcome(Side.SECOND, "adjudication", "-731 cp")),
        (Mate(3), Outcome(Side.FIRST, "adjudication", "#+3")),
        (Mate(-1), Outcome(Side.SECOND, "adjudication", "#-1")),
        (Cp(499), Outcome(None, "adjudication", "+499 cp")),
        (Cp(-499), Outcome(None, "adjudication", "-499 cp")),
    ],
)
def test_turn_limit_adjudicates_after_both_sides_made_max_turns_moves(score, outcome):
    game = Judged(score)
    result, _, black = run(["e4", "Nf3"], ["e5", "Nc6"], game=game, max_turns=2)
    assert result.outcome == outcome and result.outcome.adjudicated
    assert len(result.turns) == 4
    assert "move 2 of 2 (1 left" in black.requests[1].prompt.user
    assert [board.fen() for board in game.evaluated] == [result.state.fen()]


class FailingEngine:
    """Stands in for a Stockfish process that fails while analysing."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def analyse(self, board, limit):
        raise self.error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            EngineTerminatedError("engine process died unexpectedly"),
            "Stockfish failed to adjudicate the game: engine process died unexpectedly",
        ),
        # What SimpleEngine raises when a hung engine outlasts the limit's time cap.
        (TimeoutError(), "Stockfish stopped responding while adjudicating"),
    ],
)
def test_engine_failure_at_the_turn_limit_aborts_the_game_with_its_moves(error, message):
    game = ChessGame()
    game._engine = FailingEngine(error)
    events = []
    result, _, _ = run(["e4"], ["e5"], game=game, max_turns=1, events=events)
    assert result.outcome is None
    assert result.error == message
    assert [t.move for t in result.turns] == ["e4", "e5"]
    assert [move.uci() for move in result.state.move_stack] == ["e2e4", "e7e5"]
    assert isinstance(events[-1], GameFinished) and events[-1].result is result


def test_no_adjudication_before_the_turn_limit():
    game = Judged(Cp(900))
    result, _, _ = run(["e4", RESIGN], ["e5"], game=game, max_turns=2)
    assert result.outcome == Outcome(Side.SECOND, "resignation")
    assert not result.outcome.adjudicated and game.evaluated == []


def test_game_ending_on_the_board_at_the_turn_limit_is_not_adjudicated():
    game = Judged(Cp(900))
    result, _, _ = run(["f3", "g4"], ["e5", "Qh4#"], game=game, max_turns=2)
    assert result.outcome == Outcome(Side.SECOND, "checkmate")
    assert game.evaluated == []


def test_third_rejected_answer_loses():
    result, white, _ = run(["e5", "Ke2", unreadable()], [])
    assert result.outcome == Outcome(Side.SECOND, "illegal answers")
    turn = result.turns[0]
    assert turn.action == "illegal" and len(turn.attempts) == 3
    assert all(a.rejection for a in turn.attempts)


# --- validation and retries ---


def test_rejection_reason_is_fed_back_and_the_retry_counts():
    events = []
    result, white, _ = run(["e5", "e4", RESIGN], ["e5"], events=events)
    assert "PREVIOUS ATTEMPT" not in white.requests[0].prompt.user
    retry = white.requests[1].prompt.user
    assert "PREVIOUS ATTEMPT" in retry and "'e5' is not a legal move" in retry
    assert '"move":"e5"' in retry
    first = result.turns[0]
    assert first.move == "e4"
    assert [a.rejection is None for a in first.attempts] == [False, True]
    rejected = [e for e in events if isinstance(e, AnswerRejected)]
    assert [(r.side, r.attempt) for r in rejected] == [(Side.FIRST, 1)]
    assert rejected[0].raw == '{"action":"move","move":"e5","offer_draw":false}'
    assert rejected[0].reason == "'e5' is not a legal move in this position"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (unreadable(), "does not match the answer format"),
        (TurnAnswer(action="move"), "no move was given"),
        (ACCEPT, "no draw offer to accept"),
        ("Nf6", "not a legal move"),
    ],
)
def test_invalid_answers_are_rejected(answer, reason):
    result, _, _ = run([answer, "e4", RESIGN], ["e5"])
    assert reason in result.turns[0].attempts[0].rejection


def test_draw_offer_is_declined_by_moving_and_expires():
    result, white, black = run([offer("e4"), "Nf3", ACCEPT, RESIGN], ["e5", "Nc6"])
    assert black.requests[0].draw_offered
    assert not white.requests[1].draw_offered
    assert not black.requests[1].draw_offered
    assert "no draw offer to accept" in result.turns[4].attempts[0].rejection
    assert result.outcome == Outcome(Side.SECOND, "resignation")


def test_transport_errors_are_retried_without_counting_as_attempts():
    error = TransportError("timeout")
    result, white, _ = run([error, error, "e4", RESIGN], ["e5"])
    assert result.turns[0].move == "e4" and len(result.turns[0].attempts) == 1
    assert result.outcome == Outcome(Side.SECOND, "resignation")


def test_abort_keeps_the_attempts_of_the_unfinished_turn():
    error = TransportError("down")
    result, _, _ = run(["Ke2", error, error, error], [])
    assert result.outcome is None
    assert [(t.action, len(t.attempts)) for t in result.turns] == [("aborted", 1)]


def test_third_transport_failure_aborts_the_game():
    events = []
    error = TransportError("connection refused")
    result, _, black = run(["e4", "Nf3"], ["e5", error, error, error], events=events)
    assert result.outcome is None
    assert result.error == "black-model: connection refused"
    assert len(result.turns) == 3 and len(black.requests) == 4
    assert isinstance(events[-1], GameFinished) and events[-1].result is result


def test_permanent_error_aborts_the_game_at_once_without_retries(caplog):
    error = PermanentError("30 pieces to choose from exceed max_options 26 of 'basal'")
    result, _, black = run(["e4"], [error])
    assert result.outcome is None
    assert result.error == f"black-model: {error}"
    assert len(black.requests) == 1
    assert "retrying" not in caplog.text


# --- events and records ---


def test_events_and_turn_records():
    events = []
    result, _, _ = run(["f3", "g4"], ["e5", "Qh4#"], events=events)
    kinds = [type(e) for e in events]
    answered = [AnswerReceived, MoveMade]
    assert kinds == [GameStarted, *answered * 4, GameFinished]
    assert events[0].players == ("white-model", "black-model") and events[0].number == 3
    last = events[8]
    assert (last.side, last.player, last.move) == (Side.SECOND, "black-model", "Qh4#")
    assert last.state.is_checkmate()
    assert result.state.is_checkmate()
    attempt = result.turns[0].attempts[0]
    assert attempt.elapsed_s == 1.0 and attempt.usage.input_tokens == 100


def test_every_answer_is_published_with_its_usage_before_validation():
    events = []
    error = TransportError("timeout")
    run([error, "Ke2", unreadable(), RESIGN], [], events=events)
    answers = [e for e in events if isinstance(e, AnswerReceived | AnswerRejected)]
    assert [type(e) for e in answers] == [AnswerReceived, AnswerRejected] * 2 + [AnswerReceived]
    received = answers[0]
    assert (received.number, received.side, received.player) == (3, Side.FIRST, "white-model")
    assert (received.elapsed_s, received.usage) == (1.0, USAGE)


# --- the opening: only moves during each side's first OPENING_MOVES moves ---

OPENING = (["e4", "Nf3", "Bc4", "Nc3", "d3"], ["e5", "Nc6", "Bc5", "Nf6", "d6"])


@pytest.mark.opening
def test_opening_turns_are_move_only_for_both_sides_and_later_turns_are_not():
    white_moves, black_moves = OPENING
    result, white, black = run([*white_moves, offer("O-O")], [*black_moves, ACCEPT])
    assert result.outcome == Outcome(None, "draw agreed")
    assert len(result.turns) == 2 * OPENING_MOVES + 2
    assert [r.move_only for r in white.requests] == [True] * OPENING_MOVES + [False]
    assert [r.move_only for r in black.requests] == [True] * OPENING_MOVES + [False]
    assert "Choose exactly one move" in black.requests[-2].prompt.user
    assert "Unless you resign or accept a draw" in white.requests[-1].prompt.user


@pytest.mark.opening
@pytest.mark.parametrize("early", [RESIGN, offer("e4")], ids=["resign", "draw-offer"])
def test_resignation_or_draw_offer_in_the_opening_is_rejected(early):
    result, white, _ = run([early, "e4", RESIGN, RESIGN, RESIGN], ["e5"])
    first = result.turns[0]
    assert first.move == "e4"
    assert first.attempts[0].rejection == (
        f"resigning and draw offers start with your move {OPENING_MOVES + 1}; play a move"
    )
    assert "PREVIOUS ATTEMPT" in white.requests[1].prompt.user
    # A player that keeps resigning in the opening loses on illegal answers, never by resigning.
    assert result.outcome == Outcome(Side.SECOND, "illegal answers")


@pytest.mark.opening
def test_start_request_is_move_only_by_default_and_full_after_the_opening():
    """The warm-up primes the opening turns; the probe also checks a turn after them."""
    request = TurnBasedMaster(ChessGame()).start_request(max_turns=40)
    assert request.move_only and not request.draw_offered
    assert "Choose exactly one move" in request.prompt.user
    later = TurnBasedMaster(ChessGame()).start_request(max_turns=40, turn=OPENING_MOVES + 1)
    assert not later.move_only and len(later.legal_moves) == 20
    assert "Unless you resign or accept a draw" in later.prompt.user


# 1. e4 f6 2. Qh5+ leaves Black one legal move, g6, on its second move.
FORCED_WHITE, FORCED_BLACK = ["e4", "Qh5+"], ["f6"]


@pytest.mark.opening
def test_forced_opening_move_is_played_by_the_game_master_without_asking_the_player():
    events = []
    result, _, black = run(
        FORCED_WHITE, FORCED_BLACK, game=Judged(Cp(0)), max_turns=2, events=events
    )
    assert len(black.requests) == 1
    forced = result.turns[3]
    assert (forced.side, forced.action, forced.move, forced.attempts) == (
        Side.SECOND,
        "move",
        "g6",
        [],
    )
    black_events = [type(e) for e in events if getattr(e, "player", None) == "black-model"]
    assert black_events == [AnswerReceived, MoveMade, MoveMade]
    assert result.outcome == Outcome(None, "adjudication", "0 cp")


@pytest.mark.opening
def test_forced_move_after_the_opening_is_still_asked(monkeypatch):
    monkeypatch.setattr("ollmpiada.games.base.OPENING_MOVES", 1)
    result, _, black = run(FORCED_WHITE, [*FORCED_BLACK, RESIGN], game=Judged(Cp(0)), max_turns=2)
    forced = black.requests[1]
    assert len(forced.legal_moves) == 1 and not forced.move_only
    assert result.outcome == Outcome(Side.FIRST, "resignation")


# --- game hooks: a game of moves alone (tests.fakes.MovesOnly) ---


def test_a_move_only_game_never_offers_resigning_or_a_draw():
    game = MovesOnly()
    result, first, second = run(["a", RESIGN, offer("a"), "a", "b"], ["b", "a", "b"], game=game)
    assert all(r.move_only for r in first.requests + second.requests)
    rejections = [a.rejection for a in result.turns[2].attempts[:2]]
    # In the game's own words, so nothing about moves reaches a game that has none.
    assert rejections == ["move_only: play a letter"] * 2
    assert [t.move for t in result.turns] == ["a", "b", "a", "a", "b", "b"]
    assert result.outcome == Outcome(Side.SECOND, "count")


class Noted(MovesOnly):
    user_template = "$position; $note"

    def prompt_values(self, ctx):
        return {"note": f"move-only turn {ctx.turn}" if ctx.move_only else "full turn"}


def test_a_game_adds_its_own_prompt_values_to_the_shared_ones():
    game = Noted()
    ctx = game.context(game.new_state(0), turn=3, max_turns=None)
    prompt = game.prompt(ctx)
    assert prompt.user == "Played: nothing; move-only turn 3\n"
    assert (prompt.answer_format, prompt.questions) == ("Answer with a letter.\n", game.questions)


@pytest.mark.parametrize("placeholder", ["choose_move", "previous_attempt"])
def test_no_wording_is_shared_between_games(placeholder):
    """Neither chess wording nor the retry sentence comes from the shared code: a game's
    template can use only the values every game has and its own `prompt_values`."""
    game = Noted()
    game.user_template = f"${placeholder}"
    with pytest.raises(KeyError, match=placeholder):
        game.prompt(game.context(game.new_state(0), turn=1, max_turns=None))


@pytest.mark.parametrize("max_turns", [None, 1])
def test_a_game_without_a_turn_limit_is_never_adjudicated(max_turns):
    result, _, _ = run(["a", "a", "a"], ["a", "b", "b"], game=MovesOnly(), max_turns=max_turns)
    assert result.outcome == Outcome(Side.FIRST, "count") and result.error is None
    assert len(result.turns) == MovesOnly.LENGTH


def test_third_rejected_answer_forfeits_through_the_game():
    game = MovesOnly()
    result, _, _ = run(["a", "x", "y", "z"], ["b"], game=game)
    assert game.forfeits == [(("a", "b"), Side.FIRST, "illegal answers")]
    assert result.outcome == Outcome(Side.SECOND, "illegal answers")
