import re
import shutil

import chess
import pytest
from rich.console import Console

from ollmpiada.config import load_settings
from ollmpiada.games import GAMES
from ollmpiada.games.base import GameSetupError, IllegalMove, Outcome, PromptContext, Side
from ollmpiada.games.chess_game import ADJUDICATION_WIN_CP, DARK_LAST, LIGHT_LAST

game = GAMES["chess"]
STOCKFISH = load_settings().stockfish


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


def test_move_groups_are_one_per_origin_square_named_by_piece():
    state = game.new_state()
    groups = game.move_groups(state)
    assert len(groups) == 10
    assert groups["knight_g1"] == {"g1f3": "Nf3", "g1h3": "Nh3"}
    assert groups["pawn_e2"] == {"e2e3": "e3", "e2e4": "e4"}
    # Castling is the king's move; every legal move is in exactly one group.
    state = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    groups = game.move_groups(state)
    assert {"e1g1": "O-O", "e1c1": "O-O-O"}.items() <= groups["king_e1"].items()
    merged = {move: label for moves in groups.values() for move, label in moves.items()}
    assert merged == game.legal_moves(state)
    assert sum(map(len, groups.values())) == len(merged)
    assert all(m[:2] == key[-2:] for key, moves in groups.items() for m in moves)


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
        ("Ng1-f3", "g1f3"),
        ("  Nf3+ ", "g1f3"),
        ("Nf3!?", "g1f3"),
        ("e4", "e2e4"),
    ],
)
def test_parse_opening_move_variants(text, move_id):
    assert game.parse_move(game.new_state(), text) == move_id


@pytest.mark.parametrize(
    "text",
    [
        "1. e4",
        "1.e4",
        "1... e4",
        "1. ...e4",
        "e4.",
        "e4,",
        "e4;",
        "e4\r\n",
        "e4\r",
        "e4\u00a0",
        "1.\u00a0e4",
        "e2\u00a0e4",
        " e4 ",
        "e4…",
        "**e4**",
        "`e4`",
        '"e4"',
        "'e4'",
        "(e4)",
        "**1. e4.**",
        "e2 e4",
        "E2 E4",
        "e2 - e4",
    ],
)
def test_parse_tolerates_move_numbers_wrappers_punctuation_and_split_squares(text):
    assert game.parse_move(game.new_state(), text) == "e2e4"


def test_parse_strips_a_move_number_before_san():
    state = play("e4", "e5", "Nf3")
    assert game.parse_move(state, "2... Nc6") == "b8c6"
    assert game.parse_move(state, "2. ...Nc6") == "b8c6"


PROMOTION = "8/4P3/8/8/8/8/k7/4K3 w - - 0 1"


@pytest.mark.parametrize("text", ["e8=Q+", "e8=Q", "e8=q", "e8Q", "e7e8q", "E7E8Q"])
def test_parse_promotion_variants(text):
    assert game.parse_move(chess.Board(PROMOTION), text) == "e7e8q"


@pytest.mark.parametrize("text", ["e8=N", "e8N", "e7e8n"])
def test_parse_underpromotion(text):
    assert game.parse_move(chess.Board(PROMOTION), text) == "e7e8n"


@pytest.mark.parametrize("text", ["e7e8", "e8", "e7-e8"])
def test_missing_promotion_piece_has_its_own_reason(text):
    with pytest.raises(IllegalMove, match="is a promotion and needs a piece, e.g. e7e8q"):
        game.parse_move(chess.Board(PROMOTION), text)


@pytest.mark.parametrize(
    ("text", "move_id"),
    [
        ("O-O", "e1g1"),
        ("0-0", "e1g1"),
        ("o-o", "e1g1"),
        ("e1g1", "e1g1"),
        ("O-O-O", "e1c1"),
        ("0-0-0", "e1c1"),
        ("e1c1", "e1c1"),
    ],
)
def test_parse_castling_variants(text, move_id):
    state = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert game.parse_move(state, text) == move_id


@pytest.mark.parametrize(
    "text", ["exd6", "e5d6", "exd6 e.p.", "exd6 ep", "exd6e.p.", "exd6 (e.p.)"]
)
def test_parse_en_passant_right_after_the_double_step(text):
    state = play("e4", "a6", "e5", "d5")
    assert game.parse_move(state, text) == "e5d6"


def test_en_passant_is_illegal_once_the_moment_has_passed():
    state = play("e4", "a6", "e5", "d5", "Nf3", "a5")
    for text in ("exd6", "e5d6", "exd6 e.p."):
        with pytest.raises(IllegalMove, match="not a legal move"):
            game.parse_move(state, text)


def test_case_decides_between_pawn_and_bishop_captures():
    state = chess.Board("4k3/8/8/8/2p5/1P6/4B3/4K3 w - - 0 1")
    assert game.parse_move(state, "bxc4") == "b3c4"
    assert game.parse_move(state, "Bxc4") == "e2c4"
    with pytest.raises(IllegalMove, match="ambiguous: it could be Bxc4 or bxc4"):
        game.parse_move(state, "BXC4")


def test_promotion_without_a_piece_that_also_reads_as_a_bishop_capture_is_ambiguous():
    state = chess.Board("2r1k3/1P6/4B3/8/8/8/8/4K3 w - - 0 1")
    for text in ("bxc8", "BXC8"):
        with pytest.raises(IllegalMove, match=r"ambiguous: it could be Bxc8 or bxc8=B or "):
            game.parse_move(state, text)
    assert game.parse_move(state, "Bxc8") == "e6c8"
    assert game.parse_move(state, "bxc8=Q") == "b7c8q"


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


@pytest.mark.parametrize(
    "text", ["e5", "e2e5", "Ke2", "", "--", "0000", "resign", "1.", "**", "e4 e5", "1. e4 e5"]
)
def test_illegal_or_unreadable_moves_are_rejected(text):
    with pytest.raises(IllegalMove, match="not a legal move"):
        game.parse_move(game.new_state(), text)


def test_describe_has_fen_board_with_coordinates_recent_moves_and_check():
    text = game.describe(play("e4", "f5", "Qh5+"))
    assert "FEN: rnbqkbnr/ppppp1pp/8/5p1Q/4P3/8/PPPP1PPP/RNB1KBNR b KQkq - 1 2" in text
    assert "8 | r n b q k b n r" in text
    assert "5 | . . . . . p . Q" in text
    assert "    a b c d e f g h" in text
    assert "Recent moves: 1...f5 2. Qh5+\n" in text
    assert "Black to move" in text and "Black is in check." in text
    assert "Last move: White played Qh5+." in text
    assert "Halfmove clock: 1 (automatic draw at 100)" in text


def test_describe_of_the_first_move_has_no_recent_moves():
    text = game.describe(game.new_state())
    assert "Recent moves: none yet" in text
    assert "Halfmove clock: 0 (automatic draw at 100)" in text
    assert "Last move" not in text and "occurred" not in text
    assert "Recent moves: 1. e4\n" in game.describe(play("e4"))


def test_describe_shows_only_the_last_two_plies_with_their_move_numbers():
    def recent(*moves: str) -> str:
        return next(line for line in game.describe(play(*moves)).splitlines() if "Recent" in line)

    assert recent("e4", "e5", "Nf3", "Nc6", "Bb5") == "Recent moves: 2...Nc6 3. Bb5"
    assert recent("e4", "e5", "Nf3", "Nc6", "Bb5", "a6") == "Recent moves: 3. Bb5 a6"


def test_describe_counts_the_halfmove_clock_and_warns_of_a_repeated_position():
    shuffle = ["Nf3", "Nf6", "Ng1", "Ng8"]
    once = play(*shuffle[:3])
    assert "Halfmove clock: 3 (automatic draw at 100)" in game.describe(once)
    assert "occurred" not in game.describe(once)
    twice = play(*shuffle)
    assert "This position has now occurred twice; a third time is an automatic draw." in (
        game.describe(twice)
    )
    assert "Halfmove clock: 4 (automatic draw at 100)" in game.describe(twice)
    assert "Halfmove clock: 0 " in game.describe(play(*shuffle, "e4"))


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
    resigned = game.record(state, Outcome(Side.FIRST, "resignation"), ("a", "b"))
    assert '[Result "1-0"]' in resigned and "Stockfish" not in resigned
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
    for part in ("system", "user"):
        white_words = re.findall(r"\w+", getattr(game.prompt(white), part))
        black_words = re.findall(r"\w+", getattr(game.prompt(black), part))
        assert len(white_words) == len(black_words)
        differing = {(w, b) for w, b in zip(white_words, black_words, strict=True) if w != b}
        assert differing == {("White", "Black"), ("Black", "White")}


def sections(text: str) -> list[str]:
    return re.findall(r"^[A-Z][A-Z ]+$", text, flags=re.MULTILINE)


def test_prompt_puts_rules_in_system_and_the_turn_in_user():
    state = game.new_state()
    ctx = PromptContext(
        side=Side.FIRST,
        position=game.describe(state),
        legal_moves=list(game.legal_moves(state).values()),
        turn=1,
        max_turns=40,
    )
    prompt = game.prompt(ctx)
    assert sections(prompt.system) == ["ROLE AND GOAL", "RULES"]
    assert "playing a game of chess as White" in prompt.system and "win" in prompt.system
    assert "at most 40 moves" in prompt.system
    flat_system = " ".join(prompt.system.split())
    assert f"ahead by at least {ADJUDICATION_WIN_CP // 100} pawns" in flat_system
    assert "or with a forced mate, wins" in flat_system
    assert "FEN" not in prompt.system and "Nf3" not in prompt.system
    assert (
        "The position shows the halfmove clock for the 50-move rule and says when the current "
        "position has occurred before." in flat_system
    )

    assert sections(prompt.user) == ["POSITION", "LEGAL MOVES"]
    assert "move 1 of 40 (40 left" in prompt.user
    assert "Nh3, Nf3" in prompt.user
    assert "No draw offer is pending." in prompt.user
    assert "Unless you resign or accept a draw, choose exactly one move" in prompt.user
    assert "referee" not in prompt.user.lower()
    assert "$" not in prompt.system + prompt.user
    assert prompt.text == f"{prompt.system}\n{prompt.user}"

    retry = game.prompt(PromptContext(**{**ctx.__dict__, "rejection": "illegal move Ke2"}))
    assert retry.system == prompt.system
    assert sections(retry.user) == ["POSITION", "LEGAL MOVES", "PREVIOUS ATTEMPT"]
    assert retry.user.rstrip().endswith("Answer again with a legal move.")
    assert "illegal move Ke2" in retry.user


# --- adjudication at the turn limit (score mapping: test_game_master.py) ---


@pytest.mark.skipif(shutil.which(STOCKFISH) is None, reason="Stockfish is not installed")
def test_stockfish_adjudicates_a_queen_up_position_for_white():
    settings = load_settings({"OLLMPIADA_STOCKFISH": STOCKFISH})
    queen_up = chess.Board("4k3/8/8/8/8/8/3QK3/8 b - - 0 1")
    with game.session(settings):
        outcome = game.adjudicate(queen_up)
    assert outcome.winner is Side.FIRST and outcome.adjudicated and outcome.score


def test_session_names_the_setting_when_stockfish_does_not_start(tmp_path):
    settings = load_settings({"OLLMPIADA_STOCKFISH": str(tmp_path / "no-stockfish")})
    with pytest.raises(GameSetupError, match="OLLMPIADA_STOCKFISH"):
        with game.session(settings):
            pass


def test_session_names_the_setting_when_the_program_never_speaks_uci(monkeypatch):
    def hang(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", hang)
    settings = load_settings({"OLLMPIADA_STOCKFISH": "/bin/cat"})
    with pytest.raises(GameSetupError, match="OLLMPIADA_STOCKFISH"):
        with game.session(settings):
            pass
