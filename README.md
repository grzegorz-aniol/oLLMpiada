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

Chess comes first, as the easiest game to implement. Other games can follow,
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

Each game is a separate module behind one interface, so games other than chess
can be added without touching the referee or the terminal UI.

Players are LLMs behind an OpenAI chat-completions or Anthropic Messages API
(through PydanticAI) or decision models behind the SystemOne API. This is an
experiment, not a production app.

## Quick start

```bash
uv sync
brew install stockfish          # or apt; adjudicates chess games at the turn limit
cp .env.example .env            # fill tokens for providers that need them
$EDITOR config/models.json      # providers and models available for competitions
uv run ollmpiada models         # configured models and whether they are available
uv run ollmpiada run qwen3-8b@low claude-haiku@none --games 4
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
Next: bridge card play.

## Documentation

- [docs/README.md](docs/README.md) - documentation index.
- [docs/product.md](docs/product.md) - what the app does and does not do.
- [docs/game-master/README.md](docs/game-master/README.md) - refereeing, prompts and statistics.
- [docs/configuration.md](docs/configuration.md) - `config/models.json` and `.env`.

## Name

The Greek *Ολυμπιάδα* (Olympiad) with an LLM inside: written oLLMpiada, lowercase
`ollmpiada` for folders, the package and the command.
