"""What the user sees: Rich live view, results and statistics tables, plain lines for scripts."""

from dataclasses import dataclass, field

from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Column, Table
from rich.text import Text

from ollmpiada.competition import (
    CompetitionEvent,
    CompetitionFinished,
    CompetitionProgress,
    CompetitionState,
    GameSummary,
    PlayerStats,
)
from ollmpiada.game_master import AnswerRejected, GameFinished, GameStarted, MoveMade
from ollmpiada.games.base import Game, Side

MOVE_LIST_ROWS = 20
COMPACT = {"box": box.SIMPLE_HEAD, "collapse_padding": True}
REASON_SHOWN = 60


def results_table(state: CompetitionState) -> Table:
    names = [Column(side, no_wrap=True) for side in state.side_names]
    table = Table("Game", *names, "Result", "Reason", "Moves", title="Results", **COMPACT)
    for r in state.results:
        table.add_row(str(r.number), *r.players, r.result, r.reason, str(r.moves))
    return table


def stats_table(state: CompetitionState) -> Table:
    numbers = ["Games", "W", "L", "D", "Think s", "s/move", "Tokens in/out/reas.", "Illegal",
               "/game"]  # fmt: skip
    table = Table(
        Column("Player", overflow="fold"),
        *(Column(header, no_wrap=True) for header in numbers),
        title="Statistics",
        **COMPACT,
    )
    for s in state.stats.values():
        table.add_row(
            s.name,
            str(s.games),
            str(s.wins),
            str(s.losses),
            str(s.draws),
            f"{s.thinking_s:.1f}",
            f"{s.thinking_s / s.moves:.1f}" if s.moves else "-",
            _tokens(s),
            str(s.illegal_attempts),
            f"{s.illegal_attempts / s.games:.2f}" if s.games else "-",
        )
    return table


def _tokens(s: PlayerStats) -> str:
    """Unknown counts are never shown as zero; `≥` marks totals some calls did not report."""
    if not s.tokens_known:
        return "unknown"
    reported = s.calls - s.calls_without_usage
    reasoning = "?" if s.calls_without_reasoning == reported else _count(s.reasoning_tokens)
    text = f"{_count(s.input_tokens)} / {_count(s.output_tokens)} / {reasoning}"
    return f"≥ {text}" if s.calls_without_usage else text


def _count(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 10_000 else str(tokens)


def summary_lines(state: CompetitionState) -> list[str]:
    """The closing lines: status, totals and side imbalance."""
    lines = [f"Competition {state.status}: {len(state.results)} of {state.games} games played."]
    if state.error:
        lines.append(f"Error: {state.error}")
    for s in state.stats.values():
        lines.append(f"{s.name}: {s.wins} wins, {s.losses} losses, {s.draws} draws")
    counted = state.stats[state.players[0]].games
    if counted % 2:
        lines.append(
            f"Odd number of counted games: {state.players[0]} played {state.side_names[0]} "
            "one game more often."
        )
    return lines


def game_line(summary: GameSummary, side_names: tuple[str, str]) -> str:
    first, second = summary.players
    return (
        f"game {summary.number}: {side_names[0]} {first} vs {side_names[1]} {second}: "
        f"{summary.result} ({summary.reason}), {summary.moves} move"
        + ("" if summary.moves == 1 else "s")
    )


@dataclass
class LiveView:
    """Rich renderable fed by runner events; the CLI refreshes Live after each event."""

    game: Game
    visual: bool
    state: CompetitionState | None = None
    current: int | None = None
    turn: int = 0
    board: object = None
    moves: list[Text] = field(default_factory=list)

    def handle(self, event: CompetitionEvent) -> None:
        match event:
            case GameStarted(number=number, state=board):
                self.current, self.turn, self.board, self.moves = number, 0, board, []
            case MoveMade(side=side, move=move, state=board, draw_offered=offered):
                self.turn += 1
                self.board = board
                prefix = f"{(self.turn + 1) // 2}. " if side is Side.FIRST else "   "
                self.moves.append(Text(prefix + move + (" (offers draw)" if offered else "")))
            case AnswerRejected(reason=reason, attempt=attempt):
                reason = " ".join(reason.split())
                reason = reason if len(reason) <= REASON_SHOWN else reason[:REASON_SHOWN] + "..."
                self.moves.append(Text(f"   ✗ {attempt}: {reason}", style="red"))
            case GameFinished():
                self.current = None
            case CompetitionProgress(state=state) | CompetitionFinished(state=state):
                self.state = state

    def __rich__(self) -> RenderableType:
        if self.state is None:
            return Text("Starting...")
        status = Text(f"Games {len(self.state.results)} of {self.state.games}")
        if self.current is not None:
            # Moves per side, the unit of --max-turns.
            status.append(f" | game {self.current}, move {self.turn // 2 + 1}")
        status.append(f" | {self.state.status}")
        parts: list[RenderableType] = [status]
        if self.visual and self.board is not None:
            board_and_moves = Table.grid(padding=(0, 4))
            board_and_moves.add_row(
                self.game.render(self.board), Group(*self.moves[-MOVE_LIST_ROWS:])
            )
            parts.append(board_and_moves)
        parts += [results_table(self.state), stats_table(self.state)]
        return Group(*parts)


class PlainOutput:
    """One line per finished game, for logs and scripts."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._printed = 0

    def handle(self, event: CompetitionEvent) -> None:
        if isinstance(event, CompetitionProgress | CompetitionFinished):
            state = event.state
            for summary in state.results[self._printed :]:
                self._console.print(
                    game_line(summary, state.side_names), highlight=False, soft_wrap=True
                )
            self._printed = len(state.results)
