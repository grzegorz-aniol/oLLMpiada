---
related: ["README.md", "turns.md", "../configuration.md"]
---

# Prompts and answer formats

[Game master](README.md)

What both players are told each turn, and the answer format each player adapter adds.

## Prompts

- **Rule:** both players receive the same prompt text, built from one pair of
  templates. The only differences are those the position dictates (own side, own
  turn) and the answer format, which each player adapter adds.
- **Rule:** the prompt is written to make the model play to win: it states the
  goal (win; a draw beats a loss), that the opponent is another AI model, the
  stakes of each rule below, and asks the model to check its move against the
  legal list before answering.
- **Decision:** the prompt has two parts. The **system** part holds what is true
  for the whole game: role and goal, game rules and referee rules. The **user**
  part holds only the turn at hand: the current state and what to answer now. The
  standing rules stay apart from the turn, and the LLM adapter puts its answer
  format between them. The names follow chat roles, but no player sends the
  system part as a system message (see [answer formats](#answer-formats)).

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
  wording in `Game.prompt`, the LLM answer format and the layout of the LLM
  message, so runs with different prompts are not compared by accident.
  `prompt_version` in `competition.py` hashes the LLM messages (`llm_message`)
  rendered from sample prompts; the answer format sits at the system/user
  boundary, so moving text between the parts changes the version too.

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

The adapter sends one user message and no PydanticAI `instructions`:
`llm_message` in `players/llm.py` joins the system part, a short ANSWER FORMAT
section naming the three fields, and the user part, in that order. The text that
is the same every turn comes first, so a local server can reuse its cached
prompt prefix.

- **Decision:** ollmpiada sends no system message to LLM players, on every API
  and in every output mode, so a request carries at most one: none in `tool` and
  `native` mode, PydanticAI's JSON schema in `prompted` mode. Some chat templates
  (Bielik 3.1 on llama.cpp) refuse more than one system message. In PydanticAI
  2.71, on OpenAI chat completions every instruction part becomes its own
  `system` message and `prompted` output adds the schema as one more part, so
  ollmpiada's own instructions would make two. On Anthropic messages the parts
  are text blocks of the single `system` field. PydanticAI's profile flag
  `openai_chat_supports_multiple_system_messages=False` would merge them
  instead, but only on OpenAI and only where configured.

The chosen reasoning effort is mapped per API, as
described in [model fields](../configuration.md#model-fields), and so are the
optional `max_tokens` cap and `reasoning_budget`. The adapter sets both
`request_timeout` (the deadline of the whole call) and `timeout` (the SDK
client's per-attempt limit, 600 s by
default, which `request_timeout` alone cannot raise) from the provider's
`timeout_s` or else `OLLMPIADA_REQUEST_TIMEOUT_S` (see
[provider fields](../configuration.md#provider-fields)), and turns off the provider
client's own retries, so the game master owns every retry. The provider's
`headers` go out as `extra_headers`. Without a token the `Authorization` header is
omitted per request (`extra_headers` with the SDK's `Omit`), because both the
OpenAI and the Anthropic clients otherwise always send the key as a bearer token.
Reasoning tokens are known when the model reports them (OpenAI
`completion_tokens_details`, Anthropic thinking tokens) and unknown otherwise.

- **Sensitive:** PydanticAI retries invalid structured output on its own. Set its
  output retries to zero and let the game master retry, so every rejection is
  counted, timed and visible.
- **Sensitive:** local servers differ in structured-output support. A model's
  [`output_mode`](../configuration.md#model-fields) picks the agent's output type:
  `TurnAnswer` itself for `tool` (an output tool, the default),
  `NativeOutput(TurnAnswer)` for `native` (the API's JSON-schema output) or
  `PromptedOutput(TurnAnswer)` for `prompted` (PydanticAI sends the JSON schema as
  its instructions, the request's only system message, and parses the reply text,
  plain JSON or in a code fence). The ANSWER FORMAT section is sent and output
  retries stay at zero in every mode. The prompt version does not cover the
  output mode, which is model config like `max_tokens`.

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
  [model fields](../configuration.md#model-fields)) gets its moves asked per piece as
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
  (see [validation](turns.md#validation-and-retries)), since `piece` cannot be asked (in
  chess that needs L well below 26). Every such turn logs a warning naming the
  model, N and the number of groups.
- The `piece` wording is written for chess, the only game so far; another game
  asked per group needs its own wording.
