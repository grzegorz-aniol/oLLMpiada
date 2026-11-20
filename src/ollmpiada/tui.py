"""What the user sees: Rich live view, results and statistics tables, plain lines for scripts."""

from dataclasses import dataclass, field

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
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

# The move list keeps this height, so the board below it never moves.
MOVE_LIST_LINES = 4
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
    moves: Text = field(default_factory=Text)
    numbered_turn: int = -1

    def handle(self, event: CompetitionEvent) -> None:
        match event:
            case GameStarted(number=number, state=board):
                self.current, self.turn, self.board = number, 0, board
                self.moves, self.numbered_turn = Text(), -1
            case MoveMade(side=side, move=move, state=board, draw_offered=offered):
                self._note(side, move + (" (offers draw)" if offered else ""))
                self.turn += 1
                self.board = board
            case AnswerRejected(side=side, reason=reason, attempt=attempt):
                reason = " ".join(reason.split())
                reason = reason if len(reason) <= REASON_SHOWN else reason[:REASON_SHOWN] + "..."
                self._note(side, f"[✗ {attempt}: {reason}]", style="red")
            case GameFinished():
                self.current = None
            case CompetitionProgress(state=state) | CompetitionFinished(state=state):
                self.state = state

    def _note(self, side: Side, entry: str, style: str = "") -> None:
        """Append to the flowing move list, numbering each pair before its first entry."""
        if side is Side.FIRST and self.numbered_turn != self.turn:
            self.numbered_turn = self.turn
            self.moves.append(f"({self.turn // 2 + 1}) ")
        self.moves.append(entry, style=style)
        self.moves.append(" ")

    def _move_list(self, console: Console, width: int) -> Text:
        """The last lines of the wrapped move list, padded to a fixed height."""
        lines = list(self.moves.wrap(console, width))[-MOVE_LIST_LINES:]
        padding = [Text()] * (MOVE_LIST_LINES - len(lines))
        return Text("\n").join(padding + lines)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.state is None:
            yield Text("Starting...")
            return
        status = Text(f"Games {len(self.state.results)} of {self.state.games}")
        if self.current is not None:
            # Moves per side, the unit of --max-turns.
            status.append(f" | game {self.current}, move {self.turn // 2 + 1}")
        status.append(f" | {self.state.status}")
        yield status
        if self.visual and self.board is not None:
            yield self._move_list(console, options.max_width)
            yield self.game.render(self.board)
        yield results_table(self.state)
        yield stats_table(self.state)


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
