# oLLMpiada

![A blue robot and a gold robot play chess at a marble table in an ancient Greek temple, watched by a crowd, a translucent referee and a laurel wreath](docs/images/hero.webp)

AI models and agents compete in games with fixed rules, refereed by a game master.

## Why

Building a good benchmark for frontier models is very hard. Setting a task that
still challenges them takes top-class specialists and very hard problems.
oLLMpiada starts from a different assumption: let models and agents assess each
other, under defined rules.

It continues my earlier project RivaLLMatch, where one model invented tasks for
the other. oLLMpiada introduces defined game rules instead.

Chess came first, as the easiest game to implement, then contract bridge card
play, a game of hidden information played in pairs. Other games can follow,
including multiplayer ones, and so can evaluating agents, a harness plus a model,
rather than bare models only. In the future the project may also evolve into an
engine for self-optimizing solutions: nothing stops the same model or agent from
playing against itself with a different strategy.

## What it does

One terminal command picks two models, a game and the number of games; a game
master referees every move, and the terminal shows the board (visual mode) or just
live status (background mode) with statistics: wins, losses and draws (each with
games [adjudicated](docs/game-master/results.md#statistics) at the turn limit
counted separately), thinking time, token usage and illegal attempts. The same
command runs unattended from scripts, and every competition is saved to
`.local/runs/`.

Each game is a separate module behind one interface, so games can be added
without touching the referee or the terminal UI; the
[supported games](#supported-games) are chess and contract bridge card play.

Players are LLMs behind an OpenAI chat-completions or Anthropic Messages API
(through PydanticAI) or decision models behind the SystemOne API. This is an
experiment, not a production app.

## Supported games

Each game is summarized under the same fields. The linked documents own the
rules; this section only says what is played and what is left out.

In both games a player is always a configured model, never an engine or a human
([non-goals](docs/product.md#non-goals)). A model call that still fails after the
transport retries, such as a request timeout or a provider error, aborts the
game: it is saved with no result and the competition stops
([validation and retries](docs/game-master/turns.md#validation-and-retries)).

### Chess

- **Players:** two models, White and Black. The colors swap every game: the
  first model named plays White in odd-numbered games.
- **Start:** the standard starting position, in every game.
- **Legal play:** standard chess. Each turn a model moves, resigns, offers a
  draw together with its move, or accepts a pending offer. In each side's first
  5 moves it can only move ([opening](docs/game-master/turns.md#opening)).
- **How a game ends:** checkmate; resignation; an accepted draw offer; an
  automatic draw by stalemate or insufficient material, or by threefold
  repetition or the 50-move rule, which the game master claims as soon as they
  happen; or a third rejected answer in one turn, which loses. A game still
  running when both sides have made `--max-turns` moves (default 50) is
  adjudicated by Stockfish: a side at least 5 pawns ahead, or with a forced
  mate, wins, anything else is a draw.
- **Result and points:** win, loss or draw, with no points. Games adjudicated
  at the turn limit are counted apart from games decided on the board.
- **Hidden information:** none; both models see the whole board.
- **Not supported:** variants and other starting positions; a chess clock
  (thinking time is recorded, not limited, except by the request timeout,
  `OLLMPIADA_REQUEST_TIMEOUT_S` or a provider's
  [`timeout_s`](docs/configuration.md#provider-fields): a call that keeps timing
  out aborts the game, it does not lose it); adjudication before the turn limit.
- **Record:** one PGN file per game, with the reason in its `Termination` tag
  and, for an adjudicated game, the Stockfish score as a closing comment.
- **Rules:** [end of game](docs/game-master/results.md#end-of-game),
  [turns and retries](docs/game-master/turns.md) and
  [statistics](docs/game-master/results.md#statistics).

### Bridge

- **Players:** two models, each playing both seats of a pair. At each table one
  model plays North-South (the declarer South and dummy North) and the other
  East-West (both defenders). Every deal, a *board*, is played at two tables in
  consecutive games with the pairs swapped, so each model declares and defends
  every deal once. A competition therefore plays an even number of games, and
  `run` warns when it plays fewer than 8 boards.
- **Start:** a deal derived from `--seed`, so a saved seed replays the same
  deals, and a contract a fixed rule picks from it: the pair with more
  high-card points declares, in its longest suit of at least 8 cards or else
  no trumps, at a level set by its points. The table is turned so the declarer
  sits South.
- **Legal play:** standard card play. West leads first, then dummy is faced.
  A seat follows suit when it can; the highest trump wins the trick, else the
  highest card of the suit led, and the winner leads next. Every answer is one
  card, and the game master plays a seat's only legal card itself.
- **How a game ends:** after all 13 tricks, or at a third rejected answer in
  one turn, when the opponents take every trick not yet complete. There is no
  turn limit: `run` rejects `--max-turns`.
- **Result and points:** a win for North-South when the contract is made
  (`made`), else for East-West (`down`); never a draw. Points are the tricks
  each pair took, and `run` and `show` print a board table that compares the
  two tables of each board.
- **Hidden information:** each turn shows only what the seat to play may
  know: its own hand (the declarer's when dummy plays), dummy after the opening
  lead, the contract and the cards played. A defender never sees its partner's
  hand, even though one model plays both defending seats.
- **Not supported:** bidding; doubles and redoubles; vulnerability; scoring by
  contract points, IMPs or matchpoints (tables are compared by tricks);
  claims, resigning and draw offers.
- **Record:** one PBN file per table; both tables of a board carry the same
  board number and deal.
- **Rules:** [bridge](docs/game-master/bridge.md).

## Quick start

```bash
uv sync
brew install stockfish          # or apt; adjudicates chess games at the turn limit
cp .env.example .env            # fill tokens for providers that need them
$EDITOR config/models.json      # providers and models available for competitions
uv run ollmpiada models         # configured models and whether they are available
uv run ollmpiada run qwen3-8b@low claude-haiku@none --games 4
uv run ollmpiada run qwen3-8b@low claude-haiku@none --game bridge --games 4
```

## Configuring models

Models live in `config/models.json` (gitignored, since providers differ per
machine; set `OLLMPIADA_MODELS_CONFIG` to use another path). The file has two
lists:

- **`providers`** - where to send requests: a `name`, a `url` (the API root,
  or the full endpoint for `systemone`), the `api` it speaks
  (`openai-completions`, `anthropic-messages` or `systemone` for decision models)
  and optionally `token_env`, the *name* of the environment variable in `.env`
  that holds the token.
- **`models`** - what can play: an `id` used on the command line and in
  statistics, its `provider`, the `model` name sent to it, and the
  `reasoning_efforts` you may pick as `<id>@<effort>`.

The example below reaches local llama.cpp models through a
[Bifrost](https://github.com/maximhq/bifrost) gateway, Claude through Anthropic's
hosted API, and a local decision-model server. Bifrost holds any upstream keys
itself, so its provider sets no `token_env`, and its model names carry Bifrost's
provider prefix (`llama.cpp/`):

```json
{
  "providers": [
    {"name": "bifrost", "url": "http://localhost:8080/v1",
     "api": "openai-completions", "timeout_s": 300,
     "headers": {"x-bf-passthrough-extra-params": "true"}},
    {"name": "anthropic", "url": "https://api.anthropic.com",
     "api": "anthropic-messages", "token_env": "ANTHROPIC_API_KEY"},
    {"name": "decision-server", "url": "http://localhost:8000/v1/systemone",
     "api": "systemone"}
  ],
  "models": [
    {"id": "qwen3-8b", "provider": "bifrost", "model": "llama.cpp/Qwen3-8B-GGUF",
     "reasoning_efforts": ["none", "low", "medium", "high"],
     "max_tokens": 8192, "reasoning_budget": 1024},
    {"id": "instruct-7b", "provider": "bifrost",
     "model": "llama.cpp/Example-Instruct-7B-GGUF",
     "max_tokens": 1024, "output_mode": "prompted"},
    {"id": "claude-haiku", "provider": "anthropic", "model": "claude-haiku-5-5",
     "reasoning_efforts": ["none", "low", "medium", "high"]},
    {"id": "decision-model", "provider": "decision-server",
     "model": "example/decision-model-GGUF", "max_options": 20}
  ]
}
```

The optional settings and why they are there:

- **`timeout_s: 300`** on the Bifrost provider. Local models can take longer
  per move than the default 120-second request timeout.
- **`headers` with `x-bf-passthrough-extra-params: true`** on the Bifrost
  provider. Bifrost's OpenAI-compatible `/v1` route drops request fields it does
  not know, such as llama.cpp's `reasoning_budget_tokens`, unless each request
  carries this header; without it `reasoning_budget` is silently ignored.
- **`reasoning_budget: 1024`** on `qwen3-8b`, a small local thinking model.
  `max_tokens` counts thinking too, so such a model can think until the cap and
  never write its move. The budget makes llama.cpp close the thinking block
  early, leaving room for the answer under `max_tokens`. At effort `none` the
  model does not think, so the budget has no effect.
- **`output_mode: "prompted"`** on `instruct-7b`, standing for a model whose
  structured-output grammar llama.cpp fails to build. The default, `tool`, makes
  the model answer by calling an output tool; `native` uses the API's
  JSON-schema output instead. Both make llama.cpp build a grammar. `prompted`
  sends no tool and no `response_format`: the JSON schema goes into a system
  message and the reply text is parsed.
- **`max_options: 20`** on the decision model, the most options its server
  accepts in one question; longer move lists are asked piece by piece.

Run `uv run ollmpiada models` to see what loaded and what is available. Every
field, its validation and the reasoning behind it are in
[configuration](docs/configuration.md#model-config):
[provider fields](docs/configuration.md#provider-fields) and
[model fields](docs/configuration.md#model-fields).

## Status

Chess is complete: `models`, `probe`, `run` and `show` work with real models.
Bridge card play runs end to end with scripted players; the first runs with real
models are next.

## Documentation

- [docs/README.md](docs/README.md) - documentation index.
- [docs/product.md](docs/product.md) - what the app does and does not do.
- [docs/game-master/README.md](docs/game-master/README.md) - refereeing, prompts and statistics.
- [docs/configuration.md](docs/configuration.md) - `config/models.json` and `.env`.

## Name

The Greek *Ολυμπιάδα* (Olympiad) with an LLM inside: written oLLMpiada, lowercase
`ollmpiada` for folders, the package and the command.
