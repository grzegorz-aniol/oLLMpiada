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
| Resignation (not in a move-only turn, such as the [opening](turns.md#opening)) | The game's `forfeit`: by default, and for chess, a win for the opponent | `resignation` |
| Draw offer accepted (offers start after the [opening](turns.md#opening)) | Draw | `draw agreed` |
| Stalemate, insufficient material, 75-move rule, fivefold repetition | Draw | `stalemate`, `insufficient material`, `75-move rule`, `fivefold repetition` |
| Threefold repetition, 50-move rule | Draw, claimed automatically by the game master | `threefold repetition`, `50-move rule` |
| Turn limit reached (both sides made `max_turns` moves; only in a game with `turn_limit`) | Adjudicated by the game: for chess a win for the side Stockfish sees at least 5 pawns ahead or with a forced mate, else a draw | `adjudication` |
| All 52 cards played ([bridge](bridge.md#result)) | Win for North-South when the contract is made, else for East-West | `made`, `down` |
| Third rejected answer in one turn | The game's `forfeit`: by default, and for chess, a win for the opponent | `illegal answers` |

- **Decision:** claimable draws are claimed automatically, so models cannot shuffle
  pieces until the turn limit. Only a repetition or 50-move count that has actually
  happened is claimed, not one the side to move could reach with its next move
  (as the `chess` library's `claim_draw=True` would), so the choice stays with the
  player.
- **Rule:** in a game with a turn limit (`Game.turn_limit`, true for chess), a
  game still running when both sides have made `max_turns` moves is adjudicated
  (`Game.adjudicate`) from its final position, and only then: there is no early
  adjudication during the game. A game without a turn limit is never adjudicated:
  it ends by its rules or a forfeit. `ollmpiada run` then rejects `--max-turns`,
  and `competition.json` saves `max_turns` as `null`. For chess, Stockfish analyses the position
  to `ADJUDICATION_LIMIT` (depth 16, well under a second per position; its 5-second
  time cap makes python-chess give up on a hung engine after that time plus its
  10-second communication timeout, about 15 seconds) and the score from White's
  view decides: at least `ADJUDICATION_WIN_CP` (500 centipawns, 5 pawns) for one
  side, or a forced mate for it, wins; anything else is a draw.
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
- **Points** - a game may score each side (`Outcome.points`, in side order; for
  card play the tricks each pair took). A player's `points` sum its side's points
  over its counted games, whichever side it played. A game with points shows them
  as its result (`9-4`, not `1-0`). The statistics table, the summary lines and
  the saved files carry points only when a counted game has them, so chess
  output and files have none.
- **Decision:** points sit beside wins and losses, not in their place: the game's
  rules still name the winner (for card play, contract made or beaten), so the
  W/L statistics keep their meaning, and points add the margin. Over a duplicate
  pair (one start played once from each side, see
  [sides alternate](turns.md#turn-loop)) of 13 tricks, A scores `t1 + (13 - t2)`
  and B `(13 - t1) + t2`, where `t1` and `t2` are the first side's tricks with A
  and with B on it, so the difference `2·(t1 - t2)` is the duplicate comparison.
- **Board table** - for a game played in duplicate pairs (`Game.duplicate`), `run`
  and `show` print a `Boards` table (`board_table`) before the summary lines: one row
  per board (games 2k-1 and 2k) with both tables counted, each player's points at
  table 1 + table 2 (`9 + 6`) and the board's result, the winner and margin
  (`<a> by 4`) or `tie`. W/L counts a board where both tables took the same tricks
  as a win each; the table shows it as the tie it is. It is built from the saved
  `points` of each game, so saved competitions need no new field.
- **Thinking time** - wall-clock time of every model call, rejected attempts
  included, game master work excluded. Shown as total and average per move.
- **Forced opening moves** - a [forced opening move](turns.md#opening), which the
  game master plays without asking the player, counts as a move with no call: it
  adds to `moves` but not to `calls`, `calls_without_usage`, thinking time or
  tokens, so it lowers the per-move averages slightly.
- **Tokens** - input, output and reasoning tokens from PydanticAI's usage for LLM
  players; from the response for decision players when it reports any, otherwise
  shown as unknown, never as zero. A call that reported no usage counts in
  `calls_without_usage`; totals that miss some calls show as `≥ n`. A decision
  server that reports no output count but a `total_tokens` (SystemOne sends only `prompt_tokens` and `total_tokens`)
  gets `total_tokens` minus the input as its output, 0 when they match, because a
  decision model generates no text. Reasoning tokens are unknown on their own when
  a server reports usage without them (llama.cpp sends no
  `completion_tokens_details`).
- **Illegal attempts** - rejected answers, total and per game.

Each finished game is written to the competition folder in `.local/runs/` with its game
record (PGN for chess, PBN for bridge), result, reason, adjudication score, points (in a game
with points) and per-move costs; the competition file holds the settings,
prompt version and totals.
