import re

import chess
import pytest
from rich.console import Console

from ollmpiada.games import GAMES
from ollmpiada.games.base import IllegalMove, Outcome, PromptContext, Side
from ollmpiada.games.chess_game import DARK_LAST, LIGHT_LAST

game = GAMES["chess"]


def play(*moves: str) -> chess.Board:
    state = game.new_state()
    for move in moves:
        state = game.apply(state, game.parse_move(state, move))
    return state


def test_registry_and_side_names():
    assert game.name == "chess"
    assert game.side_names == ("White", "Black")
    assert game.side_name(Side.SECOND) == "Black"


def test_opening_position():
    state = game.new_state()
    assert game.side_to_move(state) is Side.FIRST
    legal = game.legal_moves(state)
    assert len(legal) == 20
    assert legal["g1f3"] == "Nf3"
    assert game.outcome(state) is None


def test_apply_returns_a_new_state():
    state = game.new_state()
    after = game.apply(state, "e2e4")
    assert state.fen() == chess.STARTING_FEN
    assert game.side_to_move(after) is Side.SECOND


def test_checkmate_wins_for_the_mating_side():
    assert game.outcome(play("f3", "e5", "g4", "Qh4#")) == Outcome(Side.SECOND, "checkmate")


def test_stalemate_is_a_draw():
    state = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert game.outcome(state) == Outcome(None, "stalemate")


def test_threefold_repetition_is_claimed_automatically():
    shuffle = ["Nf3", "Nf6", "Ng1", "Ng8"]
    almost = play(*shuffle, *shuffle[:3])
    assert game.outcome(almost) is None
    assert game.outcome(game.apply(almost, "f6g8")) == Outcome(None, "threefold repetition")


def test_fifty_move_rule_is_claimed_automatically():
    state = chess.Board("8/8/8/4k3/8/8/8/R3K3 w - - 100 80")
    assert game.outcome(state) == Outcome(None, "50-move rule")


def test_insufficient_material_is_a_draw():
    state = chess.Board("8/8/8/4k3/8/8/8/4K3 w - - 0 1")
    assert game.outcome(state) == Outcome(None, "insufficient material")


@pytest.mark.parametrize(
    ("text", "move_id"),
    [
        ("Nf3", "g1f3"),
        ("g1f3", "g1f3"),
        ("G1F3", "g1f3"),
        ("nf3", "g1f3"),
        ("NF3", "g1f3"),
        ("  Nf3+ ", "g1f3"),
        ("Nf3!?", "g1f3"),
        ("e4", "e2e4"),
    ],
)
def test_parse_opening_move_variants(text, move_id):
    assert game.parse_move(game.new_state(), text) == move_id


@pytest.mark.parametrize("text", ["e8=Q+", "e8=Q", "e8=q", "e8Q", "e7e8q", "E7E8Q"])
def test_parse_promotion_variants(text):
    state = chess.Board("8/4P3/8/8/8/8/k7/4K3 w - - 0 1")
    assert game.parse_move(state, text) == "e7e8q"


@pytest.mark.parametrize("text", ["O-O", "0-0", "o-o"])
def test_parse_castling_variants(text):
    state = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert game.parse_move(state, text) == "e1g1"


def test_case_decides_between_pawn_and_bishop_captures():
    state = chess.Board("4k3/8/8/8/2p5/1P6/4B3/4K3 w - - 0 1")
    assert game.parse_move(state, "bxc4") == "b3c4"
    assert game.parse_move(state, "Bxc4") == "e2c4"
    with pytest.raises(IllegalMove, match="ambiguous: it could be Bxc4 or bxc4"):
        game.parse_move(state, "BXC4")


@pytest.mark.parametrize(
    ("fen", "text", "readings"),
    [
        ("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1", "Nd2", "Nbd2 or Nfd2"),
        ("4k3/8/8/8/8/8/4K3/R6R w - - 0 1", "Rf1", "Raf1 or Rhf1"),
    ],
)
def test_under_specified_san_is_rejected_as_ambiguous(fen, text, readings):
    with pytest.raises(IllegalMove, match=f"ambiguous: it could be {readings}"):
        game.parse_move(chess.Board(fen), text)


def test_disambiguated_san_is_accepted():
    state = chess.Board("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
    assert game.parse_move(state, "Nbd2") == "b1d2"
    assert game.parse_move(state, "nfd2") == "f1d2"


@pytest.mark.parametrize("text", ["e5", "e2e5", "Ke2", "", "--", "0000", "resign"])
def test_illegal_or_unreadable_moves_are_rejected(text):
    with pytest.raises(IllegalMove, match="not a legal move"):
        game.parse_move(game.new_state(), text)


def test_describe_has_fen_board_with_coordinates_history_and_check():
    text = game.describe(play("e4", "f5", "Qh5+"))
    assert "FEN: rnbqkbnr/ppppp1pp/8/5p1Q/4P3/8/PPPP1PPP/RNB1KBNR b KQkq - 1 2" in text
    assert "8 | r n b q k b n r" in text
    assert "5 | . . . . . p . Q" in text
    assert "    a b c d e f g h" in text
    assert "Move history: 1. e4 f5 2. Qh5+" in text
    assert "Black to move" in text and "Black is in check." in text
    assert "Last move: White played Qh5+." in text
    assert "Last move" not in game.describe(game.new_state())


def test_render_shows_pieces_coordinates_and_highlights_last_move():
    board = game.render(play("e4"))
    console = Console(record=True, width=40)
    console.print(board)
    lines = console.export_text().splitlines()
    assert lines[0].startswith("8  ♜  ♞")
    assert lines[-1].split() == list("abcdefgh")
    styles = [str(span.style) for span in board.spans]
    assert sum(LIGHT_LAST in style or DARK_LAST in style for style in styles) == 2


def test_record_is_pgn_with_players_result_and_reason():
    state = play("f3", "e5", "g4", "Qh4#")
    pgn = game.record(state, game.outcome(state), ("model-a", "model-b@high"))
    assert game.record_extension == "pgn"
    assert '[White "model-a"]' in pgn and '[Black "model-b@high"]' in pgn
    assert '[Result "0-1"]' in pgn and '[Termination "checkmate"]' in pgn
    assert pgn.strip().endswith("1. f3 e5 2. g4 Qh4# 0-1")


def test_record_of_resigned_and_unfinished_games():
    state = play("e4")
    assert '[Result "1-0"]' in game.record(state, Outcome(Side.FIRST, "resignation"), ("a", "b"))
    assert '[Result "*"]' in game.record(state, None, ("a", "b"))


def test_prompt_differs_between_sides_only_in_side_words():
    state = play("e4", "e5")
    white = PromptContext(
        side=Side.FIRST,
        position=game.describe(state),
        legal_moves=list(game.legal_moves(state).values()),
        turn=2,
        max_turns=40,
        draw_offered=True,
        rejection='"Ke3" is not a legal move in this position',
    )
    black = PromptContext(**{**white.__dict__, "side": Side.SECOND})
    white_words = re.findall(r"\w+", game.prompt(white))
    black_words = re.findall(r"\w+", game.prompt(black))
    assert len(white_words) == len(black_words)
    differing = {(w, b) for w, b in zip(white_words, black_words, strict=True) if w != b}
    assert differing == {("White", "Black"), ("Black", "White")}


def test_prompt_sections_and_optional_parts():
    state = game.new_state()
    ctx = PromptContext(
        side=Side.FIRST,
        position=game.describe(state),
        legal_moves=list(game.legal_moves(state).values()),
        turn=1,
        max_turns=40,
    )
    text = game.prompt(ctx)
    headings = ["ROLE AND GOAL", "RULES", "POSITION", "LEGAL MOVES"]
    assert [text.index(h) for h in headings] == sorted(text.index(h) for h in headings)
    assert "playing a game of chess as White" in text and "win" in text
    assert "move 1 of 40 (40 left" in text
    assert "Nh3, Nf3" in text
    assert "No draw offer is pending." in text
    assert "PREVIOUS ATTEMPT" not in text and "$" not in text

    retry = game.prompt(PromptContext(**{**ctx.__dict__, "rejection": "illegal move Ke2"}))
    assert retry.rstrip().endswith("Answer again with a legal move.")
    assert "PREVIOUS ATTEMPT" in retry and "illegal move Ke2" in retry
