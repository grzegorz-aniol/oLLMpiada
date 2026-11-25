"""Chess on the `chess` library."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import chess
import chess.engine
import chess.pgn
from rich.text import Text

from ollmpiada.config import Settings
from ollmpiada.games.base import (
    ADJUDICATION,
    EVENT,
    AdjudicationError,
    Game,
    GameSetupError,
    IllegalMove,
    Outcome,
    PromptContext,
    Questions,
    Rejection,
    Side,
    first_full_move,
    strip_answer,
)

HERE = Path(__file__).parent
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
# The position text shows only the opponent's last move and the player's own previous one;
# the board and FEN carry the position, and the draw-rule lines carry what history would.
RECENT_PLIES = 2
MOVE_NUMBER = re.compile(r"^\d+\s*[.…][.…\s]*")
EN_PASSANT_MARK = re.compile(r"(?<=[1-8+#])\s*\(?e\.?\s*p\.?$", re.IGNORECASE)
SPLIT_SQUARES = re.compile(r"^([a-h][1-8])[\s-]+(?=[a-h][1-8])", re.IGNORECASE)
# At the turn limit a side this far ahead, in centipawns, wins; the system prompt states it
# in pawns. A forced mate counts as MATE_CP, beyond any material lead.
ADJUDICATION_WIN_CP = 500
MATE_CP = 100_000
# Far deeper than telling a 5-pawn lead needs, and well under a second per position. The
# time cap is a safety net: only a limit with a time makes SimpleEngine give up on a hung
# engine, after that time plus its 10 s communication timeout.
ADJUDICATION_LIMIT = chess.engine.Limit(depth=16, time=5)
ANSWER_FORMAT = """ANSWER FORMAT

Answer with three fields: action (move, resign or accept_draw); move (your move
exactly as listed under LEGAL MOVES, required when action is move); offer_draw
(true to offer a draw together with your move, otherwise false).
"""
MOVE_ONLY_ANSWER_FORMAT = """ANSWER FORMAT

Answer with one field: move (your move exactly as listed under LEGAL MOVES).
"""
QUESTIONS = Questions(
    move="Which move do you play?",
    piece="Which piece do you move? Each option lists its moves.",
    action="What do you do this turn?",
    offer_draw="Do you offer a draw together with your move?",
    options={
        "move": "Make a move",
        "resign": "Resign the game",
        "accept_draw": "Accept the opponent's draw offer",
        "yes": "Offer a draw",
        "no": "Do not offer a draw",
    },
)


def _side(color: chess.Color) -> Side:
    return Side.FIRST if color == chess.WHITE else Side.SECOND


def _loose(move_text: str) -> str:
    """Case, check marks and zero-for-O castling ignored."""
    return move_text.rstrip("+#").lower().replace("0", "o")


def _normalized(text: str) -> str:
    """`**12... exd6 e.p.**` -> `exd6`, `e2 e4` -> `e2e4`: the move without what models add."""
    move = strip_answer(MOVE_NUMBER.sub("", strip_answer(text)))
    move = EN_PASSANT_MARK.sub("", move)
    return SPLIT_SQUARES.sub(r"\1", move)


def _opening_notice() -> str:
    """Why a turn in the opening allows only a move."""
    # Through base at call time, so a test that patches OPENING_MOVES there sees it.
    return f"resigning and draw offers start with your move {first_full_move()}"


def _undisambiguated(san: str) -> str:
    """`Nbd2` -> `Nd2`, so an under-specified answer finds every move it could mean."""
    return re.sub(r"^([NBRQK])[a-h]?[1-8]?(?=x?[a-h][1-8])", r"\1", san)


class ChessGame(Game[chess.Board]):
    name = "chess"
    side_names = ("White", "Black")
    system_template = (HERE / "chess_system_prompt.txt").read_text(encoding="utf-8")
    user_template = (HERE / "chess_user_prompt.txt").read_text(encoding="utf-8")
    record_extension = "pgn"
    questions = QUESTIONS

    def __init__(self) -> None:
        self._engine: chess.engine.SimpleEngine | None = None

    def new_state(self, seed: int) -> chess.Board:
        return chess.Board()

    def side_to_move(self, state: chess.Board) -> Side:
        return _side(state.turn)

    def legal_moves(self, state: chess.Board) -> dict[str, str]:
        return {move.uci(): state.san(move) for move in state.legal_moves}

    def move_groups(self, state: chess.Board) -> dict[str, dict[str, str]]:
        """One group per origin square, keyed like `knight_g1`."""
        groups: dict[str, dict[str, str]] = {}
        for move in state.legal_moves:
            piece = state.piece_type_at(move.from_square)
            assert piece is not None
            key = f"{chess.piece_name(piece)}_{chess.square_name(move.from_square)}"
            groups.setdefault(key, {})[move.uci()] = state.san(move)
        return groups

    def parse_move(self, state: chess.Board, text: str) -> str:
        legal = self.legal_moves(state)
        move_text = _normalized(text)
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
        # A promotion without its piece is one more reading: `bxc8` can be the pawn
        # promoting or, case-blind, a bishop taking on c8.
        promotions = [
            uci
            for uci, san in legal.items()
            if len(uci) == 5 and loose in (uci[:4], _loose(san).split("=")[0])
        ]
        if len(matches) == 1 and not promotions:
            return matches[0]
        if matches:
            readings = " or ".join(sorted(legal[uci] for uci in matches + promotions))
            raise IllegalMove(f"{text!r} is ambiguous: it could be {readings}")
        if promotions:
            queen = promotions[0][:4] + "q"
            raise IllegalMove(
                f"{text!r} is a promotion and needs a piece, e.g. {queen} or {legal[queen]}"
            )
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

    def adjudicate(self, state: chess.Board) -> Outcome:
        """A win for a side ahead by `ADJUDICATION_WIN_CP` or with a forced mate, else a draw."""
        score = self.evaluate(state)
        lead = score.score(mate_score=MATE_CP)
        winner = None
        if lead >= ADJUDICATION_WIN_CP:
            winner = Side.FIRST
        elif lead <= -ADJUDICATION_WIN_CP:
            winner = Side.SECOND
        text = str(score) if score.is_mate() else f"{score} cp"
        return Outcome(winner, ADJUDICATION, text)

    def move_mark(self, state: chess.Board) -> str | None:
        """The move number before White's move: `(1) e4 e5 (2) Nf3`."""
        return f"({state.fullmove_number})" if state.turn == chess.WHITE else None

    def evaluate(self, state: chess.Board) -> chess.engine.Score:
        """The engine's score of the position from White's view."""
        if self._engine is None:
            raise RuntimeError("chess adjudication needs the engine; play inside session()")
        try:
            return self._engine.analyse(state, ADJUDICATION_LIMIT)["score"].white()
        except chess.engine.EngineError as e:  # includes the engine process dying
            raise AdjudicationError(f"Stockfish failed to adjudicate the game: {e}") from e
        except TimeoutError as e:
            raise AdjudicationError("Stockfish stopped responding while adjudicating") from e

    @contextmanager
    def session(self, settings: Settings) -> Iterator[None]:
        try:
            # Its own process group keeps Ctrl+C, which stops the competition, off the engine.
            engine = chess.engine.SimpleEngine.popen_uci(settings.stockfish, setpgrp=True)
        # TimeoutError: the program started but never finished the UCI handshake.
        except (OSError, chess.engine.EngineError, TimeoutError) as e:
            raise GameSetupError(
                f"chess needs Stockfish to adjudicate games at the turn limit, and "
                f"{settings.stockfish!r} did not start ({e}). Install Stockfish or set "
                "OLLMPIADA_STOCKFISH to its path."
            ) from e
        self._engine = engine
        try:
            yield
        finally:
            self._engine = None
            engine.close()

    def prompt_values(self, ctx: PromptContext) -> dict[str, object]:
        """The opening length and the choose-move, draw-offer and retry sentences of the turn."""
        return dict(
            opening_moves=first_full_move() - 1,
            choose_move=(
                f"Choose exactly one move from this list; {_opening_notice()}."
                if ctx.move_only
                else "Unless you resign or accept a draw, choose exactly one move from this list."
            ),
            draw_offer=(
                f"{self.side_name(ctx.side.opponent)} offers a draw. Answer accept_draw to "
                "accept it, or make a move to decline it."
                if ctx.draw_offered
                else "No draw offer is pending."
            ),
            previous_attempt=(
                f"PREVIOUS ATTEMPT\n\nYour previous answer this turn was rejected: "
                f"{ctx.rejection}\nAnswer again with a legal move."
                if ctx.rejection
                else ""
            ),
        )

    def answer_format(self, ctx: PromptContext) -> str:
        return MOVE_ONLY_ANSWER_FORMAT if ctx.move_only else ANSWER_FORMAT

    def rejection(self, rule: Rejection) -> str:
        match rule:
            case Rejection.MOVE_ONLY:
                return f"{_opening_notice()}; play a move"
            case Rejection.NO_DRAW_OFFER:
                return "there is no draw offer to accept"
            case Rejection.NO_MOVE:
                return "the action is move but no move was given"

    def describe(self, state: chess.Board) -> str:
        rows = [
            f"{rank + 1} | "
            + " ".join(
                (piece.symbol() if (piece := state.piece_at(chess.square(file, rank))) else ".")
                for file in range(8)
            )
            for rank in range(7, -1, -1)
        ]
        recent = state.move_stack[-RECENT_PLIES:]
        before_recent = state.copy()
        for _ in recent:
            before_recent.pop()
        recent_san = before_recent.variation_san(recent) or "none yet"
        to_move = self.side_name(_side(state.turn))
        facts = [
            f"FEN: {state.fen()}",
            "Board (White at the bottom; uppercase = White, lowercase = Black, . = empty):",
            *rows,
            "    a b c d e f g h",
            f"Recent moves: {recent_san}",
            f"Move number {state.fullmove_number}, {to_move} to move.",
            f"Halfmove clock: {state.halfmove_clock} (automatic draw at 100)",
        ]
        # A hint that positions are repeating, not a complete warning: it counts only the
        # current position, which a prompt only ever sees twice (a third time ends the game).
        if state.is_repetition(2):
            facts.append("This position has now occurred twice; a third time is an automatic draw.")
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

    def record(
        self, state: chess.Board, outcome: Outcome | None, players: tuple[str, str], start: int
    ) -> str:
        game = chess.pgn.Game.from_board(state)
        game.headers["Event"] = EVENT
        game.headers["White"], game.headers["Black"] = players
        if outcome is None:
            game.headers["Result"] = "*"
        else:
            game.headers["Result"] = {Side.FIRST: "1-0", Side.SECOND: "0-1", None: "1/2-1/2"}[
                outcome.winner
            ]
            game.headers["Termination"] = outcome.reason
            if outcome.score is not None:
                game.end().comment = f"Stockfish: {outcome.score}"
        return str(game)
