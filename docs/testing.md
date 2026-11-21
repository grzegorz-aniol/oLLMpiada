---
related: ["README.md", "conventions.md"]
---

# Testing

[Documentation index](README.md)

Owner of: test layers, how to run them and what each must cover.

## Running

```bash
uv run pytest                 # everything
uv run pytest tests/test_x.py -k name   # one test
```

## Layers

- **Unit** - `tests/`, no network or external services; fast enough to run on
  every change.
- **Documentation** - `tests/test_docs.py` runs `scripts/check_docs.py`, so broken
  links and images (in `docs/` and the repository `README.md`) and orphaned
  documents fail the suite.

- **Rule: no real models in tests.** Game master, competition and UI tests use
  scripted fake players that return prepared answers (legal moves, illegal moves,
  resignations, timeouts), so every end-of-game rule in
  [game master](game-master/results.md#end-of-game) is testable offline.
  The fake is `ScriptedPlayer` in `tests/fakes.py`; `tests/conftest.py` holds the
  shared model config fixtures (`models_data`, `models_file`). A competition calls
  each player once to [warm it up](architecture.md#competition-runner) before the
  first game, so a competition script starts with `WARM_UP` (the test helpers
  prepend it) unless the test is about the warm-up.
- **Rule: no engine in tests.** Turn-limit adjudication is tested with `Judged`
  in `tests/fakes.py`, chess whose `evaluate` returns prepared scores, and CLI
  tests replace `ChessGame.session` so no engine starts. One test runs the real
  Stockfish and is skipped when it is not installed.
- Player adapters are tested against recorded HTTP exchanges or PydanticAI's
  `TestModel` / `FunctionModel`, never a live endpoint. The OpenAI and Anthropic
  clients take an `httpx2` client (`httpx2.MockTransport`); the decision player
  uses `httpx` (`httpx.MockTransport`). PydanticAI streams a Claude call only
  when no `max_tokens` is set and its default (the model's maximum output) is
  large, which it is for current models, so an Anthropic mock answers with
  server-sent events and, for a capped model, with a plain JSON message.
- Terminal views are tested by rendering into a recording Rich `Console`
  (`Console(record=True)`) and checking the exported text; CLI commands are
  tested by calling the entry point with arguments and checking output and exit
  code.

<!-- Add a manual smoke-test layer (a real local model) when it exists: what it
needs running and how to start it. -->

## Writing tests

- **Rule:** test behavior through the public interface, not private helpers.
- **Rule:** a bug fix starts with a test that reproduces the bug.
