---
related: ["../game-master/bridge.md", "../game-master/prompts.md", "../game-master/results.md"]
---

# Bridge live check

[Research](README.md)

The first run of bridge card play with real models, for the manual live check of
the bridge MVP. Frozen on 2026-11-23.

## Verdict

- The bridge prompts need no tuning. Every one of 164 model calls gave a legal
  card on the first attempt, so the prompt templates and `prompt_version`
  `e1d578538219` (the hash of the rendered prompts) stay as they are.
- The game runs end to end with real models: four tables, the duplicate pairs, the
  PBN records and the statistics all come out as specified in
  [bridge](../game-master/bridge.md).
- The sample is far too small for a skill conclusion. Two deals, one model a
  single trick ahead on one of them, and a replay of the same deal ended three
  tricks apart.

## Question

Do real models play legal bridge cards from the prompts as written, what does a
table cost in calls, tokens and time, and does the duplicate comparison work on a
real run?

## Sources

- Run folder `.local/runs/20261123-154749_haiku-5-5_vs_sonnet-5-5` (local, not
  committed): `competition.json`, `game-001.json` to `game-004.json` and the
  matching `.pbn` records, from
  `ollmpiada run haiku-5-5 sonnet-5-5 --game bridge --games 4 --seed 11 --mode background`
  with `prompt_version` `e1d578538219`.
- An earlier run with the same models and seed,
  `.local/runs/20261123-153747_haiku-5-5_vs_sonnet-5-5`, interrupted after game 1.
- `ollmpiada probe haiku-5-5 --game bridge` and
  `ollmpiada probe sonnet-5-5 --game bridge`.

## Findings

### Results

| Game | Board | North-South | Contract | NS tricks | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | haiku-5-5 | 4♦ | 9 | down 1 |
| 2 | 1 | sonnet-5-5 | 4♦ | 10 | made |
| 3 | 2 | haiku-5-5 | 2♣ | 6 | down 2 |
| 4 | 2 | sonnet-5-5 | 2♣ | 6 | down 2 |

- **Totals:** sonnet-5-5 27 points, 3 wins and 1 loss; haiku-5-5 25 points,
  1 win and 3 losses.
- **Duplicate pairs.** Both tables of a board share the `Deal` tag and the board
  number, with the pairs swapped, as intended. In the notation of
  [statistics](../game-master/results.md#statistics), `t1` is haiku-5-5's tricks
  as declarer and `t2` sonnet-5-5's:
  - board 1: `t1 = 9`, `t2 = 10`, difference `2·(t1 - t2) = -2`;
  - board 2: `t1 = 6`, `t2 = 6`, difference 0.

  The -2 is the whole 25-27 points gap: sonnet-5-5 took one trick more as
  declarer on one board.
- **Wins exaggerate the gap.** On board 2 both declarers took the same six tricks,
  yet the board counts as one win each, because each pair beat the contract once
  as defenders. The 3-1 split in wins rests on a single trick on board 1; the
  points show the real margin.

### Answers and rejections

- No rejection in any of the four games: every attempt was accepted, and no turn
  needed a retry or a forfeit.
- Every answer came as a tool call (both models use `output_mode` `tool`, the
  default) with the arguments `{"move":"<card>"}`, so the tool call, not the
  prompt, enforced the JSON shape. Every card was legal and spelled as the prompt
  lists it, in suit-symbol form (`♥A`, `♦10`).
- `probe haiku-5-5 --game bridge` answered `{"move":"♠A"}`, legal, in 3.56 s with
  1,171 input and 246 output tokens.
- `probe sonnet-5-5 --game bridge` answered `{"move":"♠A"}`, legal, in 1.36 s with
  1,171 input and 12 output tokens; the provider reported no reasoning, so its
  reasoning is unknown.

### Calls and forced cards

| Game | Calls | Forced cards | Input tokens | Output tokens | Model time |
| --- | --- | --- | --- | --- | --- |
| 1 | 43 | 9 | 56,906 | 30,175 | 253 s |
| 2 | 39 | 13 | 51,422 | 28,080 | 241 s |
| 3 | 41 | 11 | 53,893 | 26,247 | 225 s |
| 4 | 41 | 11 | 54,116 | 35,095 | 259 s |
| Total | 164 | 44 | 216,337 | 119,597 | 978 s |

- Each table plays 52 cards; 44 of the 208 (21%) were forced and cost no call. 16
  of them are the four cards of each last trick; the other 28 were the only legal
  card earlier in the play.
- A call reads about 1,300 input tokens. Nearly all output is reasoning: 117,619
  of the 119,597 output tokens.
- Per call, haiku-5-5 averaged 5.2 s and 800 output tokens (at most 22.6 s and
  4,369 tokens), sonnet-5-5 6.8 s and 660 output tokens (at most 16.6 s and 1,737
  tokens).

### Time

- A table took 3 min 45 s to 4 min 19 s of wall clock (from the run folder's file
  times; game 1 includes start-up), about the same as its model time: calls run
  one after another and the game master's own work is negligible.
  The whole competition took 16 min 22 s.
- The earlier, interrupted run spent 301 s of model time on its game 1.

### Models are not deterministic

The interrupted run played the same board 1 with haiku-5-5 declaring, and
haiku-5-5 took 12 tricks (made with two overtricks), against 9 tricks in game 1 of
the full run. Same models, same seed, same deal, same prompts: a three-trick
spread. One table per board says little; any skill comparison needs many boards.

## Live check

The manual live check of the bridge MVP is done except the visual-mode run, which
was not run. Done: a probe of each of the two models and a four-game competition
between them, the answers read in `game-NNN.json`, and no prompt change needed.
The prompts actually sent are not recorded in the run files and were not
inspected in this run; they are covered by the tests that render them, and
`prompt_version` `e1d578538219` identifies them.

Not done: the competition ran with `--mode background`, not `--mode visual`, so
the terminal live view (the board and the trick marks) was not exercised with real
models. It is covered only by tests with fake players.
