---
related: ["README.md", "prompts.md", "results.md"]
---

# Turns and retries

[Game master](README.md)

How the game master runs a turn, and how it validates answers and retries.

## Turn loop

1. Build the turn context: game rules, side to move, position description
   (with the recent moves), legal moves, turn number and limit, a pending draw
   offer, and the reason the previous attempt was rejected (on a retry).
2. Render the shared prompt from it, a system and a user part (see
   [prompts](prompts.md#prompts)), and send it to the player through its adapter, which
   adds the answer format.
3. Validate the answer (see [validation](#validation-and-retries)). On rejection,
   repeat from step 1 with the reason.
4. Apply the action, record the move and its cost, publish an event.
5. Check the end of the game (see [end of game](results.md#end-of-game)); otherwise switch
   sides.

`play_game` in `game_master.py` runs this loop and publishes `GameStarted`,
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
  an odd number of games states that imbalance in its summary.

## Opening

- **Rule:** during each side's first `OPENING_MOVES` (5) moves, the first ten
  plies of a game, a player can only move: it cannot resign or offer a draw, so no
  draw offer can be pending to accept either. From White's sixth move on, every
  action is available. The rule is the same for every player:
  `PromptContext.move_only` (`games/base.py`) is true while the side's turn number
  is at most `OPENING_MOVES`, the game master copies it into
  `TurnRequest.move_only`, and each adapter then asks for a move alone (see
  [answer formats](prompts.md#answer-formats)). The shared prompt states the rule
  (see [prompts](prompts.md#prompts)). An answer that still resigns or offers a draw
  in the opening is rejected like an illegal move. When the side to move has
  exactly one legal move in the opening, the game master plays it itself without
  asking the player: nothing is sent, no `AnswerReceived` is published, and the
  `Turn` is a move with no attempts, so it counts as a move with no call, time or
  tokens (see [statistics](results.md#statistics)). After the opening a player
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
resigns or offers a draw in the [opening](#opening).

- **Rule:** a player gets three attempts per turn. The third rejection loses the
  game with reason `illegal answers`. Each rejection counts as an illegal attempt.
- **Rule:** transport failures (timeout, connection error, HTTP 5xx) are not the
  model's fault: retry twice with backoff, then abort the game and stop the
  competition with the error shown. Aborted games do not count as wins, losses or
  draws. A failure that depends only on the position and the configuration (more
  piece groups than `max_options`) is a `PermanentError`, a `TransportError` that
  is never retried: it aborts at once with the same error.
- **Rule:** chess `parse_move` reads the move inside common answer habits before
  matching: it strips whitespace, markdown and quoting wrappers (`**e4**`,
  `` `e4` ``, quotes, parentheses), `!`, `?`, `.`, `,`, `;` and `…` at either end, move-number
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
  Player adapters pass the move text through unchanged, so this is the only
  place answers are cleaned.
- Players raise `TransportError` for every failed call, HTTP 4xx included (bad
  request, authentication, a decision server refusing too many options): a retry
  rarely fixes those, but they are not the model's answer either, so they stop the
  game loudly instead of counting as illegal attempts. A reply that arrives but
  does not match the answer format is returned as a format error and counts as a
  rejected attempt.
