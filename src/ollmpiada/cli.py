"""Command line: argparse subcommands rendered with Rich."""

import argparse
import asyncio
import dataclasses
import logging
import secrets
import signal
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Column, Table

from ollmpiada.competition import (
    DEFAULT_MAX_TURNS,
    Competition,
    CompetitionEvent,
    CompetitionSetupError,
    CompetitionState,
    default_max_turns,
    load_state,
)
from ollmpiada.config import ConfigError, Model, Settings, load_models, load_settings
from ollmpiada.experiment import (
    Experiment,
    ExperimentError,
    Progress,
    load_experiment,
    progress,
)
from ollmpiada.game_master import GameMaster
from ollmpiada.games.base import (
    SAMPLE_SEED,
    Game,
    GameSetupError,
    IllegalMove,
    first_full_move,
)
from ollmpiada.players import PlayerError, create_player, resolve_player
from ollmpiada.players.base import Player, TransportError, TurnRequest, TurnResponse
from ollmpiada.registry import MASTERS
from ollmpiada.servers import ServerError, Servers, cleanups, plan, providers_of
from ollmpiada.tui import (
    LiveView,
    PlainOutput,
    board_table,
    results_table,
    stats_table,
    summary_lines,
)

MIN_BOARDS = 8
"""Boards (duplicate pairs of games) below which `run` warns that the comparison says little
about skill, since a trick or two won on a few deals is mostly chance."""
REFRESH_S = 1.0

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return the process exit code."""
    parser = argparse.ArgumentParser(prog="ollmpiada", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("models", help="list configured models")
    probe = commands.add_parser(
        "probe", help="send a model one turn per answer format of the game's start"
    )
    probe.add_argument("player", help="<model-id>[@effort]")
    probe.add_argument("--game", choices=MASTERS, default="chess")
    run = commands.add_parser("run", help="play a competition between two models")
    run.add_argument("first", help="<model-id>[@effort], plays the first side in odd games")
    run.add_argument("second", help="<model-id>[@effort]")
    run.add_argument("--game", choices=MASTERS, default="chess")
    run.add_argument(
        "--games",
        type=_positive,
        default=2,
        help="number of games, even for bridge (default 2)",
    )
    run.add_argument(
        "--max-turns",
        type=_positive,
        help="moves per side before the game is adjudicated, for games with a turn limit "
        f"(default {DEFAULT_MAX_TURNS})",
    )
    run.add_argument(
        "--seed",
        type=int,
        help="start of games 1 and 2; each next pair of games takes the next value "
        "(default: random, saved with the results)",
    )
    run.add_argument("--mode", choices=["visual", "background"], default="visual")
    show = commands.add_parser("show", help="print the results of a saved competition")
    show.add_argument("run_dir", type=Path)
    experiment = commands.add_parser(
        "experiment", help="play and inspect experiments, folders of planned competitions"
    )
    actions = experiment.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list experiments with how many competitions are done")
    status = actions.add_parser("status", help="show each competition's runs")
    status.add_argument("id", help="the experiment's folder name")
    play = actions.add_parser("run", help="play the competitions that are not done yet")
    play.add_argument("id", help="the experiment's folder name")
    play.add_argument(
        "--dry-run",
        action="store_true",
        help="print which servers each pending competition stops, cleans up and starts",
    )
    args = parser.parse_args(argv)

    console = Console()
    if args.command == "show":
        return _show(console, args.run_dir)
    try:
        settings = load_settings()
        models = load_models(settings.models_config)
    except ConfigError as e:
        console.print(f"[red]Invalid configuration:[/red] {escape(str(e))}", highlight=False)
        return 2

    if args.command == "models":
        console.print(_models_table(models))
        return 0
    if args.command == "probe":
        return _probe(console, settings, models, args.player, MASTERS[args.game])
    if args.command == "experiment":
        return _experiment(console, settings, models, args)
    return _run(console, settings, models, args)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def _models_table(models: dict[str, Model]) -> Table:
    table = Table(
        Column("Model", no_wrap=True),
        Column("API", no_wrap=True),
        "Provider",
        "Reasoning efforts",
        "Available",
    )
    for m in models.values():
        table.add_row(
            m.id,
            m.provider.api,
            m.provider.name,
            ", ".join(m.spec.reasoning_efforts) or "-",
            "yes" if m.available else "[red]no[/red]",
        )
    return table


def _probe(
    console: Console, settings: Settings, models: dict[str, Model], name: str, master: GameMaster
) -> int:
    """Exit code 0 when the model answered every answer format of the game as expected: the
    move-only one (in chess the opening) and the full one (from the first move after it)."""
    try:
        player = create_player(name, models, timeout_s=settings.request_timeout_s)
    except PlayerError as e:
        console.print(f"[red]{escape(str(e))}[/red]", highlight=False)
        return 2
    # One turn per answer format: a game whose turns are all move-only gets a single one.
    turns: dict[bool, int] = {}
    for turn in (1, first_full_move()):
        turns.setdefault(master.game.move_only(turn), turn)
    max_turns = default_max_turns(master.game)
    requests: dict[str, TurnRequest] = {}
    for move_only, turn in turns.items():
        label = "Opening turn" if turn == 1 else f"Move {turn}"
        kind = "move only" if move_only else "full answer"
        requests[f"{label} ({kind})"] = master.start_request(max_turns=max_turns, turn=turn)
    console.print(f"Probing {name} with the opening position of {master.game.name}...")
    return asyncio.run(_probe_turns(console, master.game, player, requests))


async def _probe_turns(
    console: Console, game: Game, player: Player, requests: dict[str, TurnRequest]
) -> int:
    """Plays the turns in one event loop, which the player's HTTP client is bound to, and
    prints each one as it is answered, so a transport error keeps the turns before it."""
    answered = True
    for label, request in requests.items():
        try:
            response = await player.play(request)
        except TransportError as e:
            console.print(f"[red]Transport error:[/red] {escape(str(e))}", highlight=False)
            return 1
        console.print(f"[bold]{label}[/bold]")
        console.print(_probe_table(game, request, response))
        answered = answered and response.answer is not None
    return 0 if answered else 1


def _probe_table(game: Game, request: TurnRequest, response: TurnResponse) -> Table:
    usage = response.usage
    table = Table.grid(padding=(0, 2))
    table.add_row("Raw answer", escape(response.raw) or "-")
    if response.answer is None:
        table.add_row("Parsed", f"[red]format error:[/red] {escape(str(response.format_error))}")
    else:
        table.add_row("Parsed", escape(repr(response.answer)))
        if response.answer.action == "move":
            try:
                move = game.parse_move(game.new_state(SAMPLE_SEED), response.answer.move or "")
                table.add_row("Move", f"[green]legal[/green] ({request.legal_moves[move]})")
            except IllegalMove as e:
                table.add_row("Move", f"[red]illegal:[/red] {escape(str(e))}")
    table.add_row("Time", f"{response.elapsed_s:.2f} s")
    if usage:
        reasoning = "unknown" if usage.reasoning_tokens is None else usage.reasoning_tokens
        tokens = f"in {usage.input_tokens}, out {usage.output_tokens}, reasoning {reasoning}"
    table.add_row("Tokens", tokens if usage else "unknown")
    return table


def _run(
    console: Console, settings: Settings, models: dict[str, Model], args: argparse.Namespace
) -> int:
    """Exit code 0 when the competition finished, 1 when it was aborted or stopped."""
    if args.first == args.second:
        # Statistics are kept per player name, so the two names must differ.
        console.print("[red]Pick two different players[/red] (for example another effort).")
        return 2
    game = MASTERS[args.game].game
    if args.max_turns is not None and not game.turn_limit:
        console.print(f"[red]{game.name} has no turn limit;[/red] drop --max-turns.")
        return 2
    state = _compete(
        console,
        settings,
        models,
        args.game,
        (args.first, args.second),
        games=args.games,
        max_turns=args.max_turns or default_max_turns(game),
        seed=args.seed,
        visual=args.mode == "visual",
    )
    if state is None:
        return 2
    return 0 if state.status == "finished" else 1


def _compete(
    console: Console,
    settings: Settings,
    models: dict[str, Model],
    game_name: str,
    names: tuple[str, str],
    *,
    games: int,
    max_turns: int | None,
    seed: int | None,
    visual: bool,
) -> CompetitionState | None:
    """Play one competition into `settings.runs_dir` and print its summary; None when it could
    not start, after printing why. A missing seed is drawn at random."""
    try:
        players = (
            create_player(names[0], models, timeout_s=settings.request_timeout_s),
            create_player(names[1], models, timeout_s=settings.request_timeout_s),
        )
    except PlayerError as e:
        console.print(f"[red]{escape(str(e))}[/red]", highlight=False)
        return None

    master = MASTERS[game_name]
    game = master.game
    publish, live = _output(console, master, visual=visual)
    try:
        # Built before anything starts, so a rule it enforces (an even number of games for a
        # duplicate game) stops the run with its own message.
        competition = Competition(
            master,
            players,
            games=games,
            max_turns=max_turns,
            runs_dir=settings.runs_dir,
            seed=secrets.randbelow(2**31) if seed is None else seed,
            publish=publish,
        )
    except CompetitionSetupError as e:
        console.print(f"[red]{escape(str(e))}[/red]", highlight=False)
        return None
    if game.duplicate and games < 2 * MIN_BOARDS:
        console.print(
            f"[yellow]Only {games // 2} of the {MIN_BOARDS} boards recommended:[/yellow] "
            f"a few boards say little about skill (--games {2 * MIN_BOARDS} plays {MIN_BOARDS}).",
            highlight=False,
            soft_wrap=True,
        )
    try:
        with game.session(settings):
            state = _play_competition(console, competition, live)
    except GameSetupError as e:
        console.print(f"[red]Cannot start {game.name}:[/red] {escape(str(e))}", highlight=False)
        return None

    _print_summary(console, state, duplicate=game.duplicate)
    console.print(f"Saved to {competition.run_dir}", highlight=False, soft_wrap=True)
    return state


def _output(
    console: Console, master: GameMaster, *, visual: bool
) -> tuple[Callable[[CompetitionEvent], None], Live | None]:
    """Where the competition publishes its events: the live view in a terminal, with the
    `Live` display that shows it, else plain lines and no display."""
    if not console.is_terminal:
        return PlainOutput(console).handle, None
    view = LiveView(master, visual=visual)
    # Refreshing from the event loop (no refresh thread) never renders a board mid-update.
    live = Live(view, console=console, auto_refresh=False)

    def publish(event: CompetitionEvent) -> None:
        view.handle(event)
        live.refresh()

    return publish, live


def _play_competition(
    console: Console, competition: Competition, live: Live | None
) -> CompetitionState:
    """Play inside the `Live` display when there is one; returns the final state."""
    if live is None:
        return asyncio.run(_play(competition))
    with live, _log_to(console):
        return asyncio.run(_play(competition, tick=live.refresh))


async def _play(
    competition: Competition, tick: Callable[[], None] | None = None
) -> CompetitionState:
    """Run with Ctrl+C mapped to a stop request instead of KeyboardInterrupt.

    `tick` runs every `REFRESH_S` seconds, so live timers move while a model thinks.
    """
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, competition.stop)
    ticker = asyncio.create_task(_every(REFRESH_S, tick)) if tick else None
    try:
        return await competition.run()
    finally:
        if ticker:
            ticker.cancel()
        loop.remove_signal_handler(signal.SIGINT)


async def _every(seconds: float, action: Callable[[], None]) -> None:
    while True:
        await asyncio.sleep(seconds)
        action()


def _experiment(
    console: Console, settings: Settings, models: dict[str, Model], args: argparse.Namespace
) -> int:
    if args.action == "list":
        return _list_experiments(console, settings.experiments_dir, models)
    try:
        experiment = load_experiment(settings.experiments_dir / args.id, models)
    except ExperimentError as e:
        console.print(f"[red]Cannot load experiment:[/red] {escape(str(e))}", highlight=False)
        return 2
    if args.action == "status":
        console.print(_progress_table(progress(experiment)))
        return 0
    if args.dry_run:
        return _dry_run(console, models, experiment)
    return _run_experiment(console, settings, models, experiment)


def _list_experiments(console: Console, root: Path, models: dict[str, Model]) -> int:
    folders = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if not folders:
        console.print(f"No experiments in {root}", highlight=False)
        return 0
    table = Table(Column("Experiment", no_wrap=True), "Competitions done")
    for folder in folders:
        try:
            rows = progress(load_experiment(folder, models))
            done = f"{sum(row.done for row in rows)}/{len(rows)}"
        except ExperimentError as e:
            done = f"[red]invalid:[/red] {escape(str(e))}"
        table.add_row(folder.name, done)
    console.print(table)
    return 0


def _progress_table(rows: list[Progress]) -> Table:
    table = Table("#", "Players", "Runs", "Finished", "Failed", "Run folders")
    for number, row in enumerate(rows, 1):
        folders = (
            run.path.name if run.status == "finished" else f"{run.path.name} ({run.status})"
            for run in row.runs
        )
        table.add_row(
            str(number),
            " vs ".join(row.planned.players),
            str(row.planned.runs),
            f"[green]{row.finished}[/green]" if row.done else str(row.finished),
            str(row.failed),
            "\n".join(folders) or "-",
        )
    return table


def _dry_run(console: Console, models: dict[str, Model], experiment: Experiment) -> int:
    """Print the server preparation of each pending competition, as `run` would do it starting
    with no server running; starts, stops and asks nothing."""
    rows = progress(experiment)
    pending = [(number, row) for number, row in enumerate(rows, 1) if not row.done]
    if not pending:
        console.print("Nothing to play: every competition is done.")
        return 0
    providers = providers_of(models).values()
    running: list[str] = []
    for number, row in pending:
        used = {m.provider.name for m in _models_of(row.planned.players, models)}
        steps = plan(providers, running, used)
        running = [name for name in running if name in used] + list(steps.start)
        console.print(
            f"[bold]Competition {number} of {len(rows)}:[/bold] {' vs '.join(row.planned.players)}",
            highlight=False,
            soft_wrap=True,
        )
        _print_steps(console, stop=steps.stop, cleanup=steps.cleanup, start=steps.start)
    console.print("[bold]At the end:[/bold]")
    _print_steps(console, stop=running, cleanup=cleanups(providers))
    return 0


def _print_steps(console: Console, **steps: Iterable[str]) -> None:
    for label, names in steps.items():
        console.print(f"  {label}: {', '.join(names) or '-'}", highlight=False)


def _models_of(players: tuple[str, str], models: dict[str, Model]) -> list[Model]:
    return [resolve_player(name, models)[0] for name in players]


def _run_experiment(
    console: Console, settings: Settings, models: dict[str, Model], experiment: Experiment
) -> int:
    """Play the missing runs in file order, preparing the servers before each competition; a
    competition that does not finish is reported and the next one starts, unless it was
    stopped (Ctrl+C), which ends the experiment. Owned servers are stopped at the end, also
    after an error.

    Exit code 0 when every competition is done afterwards, 1 otherwise.
    """
    rows = progress(experiment)
    if all(row.done for row in rows):
        console.print("Nothing to play: every competition is done.")
    settings = dataclasses.replace(settings, runs_dir=experiment.runs_dir)
    failed: list[str] = []

    def say(text: str) -> None:
        console.print(text, highlight=False, soft_wrap=True)

    try:
        with Servers(models, settings.services_dir, say=say) as servers:
            for number, row in enumerate(rows, 1):
                if row.done:
                    continue
                try:
                    servers.prepare(_models_of(row.planned.players, models))
                except ServerError as e:
                    console.print(
                        f"[red]Competition {number}:[/red] {escape(str(e))}",
                        highlight=False,
                        soft_wrap=True,
                    )
                    status = "failed"
                else:
                    status = _play_runs(console, settings, models, experiment, row, number)
                if status == "stopped":
                    break
                if status != "finished":
                    failed.append(str(number))
    except KeyboardInterrupt:
        console.print("[yellow]Experiment stopped.[/yellow]")

    rows = progress(experiment)
    done = sum(row.done for row in rows)
    console.print(_progress_table(rows))
    failures = f"; failed this time: {', '.join(failed)}" if failed else ""
    console.print(f"{done} of {len(rows)} competitions done{failures}.", highlight=False)
    return 0 if done == len(rows) else 1


def _play_runs(
    console: Console,
    settings: Settings,
    models: dict[str, Model],
    experiment: Experiment,
    row: Progress,
    number: int,
) -> str:
    """Play the missing runs of one competition until one does not finish; its status."""
    total = len(experiment.competitions)
    status = "finished"
    for run in range(row.finished + 1, row.planned.runs + 1):
        console.print(
            f"[bold]Competition {number} of {total}:[/bold] "
            f"{' vs '.join(row.planned.players)}, run {run} of {row.planned.runs}",
            highlight=False,
            soft_wrap=True,
        )
        status = _attempt(console, settings, models, experiment, row.planned.players)
        if status != "finished":
            break
    return status


def _attempt(
    console: Console,
    settings: Settings,
    models: dict[str, Model],
    experiment: Experiment,
    players: tuple[str, str],
) -> str:
    """Play one competition of the experiment; its status, or `failed` when it could not start
    or raised, so one broken competition does not end the experiment."""
    try:
        state = _compete(
            console,
            settings,
            models,
            experiment.game,
            players,
            games=experiment.games,
            max_turns=experiment.max_turns,
            seed=None,
            visual=False,
        )
    except Exception:
        log.exception("competition %s vs %s failed", *players)
        return "failed"
    return "failed" if state is None else state.status


def _show(console: Console, run_dir: Path) -> int:
    try:
        state = load_state(run_dir)
    except (OSError, ValueError, TypeError, KeyError) as e:
        console.print(f"[red]Cannot read {run_dir}:[/red] {escape(str(e))}", highlight=False)
        return 2
    console.print(results_table(state))
    console.print(stats_table(state))
    if state.seed is not None:
        console.print(f"Seed: {state.seed}", highlight=False)
    master = MASTERS.get(state.game)
    _print_summary(console, state, duplicate=master is not None and master.game.duplicate)
    return 0


def _print_summary(console: Console, state: CompetitionState, *, duplicate: bool) -> None:
    """The board table of a game played in duplicate pairs, then the closing lines."""
    boards = board_table(state) if duplicate else None
    if boards is not None:
        console.print(boards)
    for line in summary_lines(state):
        console.print(line, highlight=False, soft_wrap=True)


@contextmanager
def _log_to(console: Console) -> Iterator[None]:
    """Send log records through the Live console so they print above the view."""
    root = logging.getLogger()
    saved = root.handlers
    root.handlers = [RichHandler(console=console, show_time=False, show_path=False)]
    try:
        yield
    finally:
        root.handlers = saved


def configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
