---
related: ["README.md", "architecture.md", "configuration.md"]
---

# Experiments

[Documentation index](README.md)

Owner of: the experiment folder, `experiment.json`, when a planned competition is
done, and how `ollmpiada experiment` plays and reports them.

An experiment is a planned set of competitions that answers one question, such as
"does model A beat model B at effort `none` from both sides". It is written down
once and played by one command, which can be run again until everything is played.

## Experiment folder

- **Decision: an experiment is a folder.** `<experiments root>/<id>/`, where the
  folder name is the experiment's short ID (for example
  `2026-11-bielik32-basal-ref`), holds:
  - `README.md` - goal and method in free text, written by hand: what is compared
    and what is held constant (for example "effort none only"). The runner never
    reads it.
  - `experiment.json` - what to play (below). The runner only reads it.
  - `runs/` - the run folders, written by the runner. Their names and contents are
    those of any competition (see
    [competition runner](architecture.md#competition-runner)); only the folder they
    are saved in differs.

  The experiments root is `OLLMPIADA_EXPERIMENTS_DIR`, default `.local/experiments`
  (see [environment](configuration.md#environment)), so experiments stay out of
  the repository with the rest of `.local/`.

## experiment.json

- **Decision: JSON for every config.** The same format as `config/models.json`,
  read with the standard library and validated with Pydantic; no new dependency.

```json
{
  "game": "chess",
  "games": 30,
  "max_turns": 50,
  "models": {"B32": "Bielik-11B-v3.2-Instruct-GGUF:Q8_0-ctx4k@none", "HAIKU": "haiku-5-5@none"},
  "competitions": [
    {"players": ["B32", "HAIKU"]},
    {"players": ["HAIKU", "B32"], "runs": 2}
  ]
}
```

- **Decision: the shape of `experiment.json`.** `game`, `games` and `max_turns`
  hold for every competition, so an experiment compares players under one
  setting; `models` names each player once and competitions refer to it by short
  name.

| Field | Required | Meaning |
| --- | --- | --- |
| `game` | yes | A game name, as for `--game` (`chess`, `bridge`) |
| `games` | yes | Games per competition, at least 1; even for a game played in duplicate pairs (bridge) |
| `max_turns` | no | Moves per side before adjudication, at least 1. Follows `ollmpiada run`: a game with a turn limit defaults to 50, a game without one rejects the field |
| `models` | yes | Short name to `<model-id>[@effort]`, the player names of [model fields](configuration.md#model-fields) |
| `competitions` | yes | At least one entry, played in this order |
| `competitions[].players` | yes | Two short names in side order: the first plays the first side in odd games |
| `competitions[].runs` | no | How many finished competitions of this pair are wanted, at least 1; default 1 |

The file is validated when it is loaded, by every `experiment` command, and an
invalid file stops the command with a message that names the file and the
problem: an unknown field, a missing or out-of-range value, an unknown game, a
`max_turns` for a game without a turn limit, an odd `games` for bridge, a model
name `ollmpiada run` would reject (an unknown model id, an effort the model does
not list, a model whose token is not set), a competition with an unknown short
name or the same player twice, and a competition listed twice. Two entries are
the same competition when they name the same two players in the same order after
the short names are resolved; the reverse order is another competition.

## Done and pending

- **Decision: done is read from the run folders; there is no state file.** A
  competition is done when `runs/` holds at least `runs` run folders whose
  `competition.json` has status `finished`, the same two players in the same order
  (full names with effort) and the experiment's game, games and max turns. A run
  folder that matches no competition, or has no readable `competition.json`, is
  ignored.

Stopped and aborted runs, and runs left `running` by a process that died, stay on
disk as history and count as failed attempts, never as done. Deleting a run folder
makes its competition pending again; editing `experiment.json` (another `games`,
another effort) makes earlier runs stop matching, so they count for nothing.

## Commands

| Command | What it does |
| --- | --- |
| `ollmpiada experiment list` | The experiment IDs in the root with how many competitions are done, `3/5`; an invalid experiment shows its error instead |
| `ollmpiada experiment status <id>` | One row per competition: number, players, wanted runs, finished runs, failed attempts and its run folders (an unfinished one with its status) |
| `ollmpiada experiment run <id>` | Plays the missing runs, then prints the status table and how many competitions are done |

- **Decision: one competition at a time, in file order.** No reordering and no
  parallel runs, because one GPU serves the local models. A competition that does
  not finish (aborted, unable to start, or failing with an error, which is logged
  with its traceback) is reported and the runner moves on to the next competition,
  without the remaining runs of the failed one. Running the command again plays
  only what is still pending.

`run` plays each competition in the same process through the same code as
`ollmpiada run`, in background mode (the live view without the board, or plain
lines when stdout is not a terminal), with a random seed per competition saved
in its `competition.json`. Before each competition it prints
`Competition 2 of 5: <a> vs <b>, run 1 of 2`, and after it the competition's
summary as `ollmpiada run` does. Ctrl+C stops the competition in progress as in
`ollmpiada run` and then ends the experiment instead of starting the next
competition. The exit code is 0 when every competition is done afterwards, else 1;
an experiment that cannot be loaded exits with 2.
