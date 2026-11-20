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
| `name`, `prompt_template` | Identifier (the `--game` value) and the prompt template text (see [prompts](#prompts)) |
| `new_state()` | Starting position |
| `side_to_move(state)` | Which `Side` (`first` / `second`; for chess White / Black) moves |
| `legal_moves(state)` | Legal moves as `{id: label}`: a stable machine id (UCI `e2e4`) and a readable label (SAN `e4`) |
| `parse_move(state, text)` | Turns a model's move text into a legal move id, or raises with a reason |
| `apply(state, move_id)` | Next state |
| `outcome(state)` | `None` while the game runs, else winner or draw with a reason |
| `describe(state)` | Position text for the prompt (board, history, extra facts) |
| `side_names` | Display names of the two sides (for chess `White`, `Black`), used in tables and summaries |
| `render(state)` | Board for the terminal UI, as a Rich renderable |
| `context(state, ...)` | The prompt context for the side to move: position, legal moves, turn, draw offer, rejection (implemented once in the base class) |
| `prompt(context)` | The shared prompt, rendered from `prompt_template` (implemented once in the base class) |
| `record(state, outcome, players)` | The game's native record saved with each game, and `record_extension` (for chess PGN, `pgn`) |

- **Rule:** a new game adds one module under `games/`, its prompt text and one
  registry entry; the game master, players, statistics, runner and terminal UI do
  not change.
- Chess uses the `chess` library for everything above. `parse_move` accepts SAN
  (`Nf3`) and UCI (`g1f3`), case and check-mark tolerant. An answer that fits
  several legal moves is rejected with every reading, so the retry can fix it:
  under-specified SAN (`Nd2` with knights on b1 and f1) or a case-blind match
  (`BXC4`: pawn or bishop).
- States are immutable: `apply` returns a new state, so the game master can keep
  the position before a move.

## Turn loop

1. Build the turn context: game rules, side to move, position description, move
   history, legal moves, turn number and limit, a pending draw offer, and the
   reason the previous attempt was rejected (on a retry).
2. Render the shared prompt from it (see [prompts](#prompts)) and send it to the
   player through its adapter, which adds the answer format.
3. Validate the answer (see [validation](#validation-and-retries)). On rejection,
   repeat from step 1 with the reason.
4. Apply the action, record the move and its cost, publish an event.
5. Check the end of the game (see [end of game](#end-of-game)); otherwise switch
   sides.

- **Decision:** every turn is a fresh, stateless request carrying the full state.
  There is no conversation history between turns: both players get the same
  information, context stays bounded, and decision models are stateless anyway.
- **Rule:** sides alternate: in game *n* of a competition the first selected
  model plays the first side (White in chess) when *n* is odd. A competition with
  an odd number of games states that imbalance in its summary.

## Prompts

- **Rule:** both players receive the same prompt text, built from one template.
  The only differences are those the position dictates (own side, own turn) and
  the answer format, which each player adapter appends.
- **Rule:** the prompt is written to make the model play to win: it states the
  goal (win; a draw beats a loss), that the opponent is another AI model, the
  stakes of each rule below, and asks the model to check its move against the
  legal list before answering.

Prompt sections, in order:

1. **Role and goal** - which side the model plays and that it must try to win.
2. **Rules** - the game rules in brief, plus the referee's rules: turn limit and
   its result, illegal-answer limit and its penalty, how to resign, offer or accept
   a draw.
3. **Position** - move number, side to move, the board (for chess: FEN plus an
   ASCII diagram with coordinates), full move history (SAN), the opponent's last
   move, a pending draw offer, turns left.
4. **Legal moves** - every legal move label. Both players get it, so it does not
   favor either; it turns a test of rule recall into a test of play.
5. **Previous attempt** (retries only) - the rejected answer and why.

Templates live next to their game as plain text files (`games/chess_prompt.txt`)
so they can be reworded without code changes. They use `string.Template`
placeholders: `$side`, `$opponent`, `$position`, `$legal_moves`, `$turn`,
`$max_turns`, `$turns_left`, `$draw_offer` and `$previous_attempt` (empty except
on retries). The draw-offer and previous-attempt sentences are worded in
`Game.prompt` (`games/base.py`), because `string.Template` has no conditionals;
the prompt version recorded with a competition must cover them too. Wording changes are recorded in the competition result so
runs with different prompts are not compared by accident.

## Answer formats

All answers carry the same three things: an action (`move`, `resign`,
`accept_draw`), a move when the action is `move`, and an optional draw offer made
together with the move.

**LLM players** use a PydanticAI `Agent` whose `output_type` is the answer model:

```python
class TurnAnswer(BaseModel):
    action: Literal["move", "resign", "accept_draw"]
    move: str | None = None  # SAN or UCI; required when action == "move"
    offer_draw: bool = False
```

The chosen reasoning effort goes in as `OpenAIChatModelSettings(openai_reasoning_effort=...)`.
The adapter appends a short ANSWER FORMAT section naming the three fields, sets
`request_timeout` from `OLLMPIADA_REQUEST_TIMEOUT_S` and turns off the OpenAI
client's own retries, so the game master owns every retry. Without a token the
`Authorization` header is omitted per request (`extra_headers` with `openai.Omit`),
because the OpenAI client otherwise always sends one.

- **Sensitive:** PydanticAI retries invalid structured output on its own. Set its
  output retries to zero and let the game master retry, so every rejection is
  counted, timed and visible.
- **Sensitive:** local servers differ in tool-calling support. If tool-based output
  fails for a model, switch that model to PydanticAI `NativeOutput` or
  `PromptedOutput`; this may become a per-model config field.

**Decision players** call the SystemOne API: `POST {model, state, questions}`,
where `state` is the shared prompt text and every question is a `choice`:

| Question | Criteria |
| --- | --- |
| `action` | `move`, `resign`, plus `accept_draw` only while an offer is pending |
| `move` | every legal move, keyed by its id (`e2e4`), described by its label (`e4`) |
| `offer_draw` | `yes`, `no` |

The response must contain each choice under its question name, either at the top
level or inside `answers`, `results`, `result`, `output`, `data` or
`predictions`, matching the client in the author's `decision-one-evals` project.

- **Sensitive:** a chess position can have more than 200 legal moves. If a decision
  server limits options per question, the adapter must fail loudly rather than
  silently drop moves.

## Validation and retries

An answer is rejected when it does not match the format, the action is `move` with
a missing or illegal move, or the action is `accept_draw` with no pending offer.

- **Rule:** a player gets three attempts per turn. The third rejection loses the
  game with reason `illegal answers`. Each rejection counts as an illegal attempt.
- **Rule:** transport failures (timeout, connection error, HTTP 5xx) are not the
  model's fault: retry twice with backoff, then abort the game and stop the
  competition with the error shown. Aborted games do not count as wins, losses or
  draws.
- Players raise `TransportError` for every failed call, HTTP 4xx included (bad
  request, authentication, a decision server refusing too many options): a retry
  rarely fixes those, but they are not the model's answer either, so they stop the
  game loudly instead of counting as illegal attempts. A reply that arrives but
  does not match the answer format is returned as a format error and counts as a
  rejected attempt.

## End of game

| Condition | Result |
| --- | --- |
| Checkmate | Win for the side that mates |
| Resignation | Win for the opponent |
| Draw offer accepted | Draw |
| Stalemate, insufficient material, 75-move rule, fivefold repetition | Draw |
| Threefold repetition, 50-move rule | Draw, claimed automatically by the game master |
| Turn limit reached (both sides made `max_turns` moves) | Draw |
| Third rejected answer in one turn | Win for the opponent |

- **Decision:** claimable draws are claimed automatically, so models cannot shuffle
  pieces until the turn limit. Only a repetition or 50-move count that has actually
  happened is claimed, not one the side to move could reach with its next move
  (as the `chess` library's `claim_draw=True` would), so the choice stays with the
  player. Material adjudication at the turn limit is not part
  of the MVP.
- A draw offer stays open for the opponent's next answer only; any move by the
  opponent declines it.

## Statistics

Statistics are kept per player, where a player is a model id plus reasoning
effort (see [configuration](configuration.md#model-fields)).

- **Games, wins, losses, draws** - counted when a game ends; aborted games are
  listed but not counted.
- **Thinking time** - wall-clock time of every model call, rejected attempts
  included, game master work excluded. Shown as total and average per move.
- **Tokens** - input, output and reasoning tokens from PydanticAI's usage for LLM
  players; from the response for decision players when it reports any, otherwise
  shown as unknown, never as zero. Reasoning tokens are unknown on their own when
  a server reports usage without them (llama.cpp sends no
  `completion_tokens_details`).
- **Illegal attempts** - rejected answers, total and per game.

Each finished game is written to the competition folder in `runs/` with its game
record (PGN for chess), result, reason and per-move costs; the competition file holds the settings,
prompt template version and totals.
