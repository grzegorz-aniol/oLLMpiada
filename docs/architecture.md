---
related: ["README.md", "product.md", "game-master.md", "configuration.md"]
---

# Architecture

[Documentation index](README.md)

Owner of: components, data flow and the code map.

## Overview

```text
 ollmpiada run ──flags──▶ CLI ──start──▶ competition runner ──▶ runs/
                                            │  for each game
   terminal ◀── live view ◀──── events ─────┤
                                            ▼
                                       game master ──▶ game module (chess, …)
                                            │
                                ┌───────────┴───────────┐
                                ▼                       ▼
                           LLM player             decision player
                      (PydanticAI, OpenAI-       (httpx, SystemOne
                       compatible endpoint)          endpoint)
```

The CLI validates the arguments, starts one competition runner in an asyncio
event loop and hands the runner's events to the live view. The runner plays its
games in order through the game master, updates statistics, writes results and
publishes events. Everything runs in one process that exits when the competition
ends.

## Components

### Config

Loads `.env`, then `config/models.json`, validates both and exposes providers and
models. Contract: [configuration](configuration.md).

### Games

One module per game implementing the game interface owned by
[game master](game-master.md#game-abstraction), plus its prompt template.
Games are registered by name in `games/__init__.py`; `--game` picks one.

A game module is self-contained: rules, prompt, terminal board, side names and
game record all live in it. The rest of the app reaches a game only through the
interface and the registry; what adding a game may touch is ruled in
[game abstraction](game-master.md#game-abstraction).

### Players

One adapter per model `type`. A player receives the shared prompt and the turn's
legal moves, adds its own answer format, calls the model and returns the raw
answer with elapsed time and token usage. **Rule:** players never validate moves
or touch game state; that is the game master's job.

### Game master

Runs one game: turn loop, validation, retries, end of game. Contract:
[game master](game-master.md).

### Competition runner

Runs a series of games between two players: alternates sides, aggregates
statistics, writes results to `runs/`, publishes events, and handles a stop
request (the CLI sends one on Ctrl+C).

- **Decision:** visual and background mode run the same code. The mode only
  decides whether the live view renders the board; statistics and status update
  live in both.
- **Rule:** the runner knows nothing about the terminal. It only publishes events,
  so a later front end (a web UI, for example) subscribes to the same events
  without changes to the runner.

### CLI and terminal UI

argparse subcommands; Rich renders everything the user sees.

| Command | What it does |
| --- | --- |
| `ollmpiada models` | Lists configured models with type, reasoning efforts and availability |
| `ollmpiada probe <model>[@effort]` | Sends one opening-position turn and prints the parsed answer, time and tokens |
| `ollmpiada run <a>[@effort] <b>[@effort]` | Plays a competition; flags `--game` (default `chess`), `--games`, `--max-turns`, `--mode visual\|background` |
| `ollmpiada show <run-dir>` | Prints the results and statistics of a saved competition |

A model is named `<id>@<effort>`, as in
[model fields](configuration.md#model-fields). `run` checks the models and efforts
before anything starts, then shows a Rich
`Live` view: status line (games done of total, current game and turn), results
table, statistics table per player and, in visual mode, the game's board and the
move list with rejected attempts.

- **Rule:** `run` is scriptable. When stdout is not a terminal it prints one plain
  line per finished game and a final summary instead of the live view, whatever
  the mode. The exit
  code is 0 when the competition finished and non-zero when it was aborted or
  stopped.
- Ctrl+C stops the competition: the game in progress is aborted and not counted,
  finished games stay saved, and the competition status becomes `stopped`.

## Code map

Planned layout; replace this note with real entries as the modules land.

- `src/ollmpiada/__init__.py` - `main()`: load settings, run the CLI.
- `src/ollmpiada/cli.py` - argparse subcommands `models`, `probe`, `run`, `show`.
- `src/ollmpiada/config.py` - settings, model config models and loader.
- `src/ollmpiada/games/` - `base.py` (game interface, outcome types) and
  one module per game (`chess_game.py`, prompt template beside it).
- `src/ollmpiada/players/` - `base.py` (player interface, answer and usage
  types), `llm.py`, `decision.py`.
- `src/ollmpiada/game_master.py` - one game's turn loop.
- `src/ollmpiada/competition.py` - runner, statistics, events, persistence.
- `src/ollmpiada/tui.py` - Rich live view, results and statistics tables,
  plain output for scripts.
- `config/models.json` - providers and models.
- `runs/` - competition results, gitignored.
