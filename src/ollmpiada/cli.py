"""Command line: argparse subcommands rendered with Rich."""

import argparse
import logging

from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table

from ollmpiada.config import ConfigError, load_models, load_settings


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return the process exit code."""
    parser = argparse.ArgumentParser(prog="ollmpiada", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("models", help="list configured models")
    args = parser.parse_args(argv)

    console = Console()
    try:
        models = load_models(load_settings().models_config)
    except ConfigError as e:
        console.print(f"[red]Invalid configuration:[/red] {escape(str(e))}", highlight=False)
        return 2

    if args.command == "models":
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
        console.print(table)
    return 0


def configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
