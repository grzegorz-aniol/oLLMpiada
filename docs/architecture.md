---
related: ["README.md", "product.md", "game-master.md", "configuration.md"]
---

# Architecture

[Documentation index](README.md)

Owner of: components, data flow and the code map.

## Overview

```text
 ollmpiada run ──flags──▶ CLI ──start──▶ competition runner ──▶ .local/runs/
                                            │  for each game
   terminal ◀── live view ◀──── events ─────┤
                                            ▼
                                       game master ──▶ game module (chess, …)
                                            │
                                ┌───────────┴───────────┐
                                ▼                       ▼
                           LLM player             decision player
                      (PydanticAI; OpenAI        (httpx, SystemOne
                      completions or Anthropic       endpoint)
                       Messages endpoint)
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
[game master](game-master.md#game-abstraction), plus its prompt templates
(system and user part).
Games are registered by name in `games/__init__.py`; `--game` picks one.

A game module is self-contained: rules, prompt, terminal board, side names and
game record all live in it. The rest of the app reaches a game only through the
interface and the registry; what adding a game may touch is ruled in
[game abstraction](game-master.md#game-abstraction).

### Players

One adapter per provider `api` (see [provider fields](configuration.md#provider-fields)). A player receives the shared prompt (system and user
part) and the turn's legal moves, adds its own answer format, calls the model and
returns the raw answer with elapsed time and token usage. **Rule:** players never validate moves
or touch game state; that is the game master's job.

### Game master

Runs one game: turn loop, validation, retries, end of game. Contract:
[game master](game-master.md).

### Competition runner

Runs a series of games between two players: alternates sides, aggregates
statistics, writes results to `.local/runs/`, publishes events, and handles a stop
request (the CLI sends one on Ctrl+C).

- **Decision:** visual and background mode run the same code. The mode only
  decides whether the live view renders the board; statistics and status update
  live in both.
- **Rule:** the runner knows nothing about the terminal. It only publishes events,
  so a later front end (a web UI, for example) subscribes to the same events
  without changes to the runner.

`Competition` in `competition.py` plays the games in order, each as an asyncio
task, so `stop()` (Ctrl+C) cancels the game in progress, which is discarded: not
listed, not saved, not counted. It publishes the game
master's events plus `CompetitionProgress` (at the start and after every counted
game) and `CompetitionFinished`; both carry the live `CompetitionState` (results,
per-player `PlayerStats`, status `running`, `finished`, `aborted` or `stopped`).
An aborted game ends the competition.

Results go to `<runs dir>/<YYYYmmdd-HHMMSS>_<a>_vs_<b>/`, where the runs dir is
`OLLMPIADA_RUNS_DIR` (default `.local/runs`, see
[configuration](configuration.md#environment)). Missing parent folders are
created; an existing run folder stops the run instead of mixing two competitions.
The files are rewritten after every game so a crash loses at most the game in
progress:

- `competition.json` - settings (game, players, games, max turns), the
  `CompetitionState` fields, and `prompt_version`, defined in
  [prompts](game-master.md#prompts).
- `game-NNN.<ext>` - the game's record (`.pgn` for chess), written for aborted
  games too.
- `game-NNN.json` - result, reason, side names and every turn with its attempts
  (raw answer, time, tokens, rejection reason).

`ollmpiada show` reads `competition.json` back with `load_state`.

### CLI and terminal UI

argparse subcommands; Rich renders everything the user sees.

| Command | What it does |
| --- | --- |
| `ollmpiada models` | Lists configured models with API, reasoning efforts and availability |
| `ollmpiada probe <model>[@effort]` | Sends one opening-position turn and prints the parsed answer, time and tokens |
| `ollmpiada run <a>[@effort] <b>[@effort]` | Plays a competition; flags `--game` (default `chess`), `--games`, `--max-turns`, `--mode visual\|background` |
| `ollmpiada show <run-dir>` | Prints the results and statistics of a saved competition |

A model is named `<id>@<effort>`, as in
[model fields](configuration.md#model-fields). `run` checks the models and efforts
before anything starts, then shows a Rich
`Live` view: status line (games done of total, current game and turn), in visual
mode the game's board with the move list above it and the game statistics below
it, then the results table and the statistics table per player. The move list is
one wrapped line numbered by move pair,
`(1) e4 e5 (2) Nf3 Nc6`, with rejected attempts inline in red as
`[✗ <attempt>: <reason>]`. It keeps a fixed height and shows its most recent
lines, so the board never moves or gets pushed off.

The game statistics cover the current game. One line shows when it started (wall
clock), how long it has run and the number of moves completed. Under it, a table
has one row per player in this game's side order, which swaps from game to game
(see [sides alternate](game-master.md#turn-loop)): a `▶` marker on the player who
is thinking (the side to move), side and player, `Now` (how long the current turn
has run, empty for the other player), `Total` (the player's turn times in this game, the running turn
included, like a chess clock), then the requests (answered model calls, counted
as in the statistics) and the input, output and reasoning tokens. A turn's time
covers the whole turn, retries included, and restarts with each move. Token
counts not reported show as `–`, never as zero, and as `≥ n` when only some
answers reported them. When the game ends or the competition stops, the timers
freeze, the marker and `Now` disappear, and the last turn stays in its player's
`Total`.

Defaults: `--games 2`, `--max-turns 100` (moves per side), `--mode visual`. The
two players must have different names, because statistics are kept per name; to
let a model play itself, give the two sides different efforts. `tui.py` holds the
renderers: `LiveView` (subscribes to runner events; the CLI refreshes Rich `Live`
after each event and once a second from the event loop, with no refresh thread,
so timers tick while a model thinks but a render never interleaves with an event
update; timers are computed at render time from the view's `clock`, which tests
replace; log warnings go through the same console so they print above the view),
`results_table`, `stats_table`, `summary_lines` (status, totals
and the odd-game imbalance) and `PlainOutput`. `show` reuses the same tables.

- **Rule:** `run` is scriptable. When stdout is not a terminal it prints one plain
  line per finished game and a final summary instead of the live view, whatever
  the mode. A game line reads
  `game 1: White <a> vs Black <b>: 0-1 (checkmate), 4 moves`; plain lines never
  wrap, and the last line names the run folder. The exit
  code is 0 when the competition finished and non-zero when it was aborted or
  stopped.
- Ctrl+C stops the competition: the game in progress is discarded (not listed or
  counted), finished games stay saved, and the competition status becomes
  `stopped`.

## Code map

Planned layout; replace this note with real entries as the modules land.

- `src/ollmpiada/__init__.py` - `main()`: load `.env`, configure logging, run the CLI.
- `src/ollmpiada/cli.py` - argparse subcommands `models`, `probe`, `run`, `show`.
- `src/ollmpiada/config.py` - settings, model config models and loader.
- `src/ollmpiada/games/` - `base.py` (game interface, outcome types) and
  one module per game (`chess_game.py`, prompt templates beside it).
- `src/ollmpiada/players/` - `__init__.py` (`create_player` from a
  `<id>[@effort]` name), `base.py` (player interface, answer and usage types),
  `llm.py`, `decision.py`.
- `src/ollmpiada/game_master.py` - one game's turn loop.
- `src/ollmpiada/competition.py` - runner, statistics, events, persistence.
- `src/ollmpiada/tui.py` - Rich live view, results and statistics tables,
  plain output for scripts.
- `config/models.json` - providers and models.
- `.local/runs/` - competition results, gitignored with the rest of `.local/`.
- `docs/images/` - images for the README and docs, as WebP sized for the page.
