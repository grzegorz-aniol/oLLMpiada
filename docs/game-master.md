---
related: ["README.md", "architecture.md", "configuration.md", "product.md"]
---

# Game master

[Documentation index](README.md)

Owner of: the game abstraction, the turn protocol, prompts, answer formats,
validation, end-of-game rules and how statistics are counted. The game master is
the referee: models only propose; the game master decides what happens.

## Game abstraction

A game is a two-player, turn-based, perfect-information game behind one
interface, so the game master, players and UI never import chess directly:

| Member | Purpose |
| --- | --- |
| `name`, `system_template`, `user_template` | Identifier (the `--game` value) and the two prompt template texts (see [prompts](#prompts)) |
| `new_state()` | Starting position |
| `side_to_move(state)` | Which `Side` (`first` / `second`; for chess White / Black) moves |
| `legal_moves(state)` | Legal moves as `{id: label}`: a stable machine id (UCI `e2e4`) and a readable label (SAN `e4`) |
| `move_groups(state)` | The legal moves grouped per piece as `{key: {id: label}}`, for decision players (see [answer formats](#answer-formats)); the base class returns no groups (`{}`), which a decision player treats as one group `all`; chess returns one group per origin square keyed like `knight_g1` |
| `parse_move(state, text)` | Turns a model's move text into a legal move id, or raises with a reason |
| `apply(state, move_id)` | Next state |
| `outcome(state)` | `None` while the game runs, else winner or draw with a reason |
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
  accepts is described under [validation](#validation-and-retries).
- States are immutable: `apply` returns a new state, so the game master can keep
  the position before a move.

## Turn loop

1. Build the turn context: game rules, side to move, position description
   (with the recent moves), legal moves, turn number and limit, a pending draw
   offer, and the reason the previous attempt was rejected (on a retry).
2. Render the shared prompt from it, a system and a user part (see
   [prompts](#prompts)), and send it to the player through its adapter, which
   adds the answer format.
3. Validate the answer (see [validation](#validation-and-retries)). On rejection,
   repeat from step 1 with the reason.
4. Apply the action, record the move and its cost, publish an event.
5. Check the end of the game (see [end of game](#end-of-game)); otherwise switch
   sides.

`play_game` in `game_master.py` runs this loop and publishes `GameStarted`,
`AnswerReceived`, `MoveMade`, `AnswerRejected` and `GameFinished` events to a
callback. `AnswerReceived` comes for every answered turn request, before
validation, with its time and usage, so a live view can count a game's requests
and tokens while it runs, resignations and rejected answers included. Its
`GameResult` holds the outcome (or the error of an aborted game), the final state
and one `Turn` per answered turn with every attempt's raw answer, time, tokens and
rejection reason; an aborted game keeps the attempts of its unfinished turn as
an `aborted` turn. A retry's prompt quotes the rejected answer (up to 300
characters) and the reason.

- **Rule:** a request is one answered turn request, the unit of `Attempt`,
  `AnswerReceived` and the live view's request count. A decision player answers a
  turn with one query, also when it asks for the piece first (see
  [answer formats](#answer-formats)), so that turn is one request too.
- **Decision:** every turn is a fresh, stateless request carrying the full position.
  There is no conversation history between turns: both players get the same
  information, context stays bounded, and decision models are stateless anyway.
- **Rule:** sides alternate: in game *n* of a competition the first selected
  model plays the first side (White in chess) when *n* is odd. A competition with
  an odd number of games states that imbalance in its summary.

## Prompts

- **Rule:** both players receive the same prompt text, built from one pair of
  templates. The only differences are those the position dictates (own side, own
  turn) and the answer format, which each player adapter adds.
- **Rule:** the prompt is written to make the model play to win: it states the
  goal (win; a draw beats a loss), that the opponent is another AI model, the
  stakes of each rule below, and asks the model to check its move against the
  legal list before answering.
- **Decision:** the prompt has two parts. The **system** part holds what is true
  for the whole game: role and goal, game rules and referee rules; the LLM adapter
  adds its answer format to it. The **user** part holds only the turn at hand: the
  current state and what to answer now. Models weigh system instructions as
  standing rules, and the per-turn message stays short.

System part, in order:

1. **Role and goal** - which side the model plays and that it must try to win.
2. **Rules** - the game rules in brief, plus the referee's rules: turn limit and
   its result, illegal-answer limit and its penalty, automatic draws, how to
   resign, offer or accept a draw.

User part, in order:

1. **Position** - move number, side to move, the board (for chess: FEN plus an
   ASCII diagram with coordinates), the recent moves (SAN with move numbers), the
   opponent's last move, the halfmove clock, a warning when the current position
   has occurred before, this turn against the limit and turns left, a pending
   draw offer.
2. **Legal moves** - every legal move label and the instruction to pick one. Both
   players get it, so it does not favor either; it turns a test of rule recall
   into a test of play.
3. **Previous attempt** (retries only) - the rejected answer and why.

- **Decision:** chess shows only the last `RECENT_PLIES` (2) plies, the
  opponent's last move and the player's own previous one, not the full move
  history: FEN and board carry the position, and the halfmove clock
  (`Halfmove clock: N (automatic draw at 100)`) carries the 50-move rule. The
  repetition line is only a hint that positions are repeating: it reports that
  the current position has occurred twice, so a move into another position seen
  twice before can still end the game by threefold repetition without a warning.
  The system part says these lines exist.

Templates live next to their game as plain text files
(`games/chess_system_prompt.txt`, `games/chess_user_prompt.txt`) so they can be
reworded without code changes. Both use the same `string.Template` placeholders:
`$side`, `$opponent`, `$position`, `$legal_moves`, `$turn`, `$max_turns`,
`$turns_left`, `$draw_offer` and `$previous_attempt` (empty except on retries).
The draw-offer and previous-attempt sentences are worded in `Game.prompt`
(`games/base.py`), because `string.Template` has no conditionals.

- **Rule:** the prompt version recorded with a competition covers both parts, the
  wording in `Game.prompt` and the LLM answer format, so runs with different
  prompts are not compared by accident. `prompt_version` in `competition.py`
  hashes rendered sample prompts with the system/user boundary kept.

## Answer formats

All answers carry the same three things: an action (`move`, `resign`,
`accept_draw`), a move when the action is `move`, and an optional draw offer made
together with the move.

**LLM players** (`openai-completions` and `anthropic-messages` providers) use a
PydanticAI `Agent` whose `output_type` is the answer model:

```python
class TurnAnswer(BaseModel):
    action: Literal["move", "resign", "accept_draw"]
    move: str | None = None  # SAN or UCI; required when action == "move"
    offer_draw: bool = False
```

The adapter sends the system part as PydanticAI `instructions` (a `system`
message for OpenAI chat completions, the `system` field for Anthropic messages)
with a short ANSWER FORMAT section naming the three fields appended, and the user
part as the user prompt. The chosen reasoning effort is mapped per API, as
described in [model fields](configuration.md#model-fields), and so are the
optional `max_tokens` cap and `reasoning_budget`. The adapter sets both
`request_timeout` (the deadline of the whole call) and `timeout` (the SDK
client's per-attempt limit, 600 s by
default, which `request_timeout` alone cannot raise) from the provider's
`timeout_s` or else `OLLMPIADA_REQUEST_TIMEOUT_S` (see
[provider fields](configuration.md#provider-fields)), and turns off the provider
client's own retries, so the game master owns every retry. The provider's
`headers` go out as `extra_headers`. Without a token the `Authorization` header is
omitted per request (`extra_headers` with the SDK's `Omit`), because both the
OpenAI and the Anthropic clients otherwise always send the key as a bearer token.
Reasoning tokens are known when the model reports them (OpenAI
`completion_tokens_details`, Anthropic thinking tokens) and unknown otherwise.

- **Sensitive:** PydanticAI retries invalid structured output on its own. Set its
  output retries to zero and let the game master retry, so every rejection is
  counted, timed and visible.
- **Sensitive:** local servers differ in tool-calling support. If tool-based output
  fails for a model, switch that model to PydanticAI `NativeOutput` or
  `PromptedOutput`; this may become a per-model config field.

**Decision players** call the SystemOne API: `POST {model, state, questions}`,
where `state` is the shared prompt text, system part then user part, and every
question is a `choice`:

| Question | Criteria |
| --- | --- |
| `action` | `move`, `resign`, plus `accept_draw` only while an offer is pending |
| `move` | every legal move, keyed by its id (`e2e4`), described by its label (`e4`); replaced by `piece` and `move_<key>` past `max_options` (below) |
| `offer_draw` | `yes`, `no` |

The response must contain each choice under its question name, either at the top
level or inside `answers`, `results`, `result`, `output`, `data` or
`predictions`, matching the client in the author's `decision-one-evals` project.
With a single legal move no `move` question is asked, because a one-option
question tells nothing and SystemOne templates refuse it; an answer `move` plays
that move.

- **Sensitive:** a chess position can have more than 200 legal moves, and a
  decision server may limit the options per question (Basal's chat template
  accepts 2 to 26). A model with `max_options` (see
  [model fields](configuration.md#model-fields)) gets its moves asked per piece as
  below; without it, a refused question is a transport error that stops the game.
  Moves are never silently dropped.
- **Rule:** when the N legal moves exceed `max_options` L, the one query asks the
  piece first. Its questions are `action`, `offer_draw`, `piece` and one
  `move_<key>` per piece. `piece` ("Which piece do you move? Each option lists its
  moves.") has one option per group from the game's `move_groups`, keyed by the
  group key and described by the group's move labels (`knight_g1`: `Nf3, Nh3`);
  `move_<key>` offers only that group's moves. A group with more than L moves
  becomes groups `<key>_1`, `<key>_2`, ... of near-equal size, the fewest that
  fit. A group with a single move has no move question, since one option tells
  nothing: choosing it in `piece` plays that move. The answer to the chosen
  group's move question is played and the other move questions are ignored; an
  answer outside the chosen group is a format error, and the retry asks the whole
  query again. N > L always gives at least two groups, so `piece` never has fewer
  than two options; more groups than L stop the game at once, without retries
  (see [validation](#validation-and-retries)), since `piece` cannot be asked (in
  chess that needs L well below 26). Every such turn logs a warning naming the
  model, N and the number of groups.
- The `piece` wording is written for chess, the only game so far; another game
  asked per group needs its own wording.

## Validation and retries

An answer is rejected when it does not match the format, the action is `move` with
a missing or illegal move, or the action is `accept_draw` with no pending offer.

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

## End of game

| Condition | Result | Reason recorded |
| --- | --- | --- |
| Checkmate | Win for the side that mates | `checkmate` |
| Resignation | Win for the opponent | `resignation` |
| Draw offer accepted | Draw | `draw agreed` |
| Stalemate, insufficient material, 75-move rule, fivefold repetition | Draw | `stalemate`, `insufficient material`, `75-move rule`, `fivefold repetition` |
| Threefold repetition, 50-move rule | Draw, claimed automatically by the game master | `threefold repetition`, `50-move rule` |
| Turn limit reached (both sides made `max_turns` moves) | Draw | `turn limit` |
| Third rejected answer in one turn | Win for the opponent | `illegal answers` |

- **Decision:** claimable draws are claimed automatically, so models cannot shuffle
  pieces until the turn limit. Only a repetition or 50-move count that has actually
  happened is claimed, not one the side to move could reach with its next move
  (as the `chess` library's `claim_draw=True` would), so the choice stays with the
  player. Material adjudication at the turn limit is not part
  of the MVP.
- A draw offer stays open for the opponent's next answer only; any move by the
  opponent declines it.
- Fivefold repetition and the 75-move rule stay in the table for completeness:
  from a normal start the threefold and 50-move claims always come first.

## Statistics

Statistics are kept per player, where a player is a model id plus reasoning
effort (see [configuration](configuration.md#model-fields)).

- **Games, wins, losses, draws** - counted when a game ends; aborted games are
  listed but not counted.
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
record (PGN for chess), result, reason and per-move costs; the competition file holds the settings,
prompt version and totals.
