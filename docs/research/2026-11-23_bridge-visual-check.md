---
related: ["2026-11-23_bridge-live-check.md", "../game-master/bridge.md", "../architecture.md"]
---

# Bridge visual check

[Research](README.md)

The visual-mode run of bridge with real models, the part of the manual live check
that the [first live check](2026-11-23_bridge-live-check.md) left out. Frozen on
2026-11-23.

## Verdict

- The live view renders bridge with real models as specified in
  [bridge](../game-master/bridge.md#play): the compass board with the dummy and
  declarer labels, the header with the contract, the trick number and the tricks
  per pair, the `T<n> <leader>:` marks in the move list, and the results and
  statistics tables with points.
- One bug: the status line counted moves per side, so a bridge table read
  `game 2, move 24` after 46 cards. It now names the trick, as the board header
  does (`trick 12 of 13`); see [Status line](#status-line).
- Both models played every card legally on the first attempt, as in the first live
  check. The prompts stay unchanged (`prompt_version` `e1d578538219`).

## Question

Does the visual live view show a bridge table correctly while real models play it?

## Sources

- The command
  `ollmpiada run haiku-5-5 sonnet-5-5 --game bridge --games 4 --seed 12 --mode visual`,
  run inside a pseudo-terminal with `script` on 2026-11-23 from 16:16:38 to
  16:33:19, exit code 0.
- Run folder `.local/runs/20261123-161639_haiku-5-5_vs_sonnet-5-5` (local, not
  committed): `competition.json`, `game-001.json` to `game-004.json` and the
  matching `.pbn` records.
- The captured screen output with the ANSI codes stripped (57,141 lines, a
  temporary file, not kept). Frames are cited below by their status line and
  their line number in that capture.

## Findings

### Results

| Game | Board | North-South | Contract | NS tricks | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | haiku-5-5 | 2♣ | 6 | down 2 |
| 2 | 1 | sonnet-5-5 | 2♣ | 6 | down 2 |
| 3 | 2 | haiku-5-5 | 4♦ | 9 | down 1 |
| 4 | 2 | sonnet-5-5 | 4♦ | 9 | down 1 |

- **Totals:** haiku-5-5 26 points, 2 wins and 2 losses; sonnet-5-5 26 points,
  2 wins and 2 losses. Status `finished`, 4 of 4 games.
- **Duplicate pairs.** Both tables of each board took the same tricks, so in the
  notation of [statistics](../game-master/results.md#statistics) `t1 = t2` on both
  boards and the difference is 0: a tie on points, and one win each per board
  because each pair beat the contract once as defenders.

### Live view

- **Board.** The first frame of game 1 (capture line 81, status
  `Games 0 of 4 | game 1, move 1 | running`) shows the header
  `Contract 2♣ by South | trick 1 of 13 | tricks NS 0, EW 0`, then the four hands
  round the compass with `North (dummy)` above, `West` and `East` at the sides and
  `South (declarer)` below, each hand as four suit lines (`♣ A K Q 9`).
- **During play.** The frame at capture line 27613 (status
  `Games 1 of 4 | game 2, move 24 | running`) shows 46 cards played: the header
  `Contract 2♣ by South | trick 12 of 13 | tricks NS 6, EW 5`, the hands reduced
  to their last cards (`-` for an empty suit), the two cards of trick 12 (`♣10`,
  `♥4`) in the middle, and the move list
  `T1 W: ♦2 ♦J ♦A ♦4 T2 E: ♦6 ♦K ♦5 ♦7 … T11 W: ♠K ♠5 ♠10 ♠7 T12 W: ♣10 ♥4`,
  one mark before each trick's lead, naming the trick and the seat that led it.
  Under the board: `Started 16:20:57, running 4:06 | 46 moves completed` and the
  per-player table with `▶` on haiku-5-5 (East-West) and 2 legal cards.
- **End of each table.** The capture holds a final header with `trick 13 of 13`
  for every table, matching the records: `tricks NS 6, EW 7` for 2♣ and
  `tricks NS 9, EW 4` for 4♦.
- **Tables.** The last frame (from capture line 57092, status
  `Games 4 of 4 | finished`) shows the results table with the four results
  (`6-7 down`, `6-7 down`, `9-4 down`, `9-4 down`, 52 moves each) and the
  statistics table with the `Points` column (26 and 26), followed by the closing
  lines `Competition finished: 4 of 4 games played.` and the run folder.
- **No rejections.** The capture has no `✗` entry in any move list.

### Status line

The status line counted the game in moves per side (`move {turn // 2 + 1}`), the
unit of `--max-turns` for chess, which assumes the two sides alternate. In bridge
four seats play in turn and dummy's cards are the declarer's moves, so the count
meant nothing: the frame at capture line 27613 reads `game 2, move 24` beside a
header saying `trick 12 of 13`, and every table's status climbed to `move 27`.

The fix is a game hook, `Game.progress(state, turn)` (see the
[game interface](../game-master/README.md#game-abstraction)): the base class keeps
the move per side, which chess uses unchanged, and bridge returns the trick its
board header shows. Replaying game 2's first 46 cards from its `game-002.json`
through the hook gives `trick 12 of 13` with tricks 6 and 5, matching the header
in that frame. The fixed status line was not seen in a live run.

### Answers, tokens and time

| Game | Calls | Forced cards | Input tokens | Output tokens | Model time |
| --- | --- | --- | --- | --- | --- |
| 1 | 43 | 9 | 56,818 | 32,358 | 253 s |
| 2 | 42 | 10 | 55,493 | 32,376 | 258 s |
| 3 | 41 | 11 | 54,180 | 27,894 | 228 s |
| 4 | 41 | 11 | 54,317 | 31,385 | 254 s |
| Total | 167 | 41 | 220,808 | 124,013 | 992 s |

- Every one of the 167 calls gave a legal card on the first attempt:
  `illegal_attempts` is 0 for both players and no turn in `game-NNN.json` has more
  than one attempt.
- From `competition.json`: haiku-5-5 made 84 calls in 448.0 s with 111,224 input,
  71,230 output and 70,216 reasoning tokens; sonnet-5-5 made 83 calls in 544.2 s
  with 109,584 input, 52,783 output and 51,780 reasoning tokens, 2 of its calls
  without a reasoning count. The game totals above sum to these.
- Each table's statistics line in the capture: game 1 started 16:16:43 and ended
  after 4:13, game 2 after 4:18, game 3 after 3:48, game 4 after 4:14; the whole
  run, warm-up included, took 16 min 41 s.

## Limits

- The run went through a pseudo-terminal (`script`), not an interactive terminal
  someone watched; only the captured output was inspected, frame by frame as
  cited above. Colors, flicker and the refresh between events were not judged.
- The status-line fix is verified by tests and the replay above, not by a second
  live run.
- Four tables on two deals say nothing about the models' skill.
