---
related: ["README.md", "turns.md", "../configuration.md"]
---

# Prompts and answer formats

[Game master](README.md)

What both players are told each turn, and how each player adapter asks for the answer.

## Prompts

- **Decision:** no model-facing text is shared between games: chess resigns,
  offers draws and shows the whole board, bridge plays one card from a hidden
  hand, and sentences worded for both fit neither. Every sentence a model or
  decision model reads belongs to its game: the templates, the retry sentence,
  the LLM answer format, the decision questions and the game master's rejections
  of a broken referee rule. Shared code keeps only the control interface (`Game`,
  `GameMaster`, the turn loop) and each player's transport (the output tool, the
  JSON shape, parsing the answer into an action and a move). It words only what
  describes the answer and not the game: the LLM answer schema's descriptions
  (see [answer formats](#answer-formats)), the quote of a rejected answer (`you
  answered '...'; <reason>`) and a format error (`the answer does not match the
  answer format: ...`).
- **Rule:** both players receive the same templates, rendered the same way. The
  only differences are those the position dictates (own side, own turn) and the
  answer format, which depends on the player adapter: the LLM answer format or the
  decision questions, both worded by the game. In a game with hidden
  information the position is the mover's view (next rule), so the two players
  see different cards, never different wording.
- **Rule:** in a hidden-information game the position text (`describe`) holds
  only what the seat to move may know. For bridge that is its own hand, dummy
  once the opening lead is made, the contract and every card played; the
  declarer playing from dummy sees their own hand and dummy, as at a real table.
  Turns are stateless (see [turn loop](turns.md#turn-loop)), so a model that plays
  both seats of a pair never sees its partner's hidden hand.
- **Rule:** the prompt is written to make the model play to win: it states the
  goal (for chess: win, a draw beats a loss; for bridge: make or beat the
  contract and take every trick it can), that the opponent is another AI model,
  the stakes of each rule below, and asks the model to check its move against the
  legal list before answering.
- **Decision:** the prompt has two parts. The **system** part holds what is true
  for the whole game: role and goal, game rules and referee rules. The **user**
  part holds only the turn at hand: the current state and what to answer now. The
  standing rules stay apart from the turn, and the LLM adapter puts its answer
  format between them. The names follow chat roles, but no player sends the
  system part as a system message (see [answer formats](#answer-formats)).

System part, in order:

1. **Role and goal** - which side the model plays and that it must try to win.
2. **Rules** - the game rules in brief, plus the referee's rules: the
   illegal-answer limit and its penalty and, for chess, the turn limit and its
   result, automatic draws, the [opening](turns.md#opening) without resigning or
   draw offers, how to resign, offer or accept a draw.

User part, in order:

1. **Position** - for chess: move number, side to move, the board (FEN plus an
   ASCII diagram with coordinates), the recent moves (SAN with move numbers), the
   opponent's last move, the halfmove clock, a warning when the current position
   has occurred before, this turn against the limit and turns left, a pending
   draw offer.
2. **Legal moves** - every legal move label, the instruction to pick one (for
   chess in the opening: that resigning and draw offers start with move 6) and
   to check the answer against the list. Both players get it, so it does not
   favor either; it turns a test of rule recall into a test of play.
3. **Previous attempt** (retries only) - the rejected answer and why.

For bridge the system part covers the role (one model plays both seats of its
pair; North-South declares from South and plays dummy, East-West defends), the
card-play rules (follow suit, trumps, the trick winner leads, dummy faced after
the opening lead), the goal and the illegal-answer rule with its penalty (the
opponents take every trick left). The user part holds the position and the legal
cards, under a LEGAL CARDS heading, which bridge's answer format names. The position (`BridgeGame.describe`) gives the
contract, declarer and tricks each pair needs, the tricks won by each pair, the
mover's seat (with "playing from dummy (North)" on dummy's turn), its own hand by
suit, dummy or a note that it is faced after the opening lead, the completed
tricks (cards by seat from the lead, and the winner) and the trick in play.

- **Decision:** the bridge prompt tells the models that the same deal is played
  at a second table with the pairs swapped and that they are compared by tricks.
  Without it a declarer could stop trying once the contract is made, and a
  defender once it is beaten, while the duplicate result counts every trick (see
  [statistics](results.md#statistics)). For the promise to hold, a bridge
  competition plays an even number of games (see [sides alternate](turns.md#turn-loop)).
- **Decision:** chess shows only the last `RECENT_PLIES` (2) plies, the
  opponent's last move and the player's own previous one, not the full move
  history: FEN and board carry the position, and the halfmove clock
  (`Halfmove clock: N (automatic draw at 100)`) carries the 50-move rule. The
  repetition line is only a hint that positions are repeating: it reports that
  the current position has occurred twice, so a move into another position seen
  twice before can still end the game by threefold repetition without a warning.
  The system part says these lines exist.

Templates live next to their game as plain text files
(`games/chess_system_prompt.txt`, `games/chess_user_prompt.txt`,
`games/bridge_system_prompt.txt`, `games/bridge_user_prompt.txt`) so they can be
reworded without code changes. They use `string.Template` placeholders.
`Game.prompt` (`games/base.py`) only fills them, with two kinds of values. The
values every game has carry no wording: `$side`, `$opponent`, `$position`,
`$legal_moves`, `$turn`, `$max_turns` and `$turns_left`. Every sentence that
varies by turn comes from the game's own `Game.prompt_values`, since
`string.Template` has no conditionals: chess adds `$opening_moves`,
`$draw_offer`, `$choose_move` (the instruction to pick a move, which in the
opening names the move that ends it) and `$previous_attempt`; bridge adds only
`$previous_attempt`. `$previous_attempt` is empty except on retries, where it
quotes the rejection and asks again (chess `Answer again with a legal move.`,
bridge `Answer again with a legal card.`). A template uses only the placeholders
its game needs. A game without a [turn limit](results.md#end-of-game) has no
`$max_turns` or `$turns_left` (both render as `None`), so its templates leave
them out.

The `Prompt` a game renders also carries its LLM answer format
(`Game.answer_format`) and its decision questions (`Game.questions`), see
[answer formats](#answer-formats). The game master's rejections of a broken
referee rule come from `Game.rejection`: for a resignation or draw offer in a
move-only turn chess says `resigning and draw offers start with your move 6; play
a move`, bridge `bridge has no resigning or draw offers; play a card`; for the
action `move` without a move chess says `the action is move but no move was
given`, bridge `the answer names no card; play a card`.

- **Rule:** the prompt version recorded with a competition covers everything an
  LLM reads: both parts, the game's templates and `prompt_values`, its LLM answer
  format, the layout of the LLM message and the answer schema with its
  descriptions, so runs with different prompts are not compared by accident. It
  does not cover the decision questions or the wording of rejections.
  `prompt_version` in `competition.py` hashes, for the two sample turns of
  `sample_requests`, an opening turn and a later one in the start of `SAMPLE_SEED`
  (for bridge the opening lead), the LLM message (`llm_message`) and the JSON
  schema of the answer model that turn asks for (`answer_type`), so it covers both
  answer formats and both schemas; the answer format sits at the system/user boundary, so moving text
  between the parts changes the version too.
- **Rule:** a change to model-facing text, the decision questions included, is a
  deliberate decision, never a side effect of a refactor: the
  [golden test](../testing.md#golden-prompts) pins the sample turns as each player
  sends them and fails on any change. Regenerate its files in the same change and
  name the new prompt version in the change's description.

## Answer formats

All answers carry the same three things: an action (`move`, `resign`,
`accept_draw`), a move when the action is `move`, and an optional draw offer made
together with the move. In a move-only turn (`TurnRequest.move_only`: the chess
[opening](turns.md#opening), every bridge turn) every adapter asks for the move
alone: the action and the draw offer are not part of the answer format at all.

**LLM players** (`openai-completions` and `anthropic-messages` providers) use a
PydanticAI `Agent` whose `output_type` is the answer model:

```python
class TurnAnswer(BaseModel):
    action: Literal["move", "resign", "accept_draw"]
    move: str | None = None  # an entry of the legal list; required when action == "move"
    offer_draw: bool = False
```

In a move-only turn the run's output type is `MoveAnswer` (one field, `move`)
instead, in the same output mode, and the adapter turns it into a `TurnAnswer`
with action `move`; `answer_type` in `players/llm.py` picks the model.

- **Rule:** the answer models are written for the model, not for developers.
  PydanticAI sends their JSON schema in every output mode: the class docstring
  becomes the output tool's description (`tool`), the schema's description
  (`native`) or the schema's `description` in the instructions (`prompted`), and
  every `Field(description=...)` stays on its property. So the docstrings and
  field descriptions name no code (developer notes go in comments), the schema
  title is `answer` instead of the class name, and every description is
  game-neutral: it points at "the legal list in the message", never at a heading
  or a game's words, since the game's ANSWER FORMAT does that. The field
  descriptions say what each field holds, when it is set or null, the exact
  allowed values (`move`, `resign`, `accept_draw`), that `move` is one entry
  copied unchanged from the legal list, that `offer_draw` is true only together
  with a move, and that nothing else (explanation, Markdown) goes in.
- **Sensitive:** the `move` description must not forbid quotes. In `prompted`
  mode Bielik 11B v3.1 read "no quotes" literally and answered a bare move
  (`Nh6`) instead of `{"move": "Nh6"}` in every one-field answer.
- **Sensitive:** every instruction belongs in a field description. Anthropic's
  native output (`output_config.format`) sends only the JSON schema and drops the
  class description, so that line is a one-line summary the model can miss.

The adapter sends one user message and no PydanticAI `instructions`:
`llm_message` in `players/llm.py` joins the system part, the game's short ANSWER
FORMAT section (`Prompt.answer_format`) and the user part, in that order. Chess
names the three fields, and only `move` in a move-only turn; bridge names only
`move`, the card to play.

- **Rule:** a game's answer format and its user template are one unit: the answer
  format tells the model to answer exactly as listed under the heading of the
  legal moves in the same game's template (chess LEGAL MOVES, bridge LEGAL CARDS),
  so no other file depends on that heading.

The text that is the same from turn to turn comes first, so a local server can
reuse its cached prompt prefix. In chess the answer format, and in every output
mode the output schema, changes once, at a side's move 6 after the opening, so the
cached prefix restarts once per game; in bridge it never changes.

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
where `state` is the prompt text, system part then user part, and every
question is a `choice`. The game words each question's instruction and each fixed
option's description (`Game.questions`, a `Questions` in `games/base.py`; chess
asks `Which move do you play?`, bridge `Which card do you play?`); the player
supplies only the question names and the options:

| Question | Criteria |
| --- | --- |
| `action` | `move`, `resign`, plus `accept_draw` only while an offer is pending; not asked in a move-only turn |
| `move` | every legal move, keyed by its id (`e2e4`), described by its label (`e4`); replaced by `piece` and `move_<key>` past `max_options` (below) |
| `offer_draw` | `yes`, `no`; not asked in a move-only turn |

The response must contain each choice under its question name, either at the top
level or inside `answers`, `results`, `result`, `output`, `data` or
`predictions`, matching the client in the author's `decision-one-evals` project.
With a single legal move no `move` question is asked, because a one-option
question tells nothing and SystemOne templates refuse it; an answer `move` plays
that move. A move-only turn never reaches the player with a single legal move: the
game master plays that move itself (see [opening](turns.md#opening)).

- **Sensitive:** a chess position can have more than 200 legal moves, and a
  decision server may limit the options per question (Basal's chat template
  accepts 2 to 26). A model with `max_options` (see
  [model fields](../configuration.md#model-fields)) gets its moves asked per piece as
  below; without it, a refused question is a transport error that stops the game.
  Moves are never silently dropped.
- **Rule:** when the N legal moves exceed `max_options` L, the one query asks the
  piece first. Its questions are `action`, `offer_draw` (both left out in a
  move-only turn), `piece` and one `move_<key>` per piece. `piece` (chess: "Which piece do you move? Each option lists its
  moves.", bridge: "Which group of cards do you play from? Each option lists its
  cards.") has one option per group from the game's `move_groups`, keyed by the
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
