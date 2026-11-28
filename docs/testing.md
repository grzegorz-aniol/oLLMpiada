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
  links, anchors and images (in `docs/` and the repository `README.md`) and
  orphaned documents fail the suite. It also checks that the numbers the README's
  [supported games](../README.md#supported-games) quotes (opening moves, turn
  limit, adjudication threshold, attempts per turn, trump fit, recommended boards)
  match their code constants.

- **Rule: no real models in tests.** Game master, competition and UI tests use
  scripted fake players that return prepared answers (legal moves, illegal moves,
  resignations, timeouts), so every end-of-game rule in
  [game master](game-master/results.md#end-of-game) is testable offline.
  The fake is `ScriptedPlayer` in `tests/fakes.py`; its subclass `PickingPlayer`
  answers with the legal move at a fixed place in the list, which plays a whole
  bridge deal without a script. `tests/conftest.py` holds the
  shared model config fixtures (`models_data`, `models_file`). A competition calls
  each player once to [warm it up](architecture.md#competition-runner) before the
  first game, so a competition script starts with `WARM_UP` (the test helpers
  prepend it) unless the test is about the warm-up.
- **Rule:** tests run with the [opening](game-master/turns.md#opening) turned off
  (`OPENING_MOVES` set to 0 by the autouse fixture `no_opening` in
  `tests/conftest.py`), so a script can resign or offer a draw on its first move.
  Only `games/base.py` reads the constant, at call time, so patching it there
  turns the opening off everywhere.
  A test of the opening rule itself is marked `@pytest.mark.opening` and runs
  with the real value.
- **Rule: no engine in tests.** Turn-limit adjudication is tested with `Judged`
  in `tests/fakes.py`, chess whose `evaluate` returns prepared scores, and CLI
  tests replace `ChessGame.session` so no engine starts. One test runs the real
  Stockfish and is skipped when it is not installed.
- Experiment [servers](experiments.md#servers) are tested without a GPU or a real
  server: `tests/fake_server.py` is a small HTTP server a provider's `start`
  command runs; it lists models in either shape, can ignore SIGTERM, and keeps a
  child process in its group, so a test can check that a stop ends the whole
  group. `FakeServer` in `tests/fakes.py` builds its command line on a free port.
  Tests give `Servers` short timeouts; CLI tests shorten its defaults (fixture
  `quick_servers` in `tests/test_experiment.py`), and the `models_file` fixture
  points `OLLMPIADA_SERVICES_DIR` into the test's `tmp_path`. A test that starts
  the fake server itself starts it in a new session (`start_detached` in
  `tests/test_servers.py`) and kills the whole group with `os.killpg`, because
  killing the server alone leaves its child running.
- The [game hooks](game-master/README.md#game-abstraction) set the way chess
  never sets them (`move_only` always true, no `turn_limit`, a recording `forfeit`,
  a `move_mark` that does not follow move pairs, its own wording of answer format,
  questions and rejections) are tested with `MovesOnly` in
  `tests/fakes.py`, a game of moves alone; tests add it to `MASTERS` to run it
  through the CLI.
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

## Golden prompts

`tests/test_golden.py` pins what a model reads in the sample turns, so a refactor
cannot change a prompt unnoticed. For each game in the registry and each of its
sample turns (`sample_requests` in `competition.py`: `opening`, the opening turn,
and `later`, a turn after the opening with a pending draw offer and a rejected
attempt), `tests/golden/<game>/<turn>/` holds:

- `llm_message.txt` - the LLM user message (`llm_message`);
- `answer_schema.json` - the JSON schema of the LLM answer model (`answer_type`);
- `decision_state.txt`, `decision_questions.json` - the `state` and `questions`
  the decision player posts, captured from `DecisionPlayer` over a mock transport;
- `decision_questions_by_piece.json` - the same questions asked per piece, with
  `max_options` one below the number of legal moves.

`tests/golden/<game>/prompt_version.txt` holds the game's
[prompt version](game-master/prompts.md#prompts), so a new hash shows in the same
diff. The test runs with the real [opening](game-master/turns.md#opening) and
fails on any difference, a missing file or a file no longer rendered; its message
lists the files and their diff. The game master's rejection reasons
(`Game.rejection`) appear in no sample turn and are not pinned.

- **Rule:** regenerate the golden files only for a deliberate change to
  model-facing text, then review their diff with the change:

  ```bash
  UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
  ```

  It rewrites each game's folder from scratch, so a removed file disappears too.

## Writing tests

- **Rule:** test behavior through the public interface, not private helpers.
- **Rule:** a bug fix starts with a test that reproduces the bug.
