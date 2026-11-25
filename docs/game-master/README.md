---
related: ["../README.md", "../architecture.md", "../configuration.md", "../product.md"]
---

# Game master

[Documentation index](../README.md)

Owner of: the game abstraction, the game master interface, the turn protocol,
prompts, answer formats, validation, end-of-game rules, statistics and each game's
own rules. The game master is the referee: models only propose; it decides.

- [Turns and retries](turns.md) - game master interface, turn loop, side alternation,
  opening, validation.
- [Prompts and answer formats](prompts.md) - each game's prompt, LLM and decision formats.
- [End of game and statistics](results.md) - end conditions, adjudication, statistics.
- [Bridge](bridge.md) - contract rule, seats, card play, result, parsing, PBN record.

## Game abstraction

A game is a two-player, turn-based game behind one interface, so the game
master, players and UI never import chess directly:

| Member | Purpose |
| --- | --- |
| `name`, `system_template`, `user_template` | Identifier (the `--game` value) and the two prompt template texts (see [prompts](prompts.md#prompts)) |
| `new_state(seed)` | Starting position; a game with random starts derives it from `seed` alone, chess ignores it (see [sides alternate](turns.md#turn-loop)) |
| `side_to_move(state)` | Which `Side` (`first` / `second`; for chess White / Black) moves |
| `legal_moves(state)` | Legal moves as `{id: label}`: a stable machine id (UCI `e2e4`) and a readable label (SAN `e4`) |
| `move_groups(state)` | The legal moves grouped per piece as `{key: {id: label}}`, for decision players (see [answer formats](prompts.md#answer-formats)); the base class returns no groups (`{}`), which a decision player treats as one group `all`; chess returns one group per origin square keyed like `knight_g1` |
| `parse_move(state, text)` | Turns a model's move text into a legal move id, or raises with a reason |
| `apply(state, move_id)` | Next state |
| `outcome(state)` | `None` while the game runs, else winner or draw with a reason, and for a game that scores sides their `points` (see [statistics](results.md#statistics)) |
| `turn_limit` | Whether the game has a turn limit (`--max-turns`) and is adjudicated at it; `True` in the base class and for chess (see [end of game](results.md#end-of-game)) |
| `duplicate` | Whether the game compares the players over duplicate pairs, each start played once from each side, so a competition plays an even number of games (see [sides alternate](turns.md#turn-loop)); `False` in the base class and for chess, `True` for bridge |
| `adjudicate(state)` | The result of a game still running at the turn limit: winner or draw with reason `adjudication` and the `score` it was judged by; raises `AdjudicationError` when it cannot judge. The base class always raises; chess asks Stockfish (see [end of game](results.md#end-of-game)) |
| `move_only(turn)` | Whether a side's `turn` (its move number) allows only a move, with no resignation and no draw offer; the base class returns the [opening](turns.md#opening) rule, which chess keeps |
| `rejection(rule)` | The reason the game master gives a model whose answer breaks a referee rule (`Rejection`: a resignation or draw offer in a move-only turn, accepting a draw no one offered, the action `move` without a move), in the game's words (see [prompts](prompts.md#prompts)) |
| `forfeit(state, side, reason)` | The result when `side` stops answering, by resignation or illegal answers; the base class returns a win for the opponent, which chess keeps |
| `move_mark(state)` | The label the live view's move list prints before the move made in `state`, or `None`; the base class returns `None`, chess the move number before White's move (`(2)`), bridge the trick number and its leader before each lead (`T3 W:`) |
| `progress(state, turn)` | Where the game stands after `turn` moves, for the live view's [status line](../architecture.md#cli-and-terminal-ui); the base class counts moves per side (`move 24`), which assumes the sides alternate and chess keeps; bridge, whose four seats do not alternate as two sides, names the trick its board header shows (`trick 12 of 13`). **Decision:** kept apart from `move_mark`: in one state they can name different numbers (after the fourth card of trick 1 the board still shows trick 1 while the next lead is marked `T2`) and print different text |
| `session(settings)` | Context manager holding what `adjudicate` needs for one competition (for chess the Stockfish engine); raises `GameSetupError` when that cannot start. The base class holds nothing |
| `describe(state)` | Position text for the prompt (board, recent moves, facts the rules need); with hidden information (bridge), only what the side to move may know (see [prompts](prompts.md#prompts)) |
| `side_names` | Display names of the two sides (for chess `White`, `Black`), used in tables and summaries |
| `render(state)` | Board for the terminal UI, as a Rich renderable |
| `context(state, ...)` | The prompt context for the side to move: position, legal moves, turn, turn limit (`None` without one), whether the turn is move-only (`move_only(turn)`), draw offer and rejection (implemented once in the base class) |
| `prompt_values(context)` | Values for the placeholders only this game's templates use, every sentence worded per turn: chess the opening length and its draw-offer, choose-move and retry sentences, bridge its retry sentence (see [prompts](prompts.md#prompts)) |
| `answer_format(context)` | The LLM answer-format section of the turn, which names the heading of the game's legal-move list (see [answer formats](prompts.md#answer-formats)) |
| `questions` | The decision questions' wording, a `Questions` (see [answer formats](prompts.md#answer-formats)) |
| `prompt(context)` | The `Prompt` of a turn: its `system` and `user` parts, rendered from the two templates with the values every game has and `prompt_values`, plus `answer_format` and `questions` (implemented once in the base class, which adds no words) |
| `record(state, outcome, players, start)` | The game's native record saved with each game (`outcome` is `None` for an aborted game), and `record_extension` (for chess PGN, `pgn`; for bridge [PBN](bridge.md#record), `pbn`); `start` is the number of the game's [start](turns.md#turn-loop) in its competition, which bridge records as the board number and chess ignores |

- **Rule:** a new game adds one module under `games/`, its prompt text and one
  registry entry; the game master, players, statistics, runner and terminal UI do
  not change.
- Chess uses the `chess` library for everything above; its `parse_move` is under
  [validation](turns.md#validation-and-retries). States are immutable: `apply`
  returns a new state, so the game master can keep the position before a move.
- **Decision:** the referee rules that come from chess stay in the one turn loop
  and each game switches them with a hook: resigning and draw offers exist only in
  turns that are not `move_only`, adjudication runs only when `turn_limit` is true,
  and a loss off the board goes through `forfeit`. A game of moves alone, such as
  card play, switches them off instead of bringing a second loop. A chess-only
  master would copy the whole loop to change three conditions.
