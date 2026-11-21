"""What the user sees: Rich live view, results and statistics tables, plain lines for scripts."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
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
from ollmpiada.game_master import (
    AnswerReceived,
    AnswerRejected,
    GameFinished,
    GameStarted,
    MoveMade,
)
from ollmpiada.games.base import Game, Side

# The move list keeps this height, so the board below it never moves.
MOVE_LIST_LINES = 4
COMPACT = {"box": box.SIMPLE_HEAD, "collapse_padding": True}
REASON_SHOWN = 60
UNKNOWN = "–"
THINKING = "▶"


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
    """Unknown counts are never shown as zero; `≥` marks totals some requests did not report."""
    if not s.tokens_known:
        return "unknown"
    reasoning = _count(s.reasoning_tokens) if s.reasoning_known else "?"
    text = f"{_count(s.input_tokens)} / {_count(s.output_tokens)} / {reasoning}"
    return f"≥ {text}" if s.calls_without_usage else text


def _count(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 10_000 else str(tokens)


def _token_cells(s: PlayerStats) -> tuple[str, str, str]:
    """Input, output and reasoning tokens, each `–` when unknown and `≥` when partly reported."""
    if not s.tokens_known:
        return UNKNOWN, UNKNOWN, UNKNOWN
    partly = "≥ " if s.calls_without_usage else ""
    reasoning = UNKNOWN
    if s.reasoning_known:
        reasoning = ("≥ " if s.calls_without_reasoning else partly) + _count(s.reasoning_tokens)
    return partly + _count(s.input_tokens), partly + _count(s.output_tokens), reasoning


def _duration(elapsed: timedelta) -> str:
    minutes, secs = divmod(int(elapsed.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


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
class GameStats:
    """Times and per-player statistics of the current game, shown under the board."""

    players: tuple[str, str]
    stats: dict[str, PlayerStats]
    started_at: datetime
    turn_started_at: datetime
    ended_at: datetime | None = None
    # Wall-clock time of each player's finished turns, like a chess clock.
    turn_totals: defaultdict[str, timedelta] = field(default_factory=lambda: defaultdict(timedelta))


@dataclass
class LiveView:
    """Rich renderable fed by runner events; the CLI refreshes Live after each event and
    periodically, so timers, computed at render time from `clock`, tick between events."""

    game: Game
    visual: bool
    clock: Callable[[], datetime] = datetime.now
    state: CompetitionState | None = None
    current: int | None = None
    turn: int = 0
    board: object = None
    moves: Text = field(default_factory=Text)
    numbered_turn: int = -1
    game_stats: GameStats | None = None

    def handle(self, event: CompetitionEvent) -> None:
        match event:
            case GameStarted(number=number, players=players, state=board):
                self.current, self.turn, self.board = number, 0, board
                self.moves, self.numbered_turn = Text(), -1
                now = self.clock()
                stats = {name: PlayerStats(name) for name in players}
                self.game_stats = GameStats(players, stats, now, now)
            case AnswerReceived(player=player, elapsed_s=elapsed_s, usage=usage):
                assert self.game_stats is not None
                self.game_stats.stats[player].add_call(elapsed_s, usage)
            case MoveMade(side=side, player=player, move=move, state=board, draw_offered=offered):
                self._note(side, move + (" (offers draw)" if offered else ""))
                self.turn += 1
                self.board = board
                assert self.game_stats is not None
                now = self.clock()
                self.game_stats.turn_totals[player] += now - self.game_stats.turn_started_at
                self.game_stats.turn_started_at = now
            case AnswerRejected(side=side, reason=reason, attempt=attempt):
                reason = " ".join(reason.split())
                reason = reason if len(reason) <= REASON_SHOWN else reason[:REASON_SHOWN] + "..."
                entry = f"[✗ {self.game.side_name(side)} {attempt}: {reason}]"
                self._note(side, entry, style="red")
            case GameFinished():
                self.current = None
                self._end_game()
            case CompetitionProgress(state=state):
                self.state = state
            case CompetitionFinished(state=state):
                # A stopped game publishes no GameFinished, so its timers stop here.
                self.state = state
                self._end_game()

    def _end_game(self) -> None:
        if self.game_stats is not None and self.game_stats.ended_at is None:
            self.game_stats.ended_at = self.clock()

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

    def _game_stats_view(self, g: GameStats) -> Group:
        """Timers and move count of the current game, then per player: who is thinking,
        current thinking time and legal moves, total thinking time, and usage."""
        now = self.clock()
        running = "running" if g.ended_at is None else "ended after"
        length = _duration((g.ended_at or now) - g.started_at)
        line = Text(
            f"Started {g.started_at:%H:%M:%S}, {running} {length}"
            f" | {self.turn} move{'' if self.turn == 1 else 's'} completed"
        )
        # Retries keep the turn clock running: it times the whole turn, not one request.
        # A game ending without a move (resignation, a stop) counts the last turn too.
        turn = (g.ended_at or now) - g.turn_started_at
        mover = 0 if self.game.side_to_move(self.board) is Side.FIRST else 1
        legal = len(self.game.legal_moves(self.board))
        numbers = ("Now", "Legal", "Total", "Requests", "Tokens in", "Tokens out", "Reasoning")
        table = Table(
            "",
            "Side",
            Column("Player", overflow="fold"),
            *(Column(header, no_wrap=True) for header in numbers),
            **COMPACT,
        )
        for index, name in enumerate(g.players):
            moving = index == mover
            thinking = moving and g.ended_at is None
            total = g.turn_totals[name] + (turn if moving else timedelta())
            stats = g.stats[name]
            table.add_row(
                THINKING if thinking else "",
                self.game.side_names[index],
                name,
                _duration(turn) if thinking else "",
                str(legal) if thinking else "",
                _duration(total),
                str(stats.calls),
                *_token_cells(stats),
                style="bold" if thinking else "",
            )
        return Group(line, table)

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
            if self.game_stats is not None:
                yield self._game_stats_view(self.game_stats)
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
