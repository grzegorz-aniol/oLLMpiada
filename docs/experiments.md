---
related: ["README.md", "architecture.md", "configuration.md"]
---

# Experiments

[Documentation index](README.md)

Owner of: the experiment folder, `experiment.json`, when a planned competition is
done, how `ollmpiada experiment` plays and reports them, and how it starts, stops and
frees the inference servers around each competition.

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
| `max_turns` | no | Moves per side before adjudication, at least 1. Follows `ollmpiada run`: a game with a turn limit defaults to the [`--max-turns` default](architecture.md#cli-and-terminal-ui), a game without one rejects the field |
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
| `ollmpiada experiment run <id>` | Prepares the [servers](#servers) and plays the missing runs of each pending competition, then prints the status table and how many competitions are done |
| `ollmpiada experiment run <id> --dry-run` | Prints, per pending competition, which servers it would stop, whose `cleanup` it would run and which servers it would start, then what the end stops and cleans up; starts, stops and asks nothing |

- **Decision: one competition at a time, in file order.** No reordering and no
  parallel runs, because one GPU serves the local models. A competition that does
  not finish (aborted, unable to start, failing with an error, which is logged
  with its traceback, or whose [servers](#servers) cannot be prepared) is
  reported and the runner moves on to the next competition, without the remaining
  runs of the failed one. Running the command again plays only what is still
  pending.

`run` plays each competition in the same process through the same code as
`ollmpiada run`, in background mode (the live view without the board, or plain
lines when stdout is not a terminal), with a random seed per competition saved
in its `competition.json`. Before each competition it prints
`Competition 2 of 5: <a> vs <b>, run 1 of 2`, and after it the competition's
summary as `ollmpiada run` does. Ctrl+C stops the competition in progress as in
`ollmpiada run` and then ends the experiment instead of starting the next
competition; Ctrl+C while servers are being prepared ends it too. Either way the
owned servers are stopped before the command exits; a second Ctrl+C during those
stops leaves the remaining servers running with their PID files, and the next
`run` adopts and stops them (see [servers](#servers)). The exit code is 0 when every
competition is done afterwards, else 1; an experiment that cannot be loaded exits
with 2.

## Servers

Local models share one GPU, so before each competition the runner frees what the
previous one loaded and starts what this one needs. It does so from three optional
[provider fields](configuration.md#provider-fields) in `config/models.json`.

- **Decision: server management lives on providers in `models.json`.** `start` is a
  shell command that starts the provider's server, which the runner then owns: it
  starts and stops it (basal-serve with its own llama-server). `models_url` is a GET
  URL listing the served models. `cleanup` is a shell command that frees the GPU
  memory of a server that keeps running, and never stops it (the llama.cpp router:
  `just llama-unload`). A provider with none of them (a remote API) is left alone.
  `models_url` is written out, never derived from `url`, because chats may go
  through a tracking proxy such as Bifrost while readiness is read from the backend.
- **Decision: ready means the model is listed.** For every provider a competition
  uses that has a `models_url`, owned or not, the runner polls it until each of the
  competition's models on that provider is listed, for at most `READY_TIMEOUT_S`
  (in `servers.py`). Names are read loosely from `data[].id` (OpenAI
  shape: llama.cpp, Bifrost) or `models[].name` (basal-serve); any other answer is
  an empty list. A name matches when, ignoring case, either name ends with the
  other: `Remek/basal-2.0-max-GGUF` matches `basal-2.0-max-GGUF`, and
  `llama.cpp/speakleash/X` matches `speakleash/X`. On timeout the error lists the
  names the server did report, or says it gave no answer. An owned server whose
  processes have all exited fails at once, pointing to its output file.
- **Sensitive:** the llama.cpp router lists every cached model and loads one on the
  first request, so for it "listed" only means "router up"; the competition's
  [warm-up](architecture.md#competition-runner) does the loading, and a model that
  fails to load fails there.
- **Decision: owned servers are stopped as a process group.** `start` runs in a new
  session, so `just`, its script, basal-serve and its background llama-server share
  one process group. A stop sends SIGTERM to the group, waits up to
  `STOP_TIMEOUT_S` for every process to end, sends SIGKILL if any is left,
  and then waits until `models_url` stops answering, so the GPU memory is free
  before the next start. The group id goes to `<services dir>/<provider>.pid`, so a
  runner that was killed hard finds a group still running on its next start, adopts
  it and stops it like one it started. Servers without `start` (the router) are
  never signalled; `cleanup` is all the runner does to them.
- **Sensitive:** the stop kills everything left in the group, and only that: a
  process that `start` moves to its own session or group (`setsid`, a daemonizing
  server) escapes the stop and keeps its GPU memory. Group ids are reused, after a
  reboot and within one boot once process ids wrap, so a PID file is adopted only
  when a process of its group started no later than the file was written (read from
  `/proc`); otherwise it is deleted and the group, an unrelated one, is left alone.
  Off Linux there is no `/proc` and nothing is adopted.
- **Decision: GPU preparation before each competition.** In this order:
  1. stop every owned server the competition does not use;
  2. run the `cleanup` of every provider that has one, used or not;
  3. start the owned servers it uses that are not running, then wait until every
     provider it uses is ready;
  4. play the competition, saving into the experiment's `runs/`.

  A provider is used when one of the competition's two players is one of its
  models. Step 2 cleans up the providers the competition uses as well, because a
  server that keeps running (the router) would otherwise hold the previous
  competition's model next to the new one; the competition's warm-up loads what it
  needs again. An owned server the next competition still uses stays up, without a
  restart; one whose processes have all exited (killed for memory, say) is
  forgotten, and started again in step 3 when the competition uses it. On exit,
  also after Ctrl+C or an error, the runner stops every owned server it started or
  adopted and, if it prepared a competition, runs every `cleanup`. A `cleanup` that
  fails or takes longer than `STOP_TIMEOUT_S`, or a server that does not start (its
  command or output file cannot be opened) or get ready, fails the competition (a
  server that did not get ready is stopped first), and the runner moves on to the
  next one.

The services dir is `OLLMPIADA_SERVICES_DIR`, default `.local/services` (see
[environment](configuration.md#environment)). Besides the PID files it holds
`<provider>.log`, the output of the provider's `start` command, rewritten on each
start. The runner prints each step as it takes it (`Stopping the server of ...`,
`... is ready after 42 s`). `--dry-run` simulates the steps from no server
running, so it does not show servers a killed runner left behind.
