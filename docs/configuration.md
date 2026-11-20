---
related: ["README.md", "architecture.md", "game-master.md", "development.md"]
---

# Configuration

[Documentation index](README.md)

Owner of: the model config file, provider tokens and the environment variables
the app reads.

## Model config

`config/models.json` lists the providers and the models available for
competitions. It is committed: it holds no secrets, only the names of environment
variables that carry tokens. The path can be changed with `OLLMPIADA_MODELS_CONFIG`.
The committed file holds this example:

```json
{
  "providers": [
    {"name": "local-llama", "url": "http://127.0.0.1:1234/v1"},
    {"name": "openrouter", "url": "https://openrouter.ai/api/v1",
     "token_env": "OPENROUTER_API_KEY"},
    {"name": "basal", "url": "http://127.0.0.1:8000/v1/systemone"}
  ],
  "models": [
    {"id": "qwen3-30b-local", "provider": "local-llama", "model": "qwen3-30b-a3b",
     "type": "llm", "reasoning_efforts": ["none", "low", "medium", "high"]},
    {"id": "gpt-oss-120b", "provider": "openrouter", "model": "openai/gpt-oss-120b",
     "type": "llm", "reasoning_efforts": ["low", "medium", "high"]},
    {"id": "basal-mini", "provider": "basal", "model": "Remek/basal-1.5-mini-GGUF",
     "type": "decision", "reasoning_efforts": []}
  ]
}
```

### Provider fields

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique name that models refer to |
| `url` | yes | Base URL. For `llm` models: an OpenAI-compatible API root (ending in `/v1`). For `decision` models: the full SystemOne endpoint URL |
| `token_env` | no | Name of the environment variable holding the bearer token |

- **Rule:** the config never contains a token value, only `token_env`. Without
  `token_env`, or when the variable is empty, requests carry no `Authorization`
  header; this is the normal case behind a proxy that handles security itself.
- **Rule:** a `token_env` that names a variable missing from the environment is a
  startup warning, not an error: `ollmpiada models` shows the model as
  unavailable and `ollmpiada run` refuses it.

### Model fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique name used on the command line and in statistics |
| `provider` | yes | A provider `name` |
| `model` | yes | Model name sent to the provider |
| `type` | yes | `llm` (chat completion with structured output) or `decision` (SystemOne API) |
| `reasoning_efforts` | no | Reasoning efforts the model accepts; empty or missing means reasoning cannot be set |

- **Decision:** reasoning effort is chosen per player when a competition starts,
  from the model's `reasoning_efforts`, and written on the command line as
  `<id>@<effort>`. A bare `<id>` sends no effort, so the provider's default
  applies; an effort is rejected for a model that lists none. Statistics are kept
  per model *and* effort, so `qwen3-30b-local@high` and `qwen3-30b-local@low` are
  separate players.
- Effort values follow the OpenAI `reasoning_effort` names that PydanticAI passes
  through (`openai_reasoning_effort`): `none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`. A model lists only the ones its server accepts.
- **Decision:** every `llm` provider is reached through an OpenAI-compatible chat
  completions API (PydanticAI `OpenAIChatModel` with `OpenAIProvider(base_url=...,
  api_key=...)`). Native Anthropic or Gemini APIs are reached through a proxy or
  added later as a provider field.

The loader validates the file with Pydantic at startup: unique ids, existing
provider names, known `type` and effort values. An invalid file stops the app
with a message naming the bad entry.

## Environment

`.env` in the project root is loaded at startup with python-dotenv; variables
already set in the shell take precedence. `.env` is gitignored and agents must
not read it. `.env.example` lists the variable names with empty values.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLMPIADA_MODELS_CONFIG` | `config/models.json` | Path to the model config |
| `OLLMPIADA_RUNS_DIR` | `runs` | Where competition results are written |
| `OLLMPIADA_REQUEST_TIMEOUT_S` | `120` | Timeout of one model call |
| *names from `token_env`* | none | Provider tokens |
