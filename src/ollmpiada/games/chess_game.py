"""Chess on the `chess` library."""

import re
from pathlib import Path

import chess
import chess.pgn
from rich.text import Text

from ollmpiada.games.base import Game, IllegalMove, Outcome, Side

REASONS = {
    chess.Termination.CHECKMATE: "checkmate",
    chess.Termination.STALEMATE: "stalemate",
    chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material",
    chess.Termination.SEVENTYFIVE_MOVES: "75-move rule",
    chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition",
    chess.Termination.FIFTY_MOVES: "50-move rule",
    chess.Termination.THREEFOLD_REPETITION: "threefold repetition",
}
# Mid-tone squares keep both white and black pieces at a 3:1 contrast or better.
LIGHT, DARK = "on #b08a5a", "on #7a5a3a"
LIGHT_LAST, DARK_LAST = "on #8f7f2a", "on #7f8a2e"
PIECE_STYLES = {chess.WHITE: "bold #ffffff", chess.BLACK: "bold #000000"}


def _side(color: chess.Color) -> Side:
    return Side.FIRST if color == chess.WHITE else Side.SECOND


def _loose(move_text: str) -> str:
    """Case, check marks and zero-for-O castling ignored."""
    return move_text.rstrip("+#").lower().replace("0", "o")


def _undisambiguated(san: str) -> str:
    """`Nbd2` -> `Nd2`, so an under-specified answer finds every move it could mean."""
    return re.sub(r"^([NBRQK])[a-h]?[1-8]?(?=x?[a-h][1-8])", r"\1", san)


class ChessGame(Game[chess.Board]):
    name = "chess"
    side_names = ("White", "Black")
    prompt_template = Path(__file__).with_name("chess_prompt.txt").read_text(encoding="utf-8")
    record_extension = "pgn"

    def new_state(self) -> chess.Board:
        return chess.Board()

    def side_to_move(self, state: chess.Board) -> Side:
        return _side(state.turn)

    def legal_moves(self, state: chess.Board) -> dict[str, str]:
        return {move.uci(): state.san(move) for move in state.legal_moves}

    def parse_move(self, state: chess.Board, text: str) -> str:
        legal = self.legal_moves(state)
        move_text = text.strip().rstrip("!?")
        if move_text.lower() in legal:
            return move_text.lower()
        try:
            uci = state.parse_san(move_text).uci()
            if uci in legal:
                return uci
        except ValueError:
            pass
        loose = _loose(move_text)
        matches = [
            uci
            for uci, san in legal.items()
            if loose in (_loose(san), _loose(_undisambiguated(san)))
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            readings = " or ".join(sorted(legal[uci] for uci in matches))
            raise IllegalMove(f"{text!r} is ambiguous: it could be {readings}")
        raise IllegalMove(f"{text!r} is not a legal move in this position")

    def apply(self, state: chess.Board, move_id: str) -> chess.Board:
        board = state.copy()
        board.push_uci(move_id)
        return board

    def outcome(self, state: chess.Board) -> Outcome | None:
        result = state.outcome()
        if result is not None:
            winner = None if result.winner is None else _side(result.winner)
            return Outcome(winner, REASONS[result.termination])
        # claim_draw=True would also claim when a repeating move is merely available,
        # taking the choice away from the player; claim only what has happened.
        if state.is_repetition(3):
            return Outcome(None, REASONS[chess.Termination.THREEFOLD_REPETITION])
        if state.is_fifty_moves():
            return Outcome(None, REASONS[chess.Termination.FIFTY_MOVES])
        return None

    def describe(self, state: chess.Board) -> str:
        rows = [
            f"{rank + 1} | "
            + " ".join(
                (piece.symbol() if (piece := state.piece_at(chess.square(file, rank))) else ".")
                for file in range(8)
            )
            for rank in range(7, -1, -1)
        ]
        history = state.root().variation_san(state.move_stack) or "none yet"
        to_move = self.side_name(_side(state.turn))
        facts = [
            f"FEN: {state.fen()}",
            "Board (White at the bottom; uppercase = White, lowercase = Black, . = empty):",
            *rows,
            "    a b c d e f g h",
            f"Move history: {history}",
            f"Move number {state.fullmove_number}, {to_move} to move.",
        ]
        if state.move_stack:
            before = state.copy()
            last = before.pop()
            mover = self.side_name(_side(before.turn))
            facts.append(f"Last move: {mover} played {before.san(last)}.")
        if state.is_check():
            facts.append(f"{to_move} is in check.")
        return "\n".join(facts)

    def render(self, state: chess.Board) -> Text:
        last = state.peek() if state.move_stack else None
        highlighted = {last.from_square, last.to_square} if last else set()
        board = Text()
        for rank in range(7, -1, -1):
            board.append(f"{rank + 1} ", style="dim")
            for file in range(8):
                square = chess.square(file, rank)
                light = (file + rank) % 2 == 1
                if square in highlighted:
                    style = LIGHT_LAST if light else DARK_LAST
                else:
                    style = LIGHT if light else DARK
                piece = state.piece_at(square)
                if piece:
                    glyph = chess.UNICODE_PIECE_SYMBOLS[piece.symbol().lower()]
                    board.append(f" {glyph} ", style=f"{PIECE_STYLES[piece.color]} {style}")
                else:
                    board.append("   ", style=style)
            board.append("\n")
        board.append("  " + "".join(f" {f} " for f in "abcdefgh"), style="dim")
        return board

    def record(self, state: chess.Board, outcome: Outcome | None, players: tuple[str, str]) -> str:
        game = chess.pgn.Game.from_board(state)
        game.headers["Event"] = "oLLMpiada"
        game.headers["White"], game.headers["Black"] = players
        if outcome is None:
            game.headers["Result"] = "*"
        else:
            game.headers["Result"] = {Side.FIRST: "1-0", Side.SECOND: "0-1", None: "1/2-1/2"}[
                outcome.winner
            ]
            game.headers["Termination"] = outcome.reason
        return str(game)
