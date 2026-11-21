---
related: ["README.md", "turns.md", "../configuration.md", "../product.md"]
---

# End of game and statistics

[Game master](README.md)

How a game ends, how the turn limit is adjudicated, and how statistics are counted.

## End of game

| Condition | Result | Reason recorded |
| --- | --- | --- |
| Checkmate | Win for the side that mates | `checkmate` |
| Resignation | Win for the opponent | `resignation` |
| Draw offer accepted | Draw | `draw agreed` |
| Stalemate, insufficient material, 75-move rule, fivefold repetition | Draw | `stalemate`, `insufficient material`, `75-move rule`, `fivefold repetition` |
| Threefold repetition, 50-move rule | Draw, claimed automatically by the game master | `threefold repetition`, `50-move rule` |
| Turn limit reached (both sides made `max_turns` moves) | Adjudicated by the game: for chess a win for the side Stockfish sees at least 5 pawns ahead or with a forced mate, else a draw | `adjudication` |
| Third rejected answer in one turn | Win for the opponent | `illegal answers` |

- **Decision:** claimable draws are claimed automatically, so models cannot shuffle
  pieces until the turn limit. Only a repetition or 50-move count that has actually
  happened is claimed, not one the side to move could reach with its next move
  (as the `chess` library's `claim_draw=True` would), so the choice stays with the
  player.
- **Rule:** a game still running when both sides have made `max_turns` moves is
  adjudicated (`Game.adjudicate`) from its final position, and only then: there is
  no early adjudication during the game. For chess, Stockfish analyses the position
  to `ADJUDICATION_LIMIT` (depth 16, well under a second per position; capped at
  5 seconds so a hung engine times out) and the
  score from White's view decides: at least `ADJUDICATION_WIN_CP` (500 centipawns,
  5 pawns) for one side, or a forced mate for it, wins; anything else is a draw.
  The outcome's `score` keeps the evaluation, as centipawns from White's view
  (`+612 cp`, `-48 cp`) or a mate in N moves (`#+3` White mates, `#-2` Black
  mates), and the PGN ends with it as a comment (`{ Stockfish: +612 cp }`). The
  chess system prompt states the threshold in pawns, so the two change together (a
  test checks it). The game master runs `adjudicate` in a worker thread, so live
  timers and Ctrl+C stay responsive while the engine thinks.
- **Rule:** when adjudication fails (`AdjudicationError`; for chess the engine
  reports an error, its process died or it stopped responding), the game is aborted like a transport
  failure (see [validation](turns.md#validation-and-retries)): it is saved with its
  moves and no result, and the competition stops with the error shown.
- **Decision:** Stockfish is required for chess competitions, with no
  material-count fallback: a crude count would misjudge exactly the positions the
  turn limit leaves undecided. `ollmpiada run` starts the game's `session` before
  any model call and stops with exit code 2 and a message naming
  `OLLMPIADA_STOCKFISH` (see [environment](../configuration.md#environment)) when the
  engine does not start. The engine runs once per competition, in its own process
  group so Ctrl+C reaches only the app, and is closed when the competition ends.
  It referees; it never plays (see [product](../product.md#non-goals)).
- A draw offer stays open for the opponent's next answer only; any move by the
  opponent declines it.
- Fivefold repetition and the 75-move rule stay in the table for completeness:
  from a normal start the threefold and 50-move claims always come first.

## Statistics

Statistics are kept per player, where a player is a model id plus reasoning
effort (see [configuration](../configuration.md#model-fields)).

- **Games, wins, losses, draws** - counted when a game ends; aborted games are
  listed but not counted.
- **Rule:** wins, losses and draws count only games decided on the board.
  Adjudicated games (see [end of game](#end-of-game)) count in
  `adjudicated_wins`, `adjudicated_losses` and `adjudicated_draws` instead, so
  every counted game is in exactly one of the six and `games` is their sum. An
  engine's verdict is not a result the models played out, and keeping it apart
  shows how many games the turn limit decided.
- **Thinking time** - wall-clock time of every model call, rejected attempts
  included, game master work excluded. Shown as total and average per move.
- **Tokens** - input, output and reasoning tokens from PydanticAI's usage for LLM
  players; from the response for decision players when it reports any, otherwise
  shown as unknown, never as zero. A decision server that reports no output count
  but a `total_tokens` (SystemOne sends only `prompt_tokens` and `total_tokens`)
  gets `total_tokens` minus the input as its output, 0 when they match, because a
  decision model generates no text. Reasoning tokens are unknown on their own when
  a server reports usage without them (llama.cpp sends no
  `completion_tokens_details`).
- **Illegal attempts** - rejected answers, total and per game.

Each finished game is written to the competition folder in `.local/runs/` with its game
record (PGN for chess), result, reason, adjudication score and per-move costs; the competition file holds the settings,
prompt version and totals.
