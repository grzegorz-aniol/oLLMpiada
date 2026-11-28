---
related: ["README.md", "product.md", "game-master/README.md", "configuration.md", "experiments.md"]
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
                                  game master (GameMaster)
                                  └─ TurnBasedMaster ──▶ game module (chess, bridge)
                                            │
                                ┌───────────┴───────────┐
                                ▼                       ▼
                           LLM player             decision player
                      (PydanticAI; OpenAI        (httpx, SystemOne
                      completions or Anthropic       endpoint)
                       Messages endpoint)
```

The CLI validates the arguments, looks the game master up by `--game`, starts one
competition runner in an asyncio event loop and hands the runner's events to the
live view. The runner plays its games in order through the game master, updates
statistics, writes results and publishes events. Everything runs in one process
that exits when the competition ends.

## Components

### Config

Loads `.env`, then `config/models.json`, validates both and exposes providers and
models. Contract: [configuration](configuration.md).

### Games

One module per game implementing the game interface owned by
[game master](game-master/README.md#game-abstraction), plus its prompt templates
(system and user part).
Games are registered by name in `registry.py` (`MASTERS`), each wrapped in its game
master; `--game` picks one.

A game module is self-contained: rules, prompt, terminal board, side names, game
record and turn-limit adjudication (for chess through Stockfish) all live in it.
The rest of the app reaches a game only through the interface and the registry;
what adding a game may touch is ruled in
[game abstraction](game-master/README.md#game-abstraction).

### Players

One adapter per provider `api` (see [provider fields](configuration.md#provider-fields)). A player receives the game's prompt (system and user
part, LLM answer format and decision questions, all worded by the game) and the
turn's legal moves, shows its model the parts its transport uses, calls the model and
returns the raw answer with elapsed time and token usage. **Rule:** players never validate moves
or touch game state; that is the game master's job.

### Game master

Runs one game: turn loop, validation, retries, end of game. The runner and the CLI
call it through the `GameMaster` protocol; `TurnBasedMaster` implements it for every
game behind the `Game` interface. Contract:
[game master interface](game-master/turns.md#game-master-interface).

### Competition runner

Runs a series of games between two players: alternates sides, aggregates
statistics, writes results to `.local/runs/`, publishes events, and handles a stop
request (the CLI sends one on Ctrl+C).

- **Decision:** visual and background mode run the same code. The mode only
  decides whether the live view renders the board; statistics and status update
  live in both.
- **Rule:** the runner knows nothing about the terminal. It only publishes events,
  and the live view and the plain output for scripts subscribe to them.
- **Rule:** before the first game the runner warms up each player with one real
  call: the opening turn of a new game through the player's own adapter, so a
  server that loads models on demand (llama.cpp, a proxy) has the model ready and
  the first move is not spent on a cold start. The call is retried like a turn
  request after a transport failure (see
  [validation](game-master/turns.md#validation-and-retries)); its answer is
  discarded, so a format error still counts as warm, and it adds nothing to
  statistics, results or game events. The player who moves first in game 1 (by the
  side to move in that game's start) warms up last, so a server that holds one
  model at a time keeps that model loaded when the game starts. When a
  warm-up still fails, no game is played: the competition ends `aborted` with the
  error `warm-up call to <player> failed: <last error>`.

`Competition` in `competition.py` takes a game master, warms the players up
through its `warm_up`, then plays the games in order through its `play`. Each step runs as an asyncio
task, so `stop()` (Ctrl+C) cancels the warm-up or the game in progress, which is
discarded: not listed, not saved, not counted. It publishes the game
master's events plus `CompetitionProgress` (when the warm-up starts, when the
games start and after every counted game) and `CompetitionFinished`; both carry
the live `CompetitionState` (results, per-player `PlayerStats`, status
`warming up`, `running`, `finished`, `aborted` or `stopped`, which the live view's
status line shows). An aborted game ends the competition.

Results go to `<runs dir>/<YYYYmmdd-HHMMSS>_<a>_vs_<b>/`, where the runs dir is
`OLLMPIADA_RUNS_DIR` (default `.local/runs`, see
[configuration](configuration.md#environment)), or an experiment's `runs/` when
[`ollmpiada experiment run`](experiments.md#commands) plays it. Missing parent folders are
created; an existing run folder stops the run instead of mixing two competitions.
The files are rewritten after every game so a crash loses at most the game in
progress:

- `competition.json` - settings (game, players, games, max turns or `null` for a
  game without a turn limit, `seed`), the
  `CompetitionState` fields (per-player `stats` with the adjudicated counters and,
  in a game with points, `points` from
  [statistics](game-master/results.md#statistics)), and `prompt_version`, defined in
  [prompts](game-master/prompts.md#prompts).
- `game-NNN.<ext>` - the game's record, written for aborted games too. For chess
  it is `.pgn`: PGN with the result, a `Termination` header holding the reason, and
  for an adjudicated game the engine score as a comment after the last move
  (`1. e4 e5 { Stockfish: #-2 } 0-1`). For bridge it is `.pbn`, a PBN record of the
  table whose format is owned by [bridge](game-master/bridge.md#record).
- `game-NNN.json` - result, reason, `score` (the evaluation an
  [adjudicated](game-master/results.md#end-of-game) game was judged by, such as
  `"+612 cp"`; `null` otherwise), `points` (in side order, only in a game with
  [points](game-master/results.md#statistics)), side names and every turn with its attempts
  (raw answer, time, tokens, rejection reason; an empty list for a
  [forced opening move](game-master/turns.md#opening)).

`seed` is the competition's `--seed`, from which every game's start follows (see
[sides alternate](game-master/turns.md#turn-loop)). `ollmpiada show` reads
`competition.json` back with `load_state` and prints the seed under the tables; a
file without `seed` loads with none, and `show` then prints no seed line.

### CLI and terminal UI

argparse subcommands; Rich renders everything the user sees.

| Command | What it does |
| --- | --- |
| `ollmpiada models` | Lists configured models with API, reasoning efforts and availability |
| `ollmpiada probe <model>[@effort]` | Sends one turn in the game's start per answer format: for chess an [opening](game-master/turns.md#opening) turn (move only) and a move-6 turn (full answer), for a game whose turns are all move-only (bridge) one turn, the opening lead; prints each parsed answer, time and tokens and exits 0 only when every answer matches its format; flag `--game` |
| `ollmpiada run <a>[@effort] <b>[@effort]` | Plays a competition; flags `--game` (`chess`, the default, or `bridge`), `--games`, `--max-turns`, `--seed`, `--mode visual\|background` |
| `ollmpiada show <run-dir>` | Prints the results and statistics of a saved competition, with the [board table](game-master/results.md#statistics) for a game played in duplicate pairs |
| `ollmpiada experiment list\|status <id>\|run <id> [--dry-run]` | Lists experiments, shows one's competitions and runs, or plays its pending competitions one after another through the same code as `run`, starting and stopping their servers (`--dry-run` only prints that); owned by [experiments](experiments.md#commands) |

A model is named `<id>@<effort>`, as in
[model fields](configuration.md#model-fields). `run` checks the models and efforts
before anything starts, then shows a Rich
`Live` view: status line (games done of total, current game and where it stands,
from `Game.progress`: for chess the move per side, `move 24`, for bridge the
trick, `trick 12 of 13`), in visual
mode the game's board with the move list above it and the game statistics below
it, then the results table and the statistics table per player. The move list is
one wrapped line with the game's marks (`Game.move_mark`) before the turns it
marks, for chess the move number before White's move,
`(1) e4 e5 (2) Nf3 Nc6`, with rejected attempts inline in red as
`[✗ <side> <attempt>: <reason>]`, where `<side>` is the game's name for the
side whose answer was rejected (`[✗ Black 1: 'Nxf7' is not a legal move in this
position]`). It keeps a fixed height and shows its most recent
lines, so the board never moves or gets pushed off.

The game statistics cover the current game. One line shows when it started (wall
clock), how long it has run and the number of moves completed. Under it, a table
has one row per player in this game's side order, which swaps from game to game
(see [sides alternate](game-master/turns.md#turn-loop)): a `▶` marker on the player who
is thinking (the side to move), side and player, `Now` (how long the current turn
has run, empty for the other player), `Legal` (the number of legal moves of the running turn,
computed from the board, empty for the other player), `Total` (the player's turn times in this game, the running turn
included, like a chess clock), then the requests (counted as defined in
[game master](game-master/turns.md#turn-loop)) and the input, output and reasoning tokens. A turn's time
covers the whole turn, retries included, and restarts with each move. Token
counts not reported show as `–`, never as zero, and as `≥ n` when only some
answers reported them. When the game ends or the competition stops, the timers
freeze, the marker, `Now` and `Legal` disappear, and the last turn stays in its player's
`Total`.

Defaults: `--games 2` (a game played in duplicate pairs, such as bridge, rejects
an odd count, see [sides alternate](game-master/turns.md#turn-loop), and warns in
yellow before it starts when it plays fewer than `MIN_BOARDS` (8, in `cli.py`)
boards, since a few boards say little about skill; the run goes on),
`--max-turns 50` (moves per side) for a game with a
[turn limit](game-master/results.md#end-of-game) (a game without one rejects the
flag), `--seed` random below
2^31 (saved in `competition.json`, so a run can be replayed), `--mode visual`. The
two players must have different names, because statistics are kept per name; to
let a model play itself, give the two sides different efforts. How `run` starts
the engine that adjudicates chess is owned by
[end of game](game-master/results.md#end-of-game).
An adjudicated game shows its score after the reason, in the results table and
in plain lines (`0-1 (adjudication: -650 cp)`); the statistics table has an
`Adj. W/L/D` column and the summary lists adjudicated wins, losses and draws
apart. When a counted game has points, the statistics table adds a `Points`
column and each summary line ends with the player's points (see
[statistics](game-master/results.md#statistics)). `tui.py` holds the
renderers: `LiveView` (subscribes to runner events; the CLI refreshes Rich `Live`
after each event and once a second from the event loop, with no refresh thread,
so timers tick while a model thinks but a render never interleaves with an event
update; timers are computed at render time from the view's `clock`, which tests
replace; log warnings, such as a decision player asking
[per piece](game-master/prompts.md#answer-formats), go through the same console so they
print above the view),
`results_table`, `stats_table`, `board_table` (for a game played in duplicate pairs,
printed before the summary lines at the end of `run` and by `show`), `summary_lines`
(status, totals and the odd-game imbalance) and `PlainOutput`. `show` reuses the
same tables.

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

- `src/ollmpiada/__init__.py` - `main()`: load `.env`, configure logging, run the CLI.
- `src/ollmpiada/cli.py` - argparse subcommands `models`, `probe`, `run`, `show` and
  `experiment` (`list`, `status`, `run`); `_compete` plays one competition for both
  `run` and `experiment run`.
- `src/ollmpiada/experiment.py` - loads and validates `experiment.json`
  (`load_experiment`) and reads each planned competition's runs from `runs/`
  (`progress`); see [experiments](experiments.md).
- `src/ollmpiada/servers.py` - `Servers`, the providers' servers around an
  experiment's competitions: readiness, process-group start and stop, PID files,
  `cleanup` (`prepare`, `close`), and `plan`, the steps `--dry-run` prints; see
  [servers](experiments.md#servers).
- `src/ollmpiada/config.py` - settings, model config models and loader.
- `src/ollmpiada/registry.py` - `MASTERS`, the game masters by game name for
  `--game` (`chess`, `bridge`); outside `games/` because `game_master.py` imports
  `games.base`.
- `src/ollmpiada/games/` - `base.py` (game interface, side, outcome, prompt types, the
  opening rule and `strip_answer`, the answer cleanup every `parse_move` starts with)
  and one module per game with its two prompt templates beside it
  (`chess_game.py`, `chess_system_prompt.txt`, `chess_user_prompt.txt`;
  `bridge.py`, `bridge_system_prompt.txt`, `bridge_user_prompt.txt`).
- `src/ollmpiada/players/` - `__init__.py` (`create_player` from a
  `<id>[@effort]` name), `base.py` (player interface, answer and usage types),
  `llm.py`, `decision.py`.
- `src/ollmpiada/game_master.py` - the `GameMaster` protocol and `TurnBasedMaster`:
  one game's turn loop, the warm-up call before the first game and the probe's turn
  requests.
- `src/ollmpiada/competition.py` - runner, statistics, events, persistence, and the
  settings rules `run` and experiments share (`default_max_turns`, `check_games`).
- `src/ollmpiada/tui.py` - Rich live view, results and statistics tables,
  plain output for scripts.
- `config/models.json` - providers and models; gitignored, since it is each
  user's own setup (format in [configuration](configuration.md#model-config)).
- `.env.example` - the environment variables to copy into `.env`.
- `tests/` - test modules by area (`test_chess.py`, `test_bridge.py`,
  `test_game_master.py`, ...), `fakes.py` (scripted fake players and the fake
  server helpers), `fake_server.py` (a stand-in inference server a `start` command runs), `test_docs.py` (runs the docs check) and `test_golden.py` with `golden/` (the pinned model-facing requests); see [testing](testing.md).
- `scripts/check_docs.py` - the docs link and reachability check.
- `.local/runs/` - competition results, gitignored with the rest of `.local/`.
- `.local/experiments/` - [experiments](experiments.md), each with its own `runs/`.
- `.local/services/` - PID files and output of the [servers](experiments.md#servers)
  experiments start.
- `docs/images/` - images for the README and docs, as WebP sized for the page.
