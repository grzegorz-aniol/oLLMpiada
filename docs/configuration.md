---
related: ["README.md", "architecture.md", "game-master.md", "development.md"]
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
  that carry tokens. Start from this example:

```json
{
  "providers": [
    {"name": "local-llama", "url": "http://127.0.0.1:1234/v1",
     "api": "openai-completions", "timeout_s": 600},
    {"name": "openrouter", "url": "https://openrouter.ai/api/v1",
     "api": "openai-completions", "token_env": "OPENROUTER_API_KEY"},
    {"name": "anthropic", "url": "https://api.anthropic.com",
     "api": "anthropic-messages", "token_env": "ANTHROPIC_API_KEY"},
    {"name": "basal", "url": "http://127.0.0.1:8000/v1/systemone", "api": "systemone"}
  ],
  "models": [
    {"id": "qwen3-30b-local", "provider": "local-llama", "model": "qwen3-30b-a3b",
     "reasoning_efforts": ["none", "low", "medium", "high"],
     "max_tokens": 8192, "reasoning_budget": 4096},
    {"id": "gpt-oss-120b", "provider": "openrouter", "model": "openai/gpt-oss-120b",
     "reasoning_efforts": ["low", "medium", "high"], "max_tokens": 16384},
    {"id": "haiku-5-5", "provider": "anthropic", "model": "claude-haiku-5-5",
     "reasoning_efforts": ["none", "low", "medium", "high"]},
    {"id": "basal-mini", "provider": "basal", "model": "Remek/basal-1.5-mini-GGUF",
     "max_options": 26}
  ]
}
```

### Provider fields

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique name that models refer to |
| `url` | yes | Base URL; what it points at depends on `api` (below) |
| `api` | yes | The API the provider speaks, which picks the player adapter (below) |
| `token_env` | no | Name of the environment variable holding the bearer token |
| `timeout_s` | no | Timeout of one call to this provider's models in seconds, above 0; missing means `OLLMPIADA_REQUEST_TIMEOUT_S` |

| `api` | Player | `url` |
| --- | --- | --- |
| `openai-completions` | LLM, PydanticAI `OpenAIChatModel` (chat completions) | API root ending in `/v1`; requests go to `<url>/chat/completions` |
| `anthropic-messages` | LLM, PydanticAI `AnthropicModel` (Messages API) | API root without `/v1`; requests go to `<url>/v1/messages` |
| `systemone` | Decision model (SystemOne API, see [answer formats](game-master.md#answer-formats)) | The full endpoint URL |

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

### Model fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique name used on the command line and in statistics |
| `provider` | yes | A provider `name` |
| `model` | yes | Model name sent to the provider |
| `reasoning_efforts` | no | Reasoning efforts the model accepts; empty or missing means reasoning cannot be set |
| `max_tokens` | no | LLM models only: most output tokens per call, reasoning included, at least 1; sent as the `max_tokens` request field for both LLM APIs. Missing means ollmpiada sets no cap (for `anthropic-messages` PydanticAI then sends the model's maximum output) |
| `reasoning_budget` | no | `openai-completions` models only: most reasoning tokens per call, at least 1 and below `max_tokens` when both are set; sent as the `reasoning_budget_tokens` request field. Missing means nothing is sent |
| `max_options` | no | `systemone` models only: most options the server accepts in one question, at least 2; longer move lists are [asked per piece](game-master.md#answer-formats) in the same query. Missing means no limit |

- **Decision:** reasoning effort is chosen per player when a competition starts,
  from the model's `reasoning_efforts`, and written on the command line as
  `<id>@<effort>`. A bare `<id>` sends no effort, so the provider's default
  applies; an effort is rejected for a model that lists none. Statistics are kept
  per model *and* effort, so `qwen3-30b-local@high` and `qwen3-30b-local@low` are
  separate players.
- Effort values follow the OpenAI `reasoning_effort` names: `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, `max`. A model lists only the ones its server
  accepts. For `openai-completions` the effort is sent as is
  (`openai_reasoning_effort`); for `anthropic-messages` it goes through
  PydanticAI's unified `thinking` setting: `none` turns thinking off, other levels
  are mapped per Claude model (adaptive thinking with that effort on current
  models, a token budget on older ones). `systemone` models take no effort, and
  the loader rejects one that lists any, or `max_tokens`; likewise it rejects
  `max_options` on a model of any other `api`, and `reasoning_budget` on a model
  of any `api` but `openai-completions`.
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
  template marks the thinking block. A proxy must forward unknown request fields:
  per Bifrost's request-options docs, its standard route (`/v1`) collects them as
  extra parameters, forwarded when passthrough is on
  (`x-bf-passthrough-extra-params: true`), while its OpenAI integration route
  (`/openai/v1`) takes such fields only nested under `extra_params`, so a
  top-level budget sent there does not reach the server.
- **Sensitive:** for `anthropic-messages` PydanticAI looks the model up by name to
  know how to send thinking, so a proxy route prefix is stripped for that lookup
  (`anthropic/claude-haiku-5-5` is treated as `claude-haiku-5-5`). With thinking
  on, Claude cannot be forced to call the output tool, so PydanticAI asks for
  JSON-schema output instead; a bare id with no effort thinks by default.

The loader validates the file with Pydantic at startup: unique ids, existing
provider names, known `api` and effort values, `max_options` of at least 2,
`max_tokens` of at least 1, a `reasoning_budget` of at least 1 and below
`max_tokens`, a positive `timeout_s`. An
invalid file stops the app with a message naming the bad entry.

## Environment

`.env` in the project root is loaded at startup with python-dotenv; variables
already set in the shell take precedence. `.env` is gitignored and agents must
not read it. `.env.example` lists the variable names with empty values.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLMPIADA_MODELS_CONFIG` | `config/models.json` | Path to the model config |
| `OLLMPIADA_RUNS_DIR` | `.local/runs` | Where competition results are written |
| `OLLMPIADA_REQUEST_TIMEOUT_S` | `120` | Timeout of one model call in seconds; a provider's `timeout_s` overrides it ([provider fields](#provider-fields)) |
| *names from `token_env`* | none | Provider tokens |
