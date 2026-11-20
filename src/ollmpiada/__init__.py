import sys
from pathlib import Path

from dotenv import load_dotenv

from ollmpiada.cli import configure_logging
from ollmpiada.cli import main as cli_main


def main() -> None:
    """Console entry point: load `.env` (shell values win), then run the CLI."""
    load_dotenv(Path(".env"), override=False)
    configure_logging()
    sys.exit(cli_main())
