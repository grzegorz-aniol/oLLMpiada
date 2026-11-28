---
related: ["README.md", "architecture.md", "game-master/README.md", "development.md"]
---

# Configuration

[Documentation index](README.md)

Owner of: the model config file, provider tokens and the environment variables
the app reads.

## Model config

`config/models.json` lists the providers and the models available for
competitions. The app looks for it relative to the working directory; the path can
be changed with `OLLMPIADA_MODELS_CONFIG`.

- **Decision:** the file is local and gitignored, because providers and models
  differ per machine. It holds no secrets, only the names of environment variables
  that carry tokens. Start from this example; it uses every field in the tables
  below:

```json
{
  "providers": [
    {"name": "bifrost", "url": "http://localhost:8080/v1",
     "api": "openai-completions", "timeout_s": 300,
     "headers": {"x-bf-passthrough-extra-params": "true"},
     "models_url": "http://localhost:8081/v1/models", "cleanup": "just llama-unload"},
    {"name": "anthropic", "url": "https://api.anthropic.com",
     "api": "anthropic-messages", "token_env": "ANTHROPIC_API_KEY"},
    {"name": "decision-server", "url": "http://localhost:8000/v1/systemone",
     "api": "systemone", "start": "just basal-serve",
     "models_url": "http://localhost:8000/v1/models"}
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

`qwen3-8b` goes through Bifrost, so its provider sends the passthrough header
that lets `reasoning_budget` reach llama.cpp; `instruct-7b` uses `prompted`
output, the fallback for a model whose grammar llama.cpp cannot build (both in
[model fields](#model-fields)). For experiments, Bifrost's llama.cpp router behind
it is checked at its own `models_url` and freed with `cleanup`, while the decision
server is started on demand with `start`. The [README](../README.md#configuring-models)
walks through the same example and why each option is there.

### Provider fields

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique name that models refer to |
| `url` | yes | Base URL; what it points at depends on `api` (below) |
| `api` | yes | The API the provider speaks, which picks the player adapter (below) |
| `token_env` | no | Name of the environment variable holding the bearer token |
| `timeout_s` | no | Timeout of one call to this provider's models in seconds, above 0; missing means `OLLMPIADA_REQUEST_TIMEOUT_S` |
| `headers` | no | HTTP headers sent on every call to this provider's models, as a map of name to value; missing means none |
| `start` | no | Shell command that starts the provider's server, which [`experiment run`](experiments.md#servers) then owns: it starts and stops it. Requires `models_url` |
| `models_url` | no | GET URL listing the served models, which `experiment run` polls until a competition's models are listed; written out, never derived from `url` |
| `cleanup` | no | Shell command that frees the GPU memory of a server that keeps running; `experiment run` runs it before every competition, also one that uses the provider, and at the end |

| `api` | Player | `url` |
| --- | --- | --- |
| `openai-completions` | LLM, PydanticAI `OpenAIChatModel` (chat completions) | API root ending in `/v1`; requests go to `<url>/chat/completions` |
| `anthropic-messages` | LLM, PydanticAI `AnthropicModel` (Messages API) | API root without `/v1`; requests go to `<url>/v1/messages` |
| `systemone` | Decision model (SystemOne API, see [answer formats](game-master/prompts.md#answer-formats)) | The full endpoint URL |

- **Decision:** the provider's `api` picks the player adapter; models carry no
  type of their own. The names follow Pi's provider `api` values; a further API
  (for example OpenAI Responses or Gemini) is one more value and one more branch
  in `players/llm.py`.

- **Rule:** the config never contains a token value, only `token_env`. Without
  `token_env`, or when the variable is empty, requests carry no `Authorization`
  header; this is the normal case behind a proxy that handles security itself.
- **Rule:** a `token_env` that names a variable missing from the environment is a
  startup warning, not an error: `ollmpiada models` shows the model as
  unavailable and `ollmpiada run` refuses it.
- **Rule:** a provider's `timeout_s` replaces `OLLMPIADA_REQUEST_TIMEOUT_S` for
  every call to its models, whatever the `api`; it exists because local inference
  can be much slower than the hosted default allows for.
- **Rule:** a provider's `headers` go out on every call to its models, whatever
  the `api`, and hold only non-secret values; tokens still come only from
  `token_env`, so the loader rejects an `Authorization` header (any case). They
  exist for proxy switches such as Bifrost's passthrough header (see
  [`reasoning_budget`](#model-fields)).

### Model fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique name used on the command line and in statistics |
| `provider` | yes | A provider `name` |
| `model` | yes | Model name sent to the provider |
| `reasoning_efforts` | no | Reasoning efforts the model accepts; empty or missing means reasoning cannot be set |
| `max_tokens` | no | LLM models only: most output tokens per call, reasoning included, at least 1; sent as the `max_tokens` request field for both LLM APIs. Missing means ollmpiada sets no cap (for `anthropic-messages` PydanticAI then sends the model's maximum output) |
| `reasoning_budget` | no | `openai-completions` models only: most reasoning tokens per call, at least 1 and below `max_tokens` when both are set; sent as the `reasoning_budget_tokens` request field. Missing means nothing is sent |
| `output_mode` | no | LLM models only: how the answer is asked for. `tool` offers an output tool the model must call; `native` asks for the API's JSON-schema output (`response_format` for `openai-completions`, `output_config.format` for `anthropic-messages`); `prompted` puts the JSON schema into a system message and parses the reply text, with no tool and no `response_format` sent. Missing means `tool` |
| `max_options` | no | `systemone` models only: most options the server accepts in one question, at least 2; longer move lists are [asked per piece](game-master/prompts.md#answer-formats) in the same query. Missing means no limit |

- **Decision:** reasoning effort is chosen per player when a competition starts,
  from the model's `reasoning_efforts`, and written on the command line as
  `<id>@<effort>`. A bare `<id>` sends no effort, so the provider's default
  applies; an effort is rejected for a model that lists none. Statistics are kept
  per model *and* effort, so `qwen3-8b@high` and `qwen3-8b@low` are
  separate players.
- Effort values follow the OpenAI `reasoning_effort` names: `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, `max`. A model lists only the ones its server
  accepts. For `openai-completions` the effort is sent as is
  (`openai_reasoning_effort`); for `anthropic-messages` it goes through
  PydanticAI's unified `thinking` setting: `none` turns thinking off, other levels
  are mapped per Claude model (adaptive thinking with that effort on current
  models, a token budget on older ones). `systemone` models take no effort, and
  the loader rejects one that lists any, `max_tokens` or `output_mode`; likewise
  it rejects `max_options` on a model of any other `api`, and `reasoning_budget`
  on a model of any `api` but `openai-completions`.
- **Sensitive:** `max_tokens` counts reasoning tokens too, so a low cap on a
  reasoning model can cut the answer off before it is written.
- **Decision:** for `openai-completions` the cap is sent as the `max_tokens`
  field (the OpenAI model profile turns off PydanticAI's default
  `max_completion_tokens`), because llama.cpp, vLLM, LM Studio, Ollama and
  OpenRouter read `max_tokens`, while some of them (OpenRouter, Ollama) silently
  ignore the newer `max_completion_tokens`. Trade-off: OpenAI's own API rejects
  `max_tokens` for o-series reasoning models, so point such a model at another
  provider or leave its `max_tokens` unset. `anthropic-messages` sends
  `max_tokens` natively.
- **Decision:** `reasoning_budget` caps only the reasoning part, so a reasoning
  model under a `max_tokens` cap keeps room for its answer. It is sent as
  `reasoning_budget_tokens`, llama.cpp's per-request field, which overrides the
  server's `--reasoning-budget`; once the budget is spent the server closes the
  thinking block and the answer follows. The effort is still sent alongside, and
  `none` makes the budget moot. Reasoning counts towards `max_tokens`, hence the
  loader requires the budget to be lower. The loader rejects the field for every
  `anthropic-messages` model: current Claude models refuse a thinking token
  budget (PydanticAI's `anthropic_disallows_budget_thinking` profile flag), and
  on older ones the effort already maps to a budget.
- **Sensitive:** `reasoning_budget_tokens` is not part of the OpenAI API. Servers
  that name their budget differently (OpenRouter takes `reasoning.max_tokens`)
  or have none ignore it, and llama.cpp applies it only to models whose chat
  template marks the thinking block. A proxy must forward unknown request fields.
  Bifrost (v2.5.1, checked live) drops them on its OpenAI integration route
  (`/openai/v1`), where the passthrough header only unpacks a nested
  `extra_params` object; its standard route (`/v1`) forwards top-level fields
  only with the header `x-bf-passthrough-extra-params: true` on each request, and
  it has no per-provider switch for that. So point the provider at Bifrost's
  `/v1`, set the header in the provider's `headers`, and use Bifrost's
  provider-prefixed model name (`llama.cpp/<model>`). Bifrost does not turn
  `reasoning.max_tokens` into a budget for custom providers.
- **Sensitive:** for `anthropic-messages` PydanticAI looks the model up by name to
  know how to send thinking, so a proxy route prefix is stripped for that lookup
  (`anthropic/claude-haiku-5-5` is treated as `claude-haiku-5-5`). With thinking
  on, Claude cannot be forced to call the output tool, so in `tool` mode
  PydanticAI asks for JSON-schema output instead; a bare id with no effort thinks
  by default.
- **Decision:** `output_mode` is a per-model fallback for servers whose structured
  output fails, and `tool` stays the default for every model. `prompted` is the
  fallback for local servers, because it asks the server to enforce nothing. For
  `openai-completions` it also turns off the OpenAI model profile's
  `supports_json_object_output`: PydanticAI's prompted output otherwise sends
  `response_format: {"type": "json_object"}`, which hits the same server failure
  as the other modes. The Anthropic Messages API has no such JSON mode, so
  `anthropic-messages` needs no change. Whether `prompted` should be the default
  for local models is open: it changes how each model is asked, which may matter
  for fairness between competitors.
- **Sensitive:** llama.cpp `llama-server` (build 12359, checked live 2026-11-21)
  cannot initialise any grammar it generates itself for some GGUF models
  (Bielik 4.5B v3.0 Instruct): `tool_choice: required`, `response_format`
  `json_object` and `json_schema` all fail with HTTP 400 `Failed to initialize
  samplers`, which ollmpiada counts as a transport error. With the model's own
  chat template the tool request is accepted but left unconstrained, so in `tool`
  mode the model answers in free text and every turn is a format error. Only
  `prompted` gave legal answers there (32 of 32 calls, against none for the other
  modes).

The loader validates the file with Pydantic at startup: unique ids, existing
provider names, known `api`, effort and `output_mode` values, `max_options` of at
least 2, `max_tokens` of at least 1, a `reasoning_budget` of at least 1 and below
`max_tokens`, a positive `timeout_s`, `headers` with non-empty names, string
values and no `Authorization`, non-empty `start` and `cleanup`, and an
`http://` or `https://` `models_url`, required wherever `start` is set. An invalid
file stops the app with a message naming the bad entry.

## Environment

`.env` in the project root is loaded at startup with python-dotenv; variables
already set in the shell take precedence. `.env` is gitignored and agents must
not read it. `.env.example` lists the variable names with empty values.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLMPIADA_MODELS_CONFIG` | `config/models.json` | Path to the model config |
| `OLLMPIADA_RUNS_DIR` | `.local/runs` | Where competition results are written |
| `OLLMPIADA_EXPERIMENTS_DIR` | `.local/experiments` | Where [experiments](experiments.md#experiment-folder) live; an experiment's competitions are written to its own `runs/` instead of `OLLMPIADA_RUNS_DIR` |
| `OLLMPIADA_SERVICES_DIR` | `.local/services` | PID files and output of the servers [`experiment run`](experiments.md#servers) starts |
| `OLLMPIADA_REQUEST_TIMEOUT_S` | `120` | Timeout of one model call in seconds; a provider's `timeout_s` overrides it ([provider fields](#provider-fields)) |
| `OLLMPIADA_STOCKFISH` | `stockfish` (looked up on `PATH`) | Path to the Stockfish binary that adjudicates chess games at the turn limit; required for chess ([end of game](game-master/results.md#end-of-game)) |
| *names from `token_env`* | none | Provider tokens |
