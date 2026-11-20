"""Command line: argparse subcommands rendered with Rich."""

import argparse
import asyncio
import logging

from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table

from ollmpiada.config import ConfigError, Model, Settings, load_models, load_settings
from ollmpiada.games import GAMES
from ollmpiada.games.base import Game, IllegalMove
from ollmpiada.players import PlayerError, create_player
from ollmpiada.players.base import TransportError, TurnRequest

DEFAULT_MAX_TURNS = 100


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return the process exit code."""
    parser = argparse.ArgumentParser(prog="ollmpiada", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("models", help="list configured models")
    probe = commands.add_parser("probe", help="send one opening-position turn to a model")
    probe.add_argument("player", help="<model-id>[@effort]")
    probe.add_argument("--game", choices=GAMES, default="chess")
    args = parser.parse_args(argv)

    console = Console()
    try:
        settings = load_settings()
        models = load_models(settings.models_config)
    except ConfigError as e:
        console.print(f"[red]Invalid configuration:[/red] {escape(str(e))}", highlight=False)
        return 2

    if args.command == "models":
        console.print(_models_table(models))
        return 0
    return _probe(console, settings, models, args.player, GAMES[args.game])


def _models_table(models: dict[str, Model]) -> Table:
    table = Table(
        Column("Model", no_wrap=True), "Type", "Provider", "Reasoning efforts", "Available"
    )
    for m in models.values():
        table.add_row(
            m.id,
            m.spec.type,
            m.provider.name,
            ", ".join(m.spec.reasoning_efforts) or "-",
            "yes" if m.available else "[red]no[/red]",
        )
    return table


def _probe(
    console: Console, settings: Settings, models: dict[str, Model], name: str, game: Game
) -> int:
    """Exit code 0 when the model answered in the expected format."""
    try:
        player = create_player(name, models, timeout_s=settings.request_timeout_s)
    except PlayerError as e:
        console.print(f"[red]{escape(str(e))}[/red]", highlight=False)
        return 2
    state = game.new_state()
    legal = game.legal_moves(state)
    prompt = game.prompt(game.context(state, turn=1, max_turns=DEFAULT_MAX_TURNS))
    console.print(f"Probing {name} with the opening position of {game.name}...")
    try:
        response = asyncio.run(player.play(TurnRequest(prompt, legal)))
    except TransportError as e:
        console.print(f"[red]Transport error:[/red] {escape(str(e))}", highlight=False)
        return 1

    usage = response.usage
    table = Table.grid(padding=(0, 2))
    table.add_row("Raw answer", escape(response.raw) or "-")
    if response.answer is None:
        table.add_row("Parsed", f"[red]format error:[/red] {escape(str(response.format_error))}")
    else:
        table.add_row("Parsed", escape(repr(response.answer)))
        if response.answer.action == "move":
            try:
                move = game.parse_move(state, response.answer.move or "")
                table.add_row("Move", f"[green]legal[/green] ({legal[move]})")
            except IllegalMove as e:
                table.add_row("Move", f"[red]illegal:[/red] {escape(str(e))}")
    table.add_row("Time", f"{response.elapsed_s:.2f} s")
    if usage:
        reasoning = "unknown" if usage.reasoning_tokens is None else usage.reasoning_tokens
        tokens = f"in {usage.input_tokens}, out {usage.output_tokens}, reasoning {reasoning}"
    table.add_row("Tokens", tokens if usage else "unknown")
    console.print(table)
    return 0 if response.answer else 1


def configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
