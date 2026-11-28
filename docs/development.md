---
related: ["README.md", "conventions.md", "testing.md"]
---

# Development

[Documentation index](README.md)

Owner of: local setup, everyday commands, the coding-agent configuration layout
and the contribution workflow.

## Setup

```bash
uv sync            # create .venv and install the locked dependencies
```

Chess competitions also need [Stockfish](https://stockfishchess.org/) (for example
`brew install stockfish` or `apt install stockfish`) on `PATH`, or its path in
`OLLMPIADA_STOCKFISH`; it adjudicates games at the turn limit (see
[end of game](game-master/results.md#end-of-game)). Tests do not need it: the one test
against the real engine is skipped without it.

Then copy `.env.example` to `.env` and fill the tokens the configured providers
need; variable names are owned by [configuration](configuration.md#environment).
Models are served elsewhere (llama.cpp, a proxy, a hosted API, `basal-serve` for
decision models); this app only calls them.

## Commands

| Task | Command |
| --- | --- |
| List models | `uv run ollmpiada models` |
| Check one model's answers | `uv run ollmpiada probe <model>[@effort]` (one real request per answer format: two for chess) |
| Play a competition | `uv run ollmpiada run <a>[@effort] <b>[@effort]` (flags in [architecture](architecture.md#cli-and-terminal-ui)) |
| Show a saved competition | `uv run ollmpiada show .local/runs/<run-folder>` |
| Play or inspect an experiment | `uv run ollmpiada experiment list\|status <id>\|run <id>` (see [experiments](experiments.md#commands)) |
| Tests | `uv run pytest` |
| Lint and format | `uv run ruff check --fix && uv run ruff format` |
| Documentation check | `uv run python scripts/check_docs.py` |
| Add a dependency | `uv add <pkg>` (tooling: `uv add --dev <pkg>`) |

## Coding-agent configuration

One source of truth serves Claude Code, Codex, OpenCode and Pi.

- `AGENTS.md` - instructions for every agent; `CLAUDE.md` is a symlink to it.
- `.agents/skills/<name>/SKILL.md` - project skills; `.agents/commands/<name>.md` -
  project slash commands. Edit them here only.
- Tool directories hold only settings and relative symlinks. **Rule:** never put
  skill or command content in a tool directory, and never use absolute symlink
  targets (they break on another machine).

| Tool | Reads natively | Links into `.agents/` | Project config |
| --- | --- | --- | --- |
| Claude Code | `CLAUDE.md` (the symlink) | `.claude/skills`, `.claude/commands` | `.claude/settings.json`, `.mcp.json` |
| Codex | `AGENTS.md`, `.agents/skills` | none; no project commands | `.codex/config.toml` (trusted projects only) |
| OpenCode | `AGENTS.md`, `.agents/skills` | `.opencode/commands` | `opencode.json` |
| Pi | `AGENTS.md`, `.agents/skills` | `.pi/prompts` | `.pi/settings.json`, `.pi/mcp.json` |

- **Rule:** command files use only `description` frontmatter and `$ARGUMENTS`;
  positional `$1` means different arguments in different tools.
- Personal, uncommitted overrides: `.claude/settings.local.json` (gitignored). The
  other tools take personal settings from their user-level config in `~`.

## Workflow

Tasks live in `.local/TASKS.md` (local, not committed). Work on `main` or a
short-lived branch; there are no releases.
