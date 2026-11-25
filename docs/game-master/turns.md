---
related: ["README.md", "prompts.md", "results.md", "../architecture.md"]
---

# Turns and retries

[Game master](README.md)

The interface the runner calls the game master through, how the game master runs
a turn, and how it validates answers and retries.

## Game master interface

`Competition` and the CLI reach a game only through the `GameMaster` protocol in
`game_master.py`:

| Member | Purpose |
| --- | --- |
| `game` | The `Game` it referees; the runner reads its name, side names, records and prompt version from it, and the live view renders its board |
| `play(players, *, number, seed, max_turns, publish, backoff_s)` | Plays one game with `players` in side order, publishes its [events](#turn-loop) and returns the `GameResult`; the game starts from `game.new_state(seed)`, and `max_turns` is `None` for a game without a turn limit |
| `start_request(*, max_turns, turn)` | A turn request in the start of `SAMPLE_SEED`, for calls outside a game: the warm-up and `ollmpiada probe` |
| `warm_up(player, *, max_turns, backoff_s)` | Sends the player one opening turn and discards the answer (see [competition runner](../architecture.md#competition-runner)) |

`TurnBasedMaster(game)` implements it for every game behind the [game abstraction](README.md#game-abstraction):
the [turn loop](#turn-loop), validation and retries. `registry.py` maps
each `--game` name to its master (`MASTERS`).

- **Decision:** `GameMaster` is the seam between the runner and a game family,
  with one implementation for now. A game family whose play is not a turn loop over
  `Game` adds its own master instead of branches in `TurnBasedMaster`. Known limit:
  the live view still talks to the turn-based `Game` interface through
  `master.game`, so such a family brings its own view parts.

## Turn loop

1. Build the turn context: game rules, side to move, position description
   (with the recent moves), legal moves, turn number and limit, a pending draw
   offer, and the reason the previous attempt was rejected (on a retry).
2. Render the game's prompt from it, a system and a user part plus the game's
   answer format and decision questions (see [prompts](prompts.md#prompts)), and
   send it to the player through its adapter, which shows its model the parts its
   transport uses.
3. Validate the answer (see [validation](#validation-and-retries)). On rejection,
   repeat from step 1 with the reason.
4. Apply the action, record the move and its cost, publish an event.
5. Check the end of the game (see [end of game](results.md#end-of-game)); otherwise switch
   sides.

`TurnBasedMaster.play` in `game_master.py` runs this loop and publishes `GameStarted`,
`AnswerReceived`, `MoveMade`, `AnswerRejected` and `GameFinished` events to a
callback. `AnswerReceived` comes for every answered turn request, before
validation, with its time and usage, so a live view can count a game's requests
and tokens while it runs, resignations and rejected answers included. Its
`GameResult` holds the outcome (or the error of an aborted game), the final state
and one `Turn` per answered turn with every attempt's raw answer, time, tokens and
rejection reason (a [forced opening move](#opening) is a `Turn` with no attempts);
an aborted game keeps the attempts of its unfinished turn as
an `aborted` turn. A retry's prompt quotes the rejected answer (up to 300
characters) and the reason.

- **Rule:** a request is one answered turn request, the unit of `Attempt`,
  `AnswerReceived` and the live view's request count. A decision player answers a
  turn with one query, also when it asks for the piece first (see
  [answer formats](prompts.md#answer-formats)), so that turn is one request too.
- **Decision:** every turn is a fresh, stateless request carrying the full position.
  There is no conversation history between turns: both players get the same
  information, context stays bounded, and decision models are stateless anyway.
- **Rule:** sides alternate: in game *n* of a competition the first selected
  model plays the first side (White in chess) when *n* is odd. A competition with
  an odd number of games states that imbalance in its summary. A game compared
  over duplicate pairs (`Game.duplicate`, true for [bridge](bridge.md)) promises
  each start a second table in its prompt, so `Competition` rejects an odd number
  of games for it (`CompetitionSetupError`); `ollmpiada run` prints that error
  and exits with code 2 before any model call. `run` catches only that type, so
  any other `ValueError` from building the competition surfaces as a bug.
- **Rule:** games 2k-1 and 2k share a start. Game *n* plays start
  k = (n + 1) // 2 (`start_number` in `competition.py`) from
  `Game.new_state(seed + k - 1)` (`game_seed`), where `seed` is the competition's
  `--seed`, so each pair of games plays one start once from each side. The game's
  record gets k (`Game.record`), the board number of a [bridge](bridge.md#record)
  deal. Chess has a single starting position and ignores the seed. Calls outside a
  game (the warm-up, `ollmpiada probe`, `prompt_version`) use one start,
  `SAMPLE_SEED` (0, in `games/base.py`).
- **Decision:** duplicate pairs through seeds. In a game with a random start (a
  card deal), playing every start from both sides cancels the luck of the start,
  as duplicate bridge does; the side alternation above already swaps the sides
  within a pair. A saved seed replays the same starts with other models, so
  results compare across competitions. The `--seed` default is random and saved,
  so competitions do not all replay the same starts unless asked to.

## Opening

- **Rule:** a turn is move-only when `Game.move_only(turn)` says so: the player
  can only move, it cannot resign or offer a draw, so no draw offer can be pending
  to accept either. The default, which chess keeps, is the opening: each side's
  first `OPENING_MOVES` (5) moves, the first ten plies of a game, so every action
  is available from White's sixth move on. A game of moves alone (card play)
  returns true for every turn. The rule is the same for every player:
  `Game.context` (`games/base.py`) sets `PromptContext.move_only` from
  `Game.move_only`, the game master copies it into `TurnRequest.move_only`, and
  each adapter then asks for a move alone (see
  [answer formats](prompts.md#answer-formats)). The chess prompt states
  the opening rule (see [prompts](prompts.md#prompts)). An answer
  that still resigns or offers a draw in a move-only turn is rejected like an
  illegal move. When the side to move has exactly one legal move in a move-only
  turn, the game master plays it itself without asking the player: nothing is
  sent, no `AnswerReceived` is published, and the `Turn` is a move with no
  attempts, so it counts as a move with no call, time or tokens (see
  [statistics](results.md#statistics)). In a turn that is not move-only a player
  with one legal move is still asked, because resigning and offering a draw are
  real choices there.
- **Decision:** the rule exists because a deterministic decision model can resign
  every game at move 0. In the experiment of 2026-11-22, basal-2-max as White
  answered the action question on the starting position with `resign`
  (probability 0.72 against 0.28 for `move`) and lost all 78 of its White games by
  resignation without a move, while it played normally as Black. Nobody has a
  sound reason to resign or agree a draw in the first five moves, so not offering
  either removes this failure mode at no cost.
- **Decision:** the game master, not the adapter, plays a forced opening move,
  for every player type. A decision model cannot be asked a question with a single
  option (SystemOne templates refuse it; Basal's accepts 2 to 26), and treating
  every player the same keeps the opening identical for both sides and the
  statistics exact: no player is charged a call, time or tokens for a move it could
  not choose.

## Validation and retries

An answer is rejected when it does not match the format, the action is `move` with
a missing or illegal move, the action is `accept_draw` with no pending offer, or it
resigns or offers a draw in a move-only turn (see [opening](#opening)).

- **Rule:** a player gets three attempts per turn. The third rejection loses the
  game with reason `illegal answers`, with the result the game's `forfeit` gives (a
  win for the opponent by default). Each rejection counts as an illegal attempt.
- **Rule:** transport failures (timeout, connection error, HTTP 5xx) are not the
  model's fault: retry twice with backoff, then abort the game and stop the
  competition with the error shown. Aborted games do not count as wins, losses or
  draws. A failure that depends only on the position and the configuration (more
  piece groups than `max_options`) is a `PermanentError`, a `TransportError` that
  is never retried: it aborts at once with the same error.
- **Rule:** every game's `parse_move` starts with `strip_answer` (`games/base.py`),
  which strips whitespace, markdown and quoting wrappers (`**e4**`, `` `e4` ``,
  quotes, parentheses), `!`, `?`, `.`, `,`, `;` and `…` at either end of the answer.
- **Rule:** chess `parse_move` reads the move inside common answer habits before
  matching: after `strip_answer` it strips move-number
  prefixes (`1. e4`, `1.e4`, `12... Nf6`, `12. ...Nf6`) and an en passant mark
  (`exd6 e.p.`, `exd6 ep`), and joins split squares (`e2 e4`). It then tries the
  exact UCI id (`g1f3`, any case), SAN (`Nf3`, `Ng1-f3`, `Nf3+`, `e8=Q`), then a
  loose match that ignores case and check marks and reads `0-0` as `O-O`. An
  answer that fits several legal moves is rejected with every reading, so the
  retry can fix it: under-specified SAN (`Nd2` with knights on b1 and f1) or a
  case-blind match (`BXC4`: pawn or bishop). A promotion without a piece
  (`e7e8`, `e8`) is rejected with its own reason naming an example (`e7e8q`), or
  as ambiguous when it can also read as another move (`bxc8` with a bishop that
  can also take on c8).
  Player adapters pass the move text through unchanged, so `parse_move` is the
  only place answers are cleaned.
- Players raise `TransportError` for every failed call, HTTP 4xx included (bad
  request, authentication, a decision server refusing too many options): a retry
  rarely fixes those, but they are not the model's answer either, so they stop the
  game loudly instead of counting as illegal attempts. A reply that arrives but
  does not match the answer format is returned as a format error and counts as a
  rejected attempt.
