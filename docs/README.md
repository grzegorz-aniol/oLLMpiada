---
related: ["product.md", "architecture.md", "conventions.md"]
---

# oLLMpiada documentation

Start here. This file routes you to the subject you are working on and holds the
maintenance policy for everything in `docs/`. Read [conventions](conventions.md)
for every change, then only the subject you touch; you do not need every sibling.

## Find the subject

| Working on | Start here |
| --- | --- |
| What the project is, who it is for, scope and non-goals | [Product](product.md) |
| Components, data flow, CLI commands and terminal UI, where code lives | [Architecture](architecture.md) |
| Refereeing: game interface, turns, prompts, answers, end of game, statistics | [Game master](game-master/README.md) |
| Model config file, providers, tokens, environment variables | [Configuration](configuration.md) |
| Coding rules that apply to every change | [Conventions](conventions.md) |
| Setup, commands, agent configuration, contribution workflow | [Development](development.md) |
| Test layers, how to run and write tests | [Testing](testing.md) |
| Dated research, spikes and evaluations | [Research](research/README.md) |

<!-- Keep this table at about eight rows. When a subject grows into a folder, point
its row at `<subject>/README.md`. -->

## Maintaining this knowledge base

This policy is self-contained: contributors and agents need nothing outside the
repository to follow it. `AGENTS.md` carries the short version; this section is
authoritative.

- **Rule: top-down discovery.** `AGENTS.md` points here; this index routes to
  subjects; every subject hub links each of its documents. Every document is
  reachable from this file and every link resolves. Check it with
  `uv run python scripts/check_docs.py`.
- **Rule: two kinds of folders.** A *topic folder* holds a few different documents
  about one subject; its `README.md` hub links every child with a one-line
  description. A *catalog folder* holds many documents of one kind (research
  snapshots, evaluation reports, generated output); its `README.md` sets
  `catalog: true` in frontmatter and describes what the files hold and how they
  are named, never lists them. `ls` is the catalog's index; a hand-kept file list
  goes stale.
- **Rule: grow down, not across.** A subject starts as one file, `docs/<subject>.md`.
  When it passes about 300 lines of mixed topics, split it into
  `docs/<subject>/README.md` plus leaves. Create a topic folder only when it would
  hold three or more documents including its hub, and only as a side effect of real
  work, never in advance; a catalog folder may exist as soon as its kind of file
  does. Maximum depth is `docs/<subject>/<area>/<topic>.md`.
  Keep this root at about eight subjects.
- **Rule: granularity.** A document is what one task reads together. Aim for
  40-80-line hubs and 100-250-line leaves. A topic under about 15 substantive
  lines belongs as a section in an existing document. Hubs route; they do not
  restate their children.
- **Rule: one owner per rule.** Each rule, contract or decision lives in exactly one
  document, next to the thing it constrains, marked inline with `**Rule:**`,
  `**Decision:**` or `**Sensitive:**`. Other documents link to the owner instead of
  copying it. Aggregated views come from `grep -rn '\*\*Rule:\*\*' docs/`; never
  maintain a separate catalog of rules or decisions.
- **Rule: same-change maintenance.** Update affected documents and hubs in the same
  change as the behavior, pattern or decision. Put new knowledge into its existing
  owner with the reason and, when it helps, a good-versus-bad example.
- **Rule: restructuring is a side effect.** Split, merge or delete documents that
  the current change touches. A reorganization with no motivating change is
  proposed first. When moving or deleting, update every inbound reference in the
  same change (grep the old path with and without `.md`, including code comments,
  scripts, workflows, skills and `AGENTS.md`) and rerun the check.
- **Rule: cross-links.** Use relative Markdown links, with heading anchors when they
  help. Optional `related` frontmatter lists relative paths including `.md`; it
  records graph edges the folder tree cannot express.
- **Rule: current truth, git is history.** Documents describe what is true now.
  Requirements change only through an explicit human decision; after it, remove
  superseded wording everywhere in the same change. Never keep a file for history.
- **Rule: not a backlog.** Plans, tasks and roadmaps live in the issue tracker
  (`.local/TASKS.md`). A document records what is and why, not what is to be done.
- **Rule: research is dated and frozen.** Snapshots go to [research](research/README.md)
  as `YYYY-MM-DD_slug.md` and are not updated afterwards. When a conclusion ships,
  record it in the owning subject document, which links the snapshot.
