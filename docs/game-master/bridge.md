---
related: ["README.md", "turns.md", "prompts.md", "results.md"]
---

# Bridge

[Game master](README.md)

Owner of the bridge rules: the contract rule, seats and sides, card play, the
result, answer parsing and the PBN record. The code is `games/bridge.py`
(`BridgeGame`), with its prompt templates beside it; what the prompt says is owned
by [prompts](prompts.md#prompts).

## Card play from a fixed contract

Two models play the cards of one deal; there is no bidding, and a fixed rule picks
the contract from the deal.

- **Decision:** card play only, with the contract from a rule. This isolates the
  play skill: bidding brings conventions and partnership agreements, a second
  experiment for later. The contract need not be optimal; both pairs play the same
  contract on the same deal, which is what makes the comparison fair.
- **Decision:** no new dependency. Dealing, legality, trick winners and the result
  are a small amount of our own code. A double-dummy solver (`endplay`, which
  wraps DDS, the C++ double-dummy solver) ships wheels only up to Python 3.13 and
  needs a spike on 3.14 first.

The deal: `new_state(seed)` shuffles the deck with `random.Random(seed)` and deals
13 cards to each seat. The contract rule (`from_deal`):

1. The declaring pair is the pair with more HCP (high-card points: A=4, K=3, Q=2,
   J=1). On a 20-20 tie the same generator deals again, so the deal stays
   deterministic for the seed.
2. The strain is the pair's longest combined suit when it has at least 8 cards
   (ties: majors first, then higher rank, ♠♥♦♣), otherwise no trumps.
3. The level comes from the pair's combined HCP:

   | Pair HCP | No trumps | Suit |
   | --- | --- | --- |
   | 20-22 | 1NT | 2 |
   | 23-24 | 2NT | 3 |
   | 25-29 | 3NT | 4 |
   | 30-32 | 4NT | 5 |
   | 33-36 | 6NT | 6 |
   | 37+ | 7NT | 7 |

4. The declarer is the hand of the pair with more trumps (in no trumps, more HCP);
   ties go to more HCP, then to North or East. The table is turned so the declarer
   sits South.

## Seats and sides

| Seat | Role | Side |
| --- | --- | --- |
| South | Declarer | `FIRST` (North-South) |
| North | Dummy, played by the declarer | `FIRST` (North-South) |
| West | Makes the opening lead | `SECOND` (East-West) |
| East | Defender | `SECOND` (East-West) |

- **Decision:** four seats, two sides. One model plays both seats of its pair, so
  the two-player sides, statistics and tables work unchanged, and the fixed layout
  (declarer always South) keeps the prompt and the board simple. Each model sees
  only its seat's view, never its partner's hidden hand: the rule is owned by
  [prompts](prompts.md#prompts).

## Play

`legal_moves` lists the cards of the seat to play, only the suit led when it holds
any, ordered ♠♥♦♣ and high to low, labelled `♠A`, `♥10`; ids are suit plus rank
(`SA`, `HT`). The highest trump wins a trick, else the highest card of the suit
led, and the winner leads to the next trick. The board in the live view is a
compass with the trick in play in the middle, and the move list marks each trick
with its number and leader (`T3 W:`). The status line names the trick the board
header shows (`trick 12 of 13`), not a move per side, since the four seats do not
alternate as two sides (`progress`).

- **Decision:** every bridge turn is move-only (`move_only` is always true,
  `turn_limit` false): there is no resigning, draw offer or claim. The general rule
  that the game master plays a move-only turn with one legal move (see
  [opening](turns.md#opening)) then plays every forced card, singletons and the
  last trick, at no call, time or tokens. It also suits decision models, which
  cannot be asked a one-option question.
- **Decision:** a forfeit gives the rest to the opponents. A third rejected answer
  (see [validation](turns.md#validation-and-retries)) ends the table, and the
  opponents take every trick not yet complete (`forfeit`). The result follows from
  the tricks, so points stay comparable and a contract already made stays made.

## Result

After 52 cards the table is a win for North-South when the contract is made
(reason `made`) and a loss when it goes down (`down`); there are no draws, so the
win and loss statistics apply unchanged. `Outcome.points` are the tricks each pair
took, `(ns_tricks, ew_tricks)`, which statistics sum per player (see
[statistics](results.md#statistics), which owns the duplicate arithmetic and the
board table that compares the two tables of each deal).

Each deal is played at two tables with the pairs swapped: games 2k-1 and 2k share
a start, by the rule in [sides alternate](turns.md#turn-loop), and the bridge
prompt tells the models so (see [prompts](prompts.md#prompts)). A competition therefore plays an even number of games
(`Game.duplicate`, same rule).

- **Decision:** the result is counted in tricks, not IMPs (International Match
  Points, the duplicate scoring scale). With a fixed contract, tricks are the most
  sensitive unit; IMPs only add thresholds.

## Parsing

`parse_move` starts with `strip_answer` (see
[validation](turns.md#validation-and-retries)) and reads a card in either order,
any case and many spellings: the id (`SA`, `AS`), suit symbols (`♠A`, `A♠`, also
the outlined `♤` and the emoji variant), `10` or `T`, and words (`spades A`, `ace
of spades`, `ten of hearts`). A rejection says why:

- `'the ace' is not a card; name one like ♠A or ♥10`
- `♠3 is not in your hand` (on dummy's turn: `dummy's hand`)
- `you must follow ♥: you hold ♥K, ♥4` (on dummy's turn: `dummy holds`)

The prompts held up with haiku-5-5 and sonnet-5-5 in the first live check: over
four tables every one of 164 calls gave a legal card on the first attempt, so the
prompts went unchanged. The
[live check snapshot](../research/2026-11-23_bridge-live-check.md) records the
results, the cost per table and the duplicate comparison; the
[visual check snapshot](../research/2026-11-23_bridge-visual-check.md) records the
live view rendering a table with real models.

## Record

Each table is saved as `game-NNN.pbn`, in PBN (Portable Bridge Notation) 2.1:

- the 15 mandatory tags in the export order: `Event` (`oLLMpiada`), `Site` (`?`),
  `Date` (`????.??.??`), `Board`, `West`, `North`, `East`, `South` (the model
  playing each seat), `Dealer` (`N`), `Vulnerable` (`None`), `Deal` (`N:` and the
  hands clockwise from North), `Scoring` (empty), `Declarer` (`S`), `Contract`
  (`3NT`, `4H`) and `Result` (the declarer's tricks);
- the `Play` section from the opening leader (`[Play "W"]`), one trick a line in
  the fixed seat order W N E S, `-` for a seat that did not play, and `*` after a
  table that stopped before trick 13 (a forfeit or an abort).

`Board` is the game's start number (see [sides alternate](turns.md#turn-loop)), so
both tables of a deal carry the same board number and the same `Deal`. An aborted
table (a transport failure) has no `Result`.

- **Decision:** the record is PBN in import format, not strict export format: lines
  end in LF and the file is UTF-8, as for every other file of a run. The tags that
  have no value here take PBN's own notation: `?` for unknown (`Site`, and
  `????.??.??` for `Date`) and an empty string for not applicable (`Scoring`,
  since a table is compared by tricks, not scored). Bridge software that reads PBN
  then finds every mandatory tag it expects.
