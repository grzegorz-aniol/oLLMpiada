---
related: ["../README.md", "../architecture.md", "../configuration.md", "../product.md"]
---

# Game master

[Documentation index](../README.md)

Owner of: the game abstraction, the turn protocol, prompts, answer formats,
validation, end-of-game rules and how statistics are counted. The game master is
the referee: models only propose; the game master decides what happens.

- [Turns and retries](turns.md) - the turn loop, requests, side alternation,
  answer validation and retries.
- [Prompts and answer formats](prompts.md) - the shared prompt and its templates,
  LLM and decision-player answer formats.
- [End of game and statistics](results.md) - end conditions, adjudication at the
  turn limit, statistics.

## Game abstraction

A game is a two-player, turn-based, perfect-information game behind one
interface, so the game master, players and UI never import chess directly:

| Member | Purpose |
| --- | --- |
| `name`, `system_template`, `user_template` | Identifier (the `--game` value) and the two prompt template texts (see [prompts](prompts.md#prompts)) |
| `new_state()` | Starting position |
| `side_to_move(state)` | Which `Side` (`first` / `second`; for chess White / Black) moves |
| `legal_moves(state)` | Legal moves as `{id: label}`: a stable machine id (UCI `e2e4`) and a readable label (SAN `e4`) |
| `move_groups(state)` | The legal moves grouped per piece as `{key: {id: label}}`, for decision players (see [answer formats](prompts.md#answer-formats)); the base class returns no groups (`{}`), which a decision player treats as one group `all`; chess returns one group per origin square keyed like `knight_g1` |
| `parse_move(state, text)` | Turns a model's move text into a legal move id, or raises with a reason |
| `apply(state, move_id)` | Next state |
| `outcome(state)` | `None` while the game runs, else winner or draw with a reason |
| `adjudicate(state)` | The result of a game still running at the turn limit: winner or draw with reason `adjudication` and the `score` it was judged by; raises `AdjudicationError` when it cannot judge (see [end of game](results.md#end-of-game)) |
| `session(settings)` | Context manager holding what `adjudicate` needs for one competition (for chess the Stockfish engine); raises `GameSetupError` when that cannot start. The base class holds nothing |
| `describe(state)` | Position text for the prompt (board, recent moves, facts the rules need) |
| `side_names` | Display names of the two sides (for chess `White`, `Black`), used in tables and summaries |
| `render(state)` | Board for the terminal UI, as a Rich renderable |
| `context(state, ...)` | The prompt context for the side to move: position, legal moves, turn, draw offer, rejection (implemented once in the base class) |
| `prompt(context)` | The shared `Prompt`: its `system` and `user` parts, rendered from the two templates (implemented once in the base class) |
| `record(state, outcome, players)` | The game's native record saved with each game, and `record_extension` (for chess PGN, `pgn`) |

- **Rule:** a new game adds one module under `games/`, its prompt text and one
  registry entry; the game master, players, statistics, runner and terminal UI do
  not change.
- Chess uses the `chess` library for everything above. What its `parse_move`
  accepts is described under [validation](turns.md#validation-and-retries).
- States are immutable: `apply` returns a new state, so the game master can keep
  the position before a move.
