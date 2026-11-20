import asyncio

import chess
import pytest

from ollmpiada.game_master import (
    AnswerRejected,
    GameFinished,
    GameStarted,
    MoveMade,
    play_game,
)
from ollmpiada.games.base import Outcome, Side
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import TransportError, TurnAnswer
from tests.fakes import ScriptedPlayer, unreadable

RESIGN = TurnAnswer(action="resign")
ACCEPT = TurnAnswer(action="accept_draw")


class ChessFrom(ChessGame):
    """Chess starting from a given position, to reach rare endings in a move or two."""

    def __init__(self, fen: str) -> None:
        self.fen = fen

    def new_state(self) -> chess.Board:
        return chess.Board(self.fen)


def offer(move: str) -> TurnAnswer:
    return TurnAnswer(action="move", move=move, offer_draw=True)


def run(white, black, *, game=None, max_turns=50, events=None):
    white = ScriptedPlayer("white-model", white)
    black = ScriptedPlayer("black-model", black)
    result = asyncio.run(
        play_game(
            game or ChessGame(),
            (white, black),
            max_turns=max_turns,
            number=3,
            publish=events.append if events is not None else lambda e: None,
            backoff_s=(0, 0),
        )
    )
    return result, white, black


# --- end of game: one test per row of docs/game-master.md#end-of-game ---


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
    assert "White offers a draw" in black.requests[0].prompt


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


def test_turn_limit_ends_after_both_sides_made_max_turns_moves():
    result, white, black = run(["e4", "Nf3"], ["e5", "Nc6"], max_turns=2)
    assert result.outcome == Outcome(None, "turn limit")
    assert len(result.turns) == 4
    assert "move 2 of 2 (1 left" in black.requests[1].prompt


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
    assert "PREVIOUS ATTEMPT" not in white.requests[0].prompt
    retry = white.requests[1].prompt
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


# --- events and records ---


def test_events_and_turn_records():
    events = []
    result, _, _ = run(["f3", "g4"], ["e5", "Qh4#"], events=events)
    kinds = [type(e) for e in events]
    assert kinds == [GameStarted, MoveMade, MoveMade, MoveMade, MoveMade, GameFinished]
    assert events[0].players == ("white-model", "black-model") and events[0].number == 3
    last = events[4]
    assert (last.side, last.player, last.move) == (Side.SECOND, "black-model", "Qh4#")
    assert last.state.is_checkmate()
    assert result.state.is_checkmate()
    attempt = result.turns[0].attempts[0]
    assert attempt.elapsed_s == 1.0 and attempt.usage.input_tokens == 100
