---
related: ["README.md", "testing.md", "development.md"]
---

# Conventions

[Documentation index](README.md)

Rules for every change. Subject-specific rules live in their subject document;
this file owns only what applies everywhere.

## General

- **Rule: simplest thing that works.** YAGNI and KISS: no abstraction, option or
  layer before a second real use needs it. Delete dead code instead of keeping it.
- **Rule: comments explain why, never what.** If a comment describes what a line
  does, rename things instead. Keep inline comments to two lines.
- **Rule: tests with every change.** Behavior changes come with tests of behavior;
  see [testing](testing.md).
- **Rule: configuration through environment.** Settings are read from environment
  variables in one place; secrets never enter the repository. `.env` is gitignored,
  and the Claude Code and OpenCode configs deny reading it.

## Python

- **Rule: uv only.** Add and remove dependencies with `uv add` / `uv remove`
  (`--dev` for tooling). Never `pip install`, never edit the lock file by hand.
- **Rule: lint and format with ruff.** `uv run ruff check --fix` and
  `uv run ruff format` before finishing.
- Imports are absolute from the package root (`from <package>.models import Item`),
  grouped stdlib / third-party / local.
- Type hints use modern syntax (`list[str]`, `X | None`, `def f[T](x: T) -> T`).

<!-- Add a pattern with this shape when one gets established:

### <Pattern name>

**When:** <scenario>. **Pattern:** <description>.
Good: `<example>` / Bad: `<anti-pattern>`. **Why:** <rationale>.
-->
