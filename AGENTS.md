# oLLMpiada

Board-game competitions between AI models: a game master referees two models
(LLMs or decision models) through a series of games and collects statistics.
Chess first; the game is an abstraction so other games can follow. This is an
experiment, not a production app.

`CLAUDE.md` is a symlink to this file; Claude Code, Codex, OpenCode and Pi all read it.

## Read before working

Start at [docs/README.md](docs/README.md), the documentation router and its
maintenance policy. Read [conventions](docs/conventions.md) for every change, then
only the subject document for the area you touch. Do not load every document or
assume undocumented patterns.

Work items live in `.local/TASKS.md` (local, not committed). Tick a task in the same change that
completes it; add new tasks there, never in `docs/`.

## Documentation

`docs/` is the project's self-maintained knowledge base. The full policy is in
`docs/README.md`; the fundamentals:

- Update docs in the same change as behavior, patterns and decisions.
- Keep every document reachable from `docs/README.md` and every link valid;
  `uv run python scripts/check_docs.py` checks it (also part of `uv run pytest`).
- Give each rule one home, marked inline with `**Rule:**`, `**Decision:**` or
  `**Sensitive:**`; elsewhere link to it instead of copying it.
- Split a document past about 300 lines of mixed topics; restructure as a side
  effect of the current task and fix inbound links in the same change.
- Plans and tasks live in `.local/TASKS.md`, not in `docs/`. Research goes to
  `docs/research/` as dated, frozen snapshots.

## Commands

```bash
uv sync                                   # install
uv run pytest                             # tests, including the docs check
uv run ruff check --fix && uv run ruff format   # lint and format
uv run ollmpiada run <a> <b>              # play a competition in the terminal
```

Dependencies change only through `uv add` / `uv remove` (`--dev` for tooling).
Never use `pip`.

## Agent configuration

Skills live in `.agents/skills/<name>/SKILL.md`, commands in
`.agents/commands/<name>.md`. Tool directories (`.claude/`, `.codex/`, `.opencode/`,
`.pi/`) hold only tool settings and relative symlinks; see
[development](docs/development.md#coding-agent-configuration). A command that Codex
must also run belongs in a skill, because Codex has no project commands.

## Rules

- Write behavior tests with every change and run `uv run pytest` and
  `uv run ruff check` before finishing.
- Never read or print `.env`; it holds provider tokens. Settings come from
  environment variables, loaded from `.env` at startup.
- Tests never call a real model; they use scripted fake players (see
  [testing](docs/testing.md)).
- Both players of a game get the same prompt text; only the answer format differs.
  The rule is owned by [game master](docs/game-master/prompts.md#prompts).
