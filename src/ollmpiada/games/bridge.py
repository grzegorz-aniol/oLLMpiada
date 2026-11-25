"""Contract bridge card play: a contract fixed by a rule from the deal, no bidding."""

import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rich.align import Align
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ollmpiada.games.base import (
    EVENT,
    Game,
    IllegalMove,
    Outcome,
    PromptContext,
    Questions,
    Rejection,
    Side,
    strip_answer,
)

SUITS = "SHDC"
"""Suit letters in display order, which is also the strain tie-break: majors first, then rank."""
RANKS = "AKQJT98765432"
"""Ranks from high to low; `T` is the ten."""
NO_TRUMPS = "NT"
SYMBOLS = dict(zip(SUITS, "♠♥♦♣", strict=True))
RED_SUITS = "HD"
HCP = {"A": 4, "K": 3, "Q": 2, "J": 1}
FIT = 8
"""The fewest cards of a suit the declaring pair needs to play it as trumps."""
LEVELS = [(37, 7, 7), (33, 6, 6), (30, 4, 5), (25, 3, 4), (23, 2, 3), (20, 1, 2)]
"""`(fewest pair HCP, no-trump level, suit level)`, highest first; the declaring pair always
holds more than 20 HCP, so the last row catches every other hand."""
BOOK = 6
"""Tricks the declarer takes before the contract level counts."""
DECK_SIZE = 52
HAND_SIZE = 13
BOARD_COLUMN = 16
"""The narrowest column of the board: `South (declarer)` and most hands fit, so the board keeps
its width while cards leave the hands and the trick in the middle changes."""
HERE = Path(__file__).parent
# Answers name a card in many ways (`A♠`, `10♠`, `ace of spades`); each piece maps to a
# suit or rank letter, so what is left is the two letters in either order.
CARD_WORDS = re.compile(r"(ace|king|queen|jack|ten|spade|heart|diamond|club)s?|of|10|\s+")
WORD_LETTERS = {"ace": "A", "king": "K", "queen": "Q", "jack": "J", "ten": "T", "10": "T"}
WORD_LETTERS |= {"spade": "S", "heart": "H", "diamond": "D", "club": "C"}
SYMBOL_LETTERS = str.maketrans({"♤": "S", "♡": "H", "♢": "D", "♧": "C", "\ufe0f": None})
SYMBOL_LETTERS |= str.maketrans({symbol: suit for suit, symbol in SYMBOLS.items()})
ANSWER_FORMAT = """ANSWER FORMAT

Answer with one field: move (the card you play, exactly as listed under LEGAL CARDS).
"""
QUESTIONS = Questions(
    move="Which card do you play?",
    piece="Which group of cards do you play from? Each option lists its cards.",
)
REJECTIONS = {
    Rejection.MOVE_ONLY: "bridge has no resigning or draw offers; play a card",
    Rejection.NO_DRAW_OFFER: "bridge has no draw offers; play a card",
    Rejection.NO_MOVE: "the answer names no card; play a card",
}


class Seat(StrEnum):
    N = "N"
    E = "E"
    S = "S"
    W = "W"

    def turned(self, steps: int) -> Seat:
        """The seat `steps` places clockwise from this one."""
        seats = list(Seat)
        return seats[(seats.index(self) + steps) % len(seats)]

    @property
    def next(self) -> Seat:
        return self.turned(1)

    @property
    def side(self) -> Side:
        """North-South is the declaring side `FIRST`, East-West the defending `SECOND`."""
        return Side.FIRST if self in (Seat.N, Seat.S) else Side.SECOND


SEAT_NAMES = {Seat.N: "North", Seat.E: "East", Seat.S: "South", Seat.W: "West"}
DECLARER = Seat.S
DUMMY = Seat.N
OPENING_LEADER = DECLARER.next


def _rank(rank: str) -> str:
    return "10" if rank == "T" else rank


def label(card: str) -> str:
    """`HT` -> `♥10`."""
    suit, rank = card
    return SYMBOLS[suit] + _rank(rank)


def _order(card: str) -> tuple[int, int]:
    return SUITS.index(card[0]), RANKS.index(card[1])


def hcp(cards: Iterable[str]) -> int:
    """High-card points: A=4, K=3, Q=2, J=1."""
    return sum(HCP.get(card[1], 0) for card in cards)


@dataclass(frozen=True)
class Contract:
    level: int
    strain: str
    """A suit letter, or `NO_TRUMPS`."""

    @property
    def trump(self) -> str | None:
        return None if self.strain == NO_TRUMPS else self.strain

    @property
    def tricks_needed(self) -> int:
        return BOOK + self.level

    def __str__(self) -> str:
        return f"{self.level}{NO_TRUMPS if self.trump is None else SYMBOLS[self.strain]}"


@dataclass(frozen=True)
class Trick:
    leader: Seat
    cards: tuple[str, ...]
    winner: Seat | None
    """None while the trick is incomplete."""

    def by_seat(self) -> dict[Seat, str]:
        """Each card with the seat that played it, in play order."""
        return {self.leader.turned(i): card for i, card in enumerate(self.cards)}


@dataclass(frozen=True)
class BridgeState:
    """One table: the hands as dealt (declarer South), the contract and the cards played.
    Everything else (whose turn, the tricks, the cards still held) follows from `played`."""

    hands: Mapping[Seat, tuple[str, ...]]
    contract: Contract
    played: tuple[str, ...] = ()

    @property
    def tricks(self) -> list[Trick]:
        """Every trick started so far, in play order; only the last one can be incomplete."""
        tricks = []
        leader = OPENING_LEADER
        for start in range(0, len(self.played), len(Seat)):
            cards = self.played[start : start + len(Seat)]
            if len(cards) < len(Seat):
                tricks.append(Trick(leader, cards, None))
                break
            tricks.append(Trick(leader, cards, _winner(leader, cards, self.contract.trump)))
            leader = tricks[-1].winner
        return tricks

    @property
    def to_play(self) -> Seat:
        """The seat whose card comes next: the opening leader, the winner of the last
        complete trick, or the next seat round the current trick."""
        if not self.played:
            return OPENING_LEADER
        last = self.tricks[-1]
        return last.winner or last.leader.turned(len(last.cards))

    @property
    def taken(self) -> tuple[int, int]:
        """Complete tricks won by North-South and by East-West."""
        winners = [trick.winner for trick in self.tricks if trick.winner is not None]
        ns = sum(winner.side is Side.FIRST for winner in winners)
        return ns, len(winners) - ns

    def held(self, seat: Seat) -> tuple[str, ...]:
        """The cards `seat` has not played yet, in display order."""
        return tuple(card for card in self.hands[seat] if card not in self.played)


def _winner(leader: Seat, cards: tuple[str, ...], trump: str | None) -> Seat:
    """The highest trump wins a trick, else the highest card of the suit led."""
    led = cards[0][0]
    best = max(
        range(len(cards)),
        key=lambda i: (cards[i][0] == trump, cards[i][0] == led, -RANKS.index(cards[i][1])),
    )
    return leader.turned(best)


def from_deal(hands: Mapping[Seat, Iterable[str]]) -> BridgeState | None:
    """The table for four dealt hands: the contract by a fixed rule, turned so the declarer
    sits South. None when both pairs hold 20 HCP and no pair declares.

    The declaring pair holds more HCP. It plays its longest suit of at least `FIT` cards
    (ties by `SUITS` order), else no trumps, at the level `LEVELS` gives for its HCP. Its
    declarer holds more trumps, then more HCP (in no trumps only more HCP); a full tie goes
    to North or East.
    """
    cards = {seat: tuple(sorted(hand, key=_order)) for seat, hand in hands.items()}
    pairs = [(Seat.N, Seat.S), (Seat.E, Seat.W)]
    points = [hcp(cards[a] + cards[b]) for a, b in pairs]
    if points[0] == points[1]:
        return None
    pair = pairs[0] if points[0] > points[1] else pairs[1]
    pair_hcp = max(points)
    pair_cards = cards[pair[0]] + cards[pair[1]]
    lengths = {suit: sum(card[0] == suit for card in pair_cards) for suit in SUITS}
    longest = max(SUITS, key=lambda suit: lengths[suit])
    strain = longest if lengths[longest] >= FIT else NO_TRUMPS
    _, nt_level, suit_level = next(row for row in LEVELS if pair_hcp >= row[0])
    contract = Contract(nt_level if strain == NO_TRUMPS else suit_level, strain)
    declarer = max(
        pair,
        key=lambda seat: (sum(card[0] == contract.trump for card in cards[seat]), hcp(cards[seat])),
    )
    steps = list(Seat).index(DECLARER) - list(Seat).index(declarer)
    return BridgeState({seat: cards[seat.turned(-steps)] for seat in Seat}, contract)


def _result(contract: Contract, tricks: tuple[int, int], reason: str | None = None) -> Outcome:
    """A win for North-South when its `tricks` make the contract, with the tricks as points;
    the reason is `made` or `down` unless the hand ended off the table for `reason`."""
    made = tricks[0] >= contract.tricks_needed
    return Outcome(
        Side.FIRST if made else Side.SECOND, reason or ("made" if made else "down"), points=tricks
    )


def _card(text: str) -> str | None:
    """The card id an answer names, in any order, case and spelling, or None."""
    letters = CARD_WORDS.sub(
        lambda m: WORD_LETTERS.get(m[1] or m[0], ""), strip_answer(text).lower()
    )
    letters = letters.translate(SYMBOL_LETTERS).upper()
    if len(letters) != 2:
        return None
    for suit, rank in (letters, letters[::-1]):
        if suit in SUITS and rank in RANKS:
            return suit + rank
    return None


def _ranks(cards: Iterable[str], suit: str) -> str:
    """`K 10 4`: the ranks of `suit` among `cards`, or `-` for a void."""
    return " ".join(_rank(card[1]) for card in cards if card[0] == suit) or "-"


def _hand(title: str, cards: tuple[str, ...]) -> list[str]:
    """A hand by suit, one line each: `  ♥ K 10 4`."""
    return [f"{title}:", *(f"  {SYMBOLS[suit]} {_ranks(cards, suit)}" for suit in SUITS)]


def _played(trick: Trick) -> str:
    """`West ♠5, North ♠A`: the cards of a trick in play order, each with its seat."""
    return ", ".join(f"{SEAT_NAMES[seat]} {label(card)}" for seat, card in trick.by_seat().items())


def _tricks(state: BridgeState) -> list[str]:
    """The completed tricks, numbered from 1, then the trick in play."""
    tricks = state.tricks
    current = tricks.pop() if tricks and tricks[-1].winner is None else None
    lines = ["Completed tricks, each from its lead:" if tricks else "Completed tricks: none."]
    lines += [
        f"  {n}. {_played(trick)}; won by {SEAT_NAMES[trick.winner]}"
        for n, trick in enumerate(tricks, 1)
    ]
    number = len(tricks) + 1
    if current is None:
        lines.append(f"Trick {number}: {SEAT_NAMES[state.to_play]} leads.")
    else:
        lines.append(f"Trick {number} so far: {_played(current)}.")
    return lines


class BridgeGame(Game[BridgeState]):
    """Card play of one deal: North-South (`FIRST`) declares, East-West (`SECOND`) defends,
    and one model plays both seats of its pair."""

    name = "bridge"
    side_names = ("North-South", "East-West")
    system_template = (HERE / "bridge_system_prompt.txt").read_text(encoding="utf-8")
    user_template = (HERE / "bridge_user_prompt.txt").read_text(encoding="utf-8")
    record_extension = "pbn"
    questions = QUESTIONS
    turn_limit = False
    duplicate = True

    def new_state(self, seed: int) -> BridgeState:
        rng = random.Random(seed)
        deck = [suit + rank for suit in SUITS for rank in RANKS]
        while True:
            rng.shuffle(deck)
            hands = {seat: deck[i * HAND_SIZE : (i + 1) * HAND_SIZE] for i, seat in enumerate(Seat)}
            if (state := from_deal(hands)) is not None:
                return state

    def side_to_move(self, state: BridgeState) -> Side:
        return state.to_play.side

    def legal_moves(self, state: BridgeState) -> dict[str, str]:
        """The cards of the seat to play, only the suit led when it holds any."""
        held = state.held(state.to_play)
        trick = state.tricks[-1] if state.tricks else None
        if trick is not None and trick.winner is None:
            following = [card for card in held if card[0] == trick.cards[0][0]]
            held = tuple(following) or held
        return {card: label(card) for card in held}

    def parse_move(self, state: BridgeState, text: str) -> str:
        card = _card(text)
        if card is None:
            raise IllegalMove(f"{text!r} is not a card; name one like ♠A or ♥10")
        legal = self.legal_moves(state)
        if card in legal:
            return card
        seat = state.to_play
        hand, hold = ("dummy's hand", "dummy holds") if seat is DUMMY else ("your hand", "you hold")
        if card not in state.held(seat):
            raise IllegalMove(f"{label(card)} is not in {hand}")
        led = SYMBOLS[next(iter(legal))[0]]
        raise IllegalMove(f"you must follow {led}: {hold} {', '.join(legal.values())}")

    def apply(self, state: BridgeState, move_id: str) -> BridgeState:
        return BridgeState(state.hands, state.contract, (*state.played, move_id))

    def outcome(self, state: BridgeState) -> Outcome | None:
        if len(state.played) < DECK_SIZE:
            return None
        return _result(state.contract, state.taken)

    def forfeit(self, state: BridgeState, side: Side, reason: str) -> Outcome:
        """The opponents of `side` take every trick not yet complete; the result follows from
        the tricks, so a contract already made stays made."""
        ns, ew = state.taken
        rest = HAND_SIZE - ns - ew
        tricks = (ns, ew + rest) if side is Side.FIRST else (ns + rest, ew)
        return _result(state.contract, tricks, reason)

    def move_only(self, turn: int) -> bool:
        return True

    def move_mark(self, state: BridgeState) -> str | None:
        """`T3 W:` before the lead of each trick: the trick number and its leader."""
        if len(state.played) % len(Seat):
            return None
        return f"T{len(state.played) // len(Seat) + 1} {state.to_play}:"

    def progress(self, state: BridgeState, turn: int) -> str:
        """The trick the board shows, since the four seats do not alternate as two sides."""
        return f"trick {max(len(state.tricks), 1)} of {HAND_SIZE}"

    def rejection(self, rule: Rejection) -> str:
        return REJECTIONS[rule]

    def prompt_values(self, ctx: PromptContext) -> dict[str, object]:
        """The retry sentence; the templates word the rest, which never varies by turn."""
        return dict(
            previous_attempt=(
                f"PREVIOUS ATTEMPT\n\nYour previous answer this turn was rejected: "
                f"{ctx.rejection}\nAnswer again with a legal card."
                if ctx.rejection
                else ""
            )
        )

    def answer_format(self, ctx: PromptContext) -> str:
        """One format for every turn, since every bridge turn is move-only."""
        return ANSWER_FORMAT

    def describe(self, state: BridgeState) -> str:
        """The view of the seat to play: the contract, its own hand, dummy once the opening
        lead is made and every card played, never a hidden hand (docs/game-master/prompts.md)."""
        contract, seat = state.contract, state.to_play
        needed = contract.tricks_needed
        ns, ew = state.taken
        trumps = "no trumps" if contract.trump is None else f"trumps {SYMBOLS[contract.trump]}"
        if seat is DUMMY:
            role = "You are South, the declarer, playing from dummy (North): play one of its cards."
        elif seat is DECLARER:
            role = "You are South, the declarer: play a card from your own hand."
        else:
            partner = SEAT_NAMES[seat.turned(2)]
            role = f"You are {SEAT_NAMES[seat]}, a defender with {partner}: play from your hand."
        own = DECLARER if seat is DUMMY else seat
        lines = [
            f"Contract: {contract} by South, {trumps}.",
            f"North-South need {needed} tricks to make it; East-West need "
            f"{HAND_SIZE - needed + 1} to beat it.",
            f"Tricks won: North-South {ns}, East-West {ew}.",
            "",
            role,
            "",
            *_hand(f"{SEAT_NAMES[own]}, your hand", state.held(own)),
            "",
            *(
                _hand("North, dummy", state.held(DUMMY))
                if state.played
                else ["North, dummy: faced after the opening lead."]
            ),
            "",
            *_tricks(state),
        ]
        return "\n".join(lines)

    def render(self, state: BridgeState) -> Group:
        """Every hand as it stands, round a compass with the latest trick in the middle, under
        a line with the contract, the trick number and the tricks taken."""
        contract, tricks = state.contract, state.tricks
        ns, ew = state.taken
        header = Text.assemble(
            "Contract ",
            (str(contract), _style(contract.strain)),
            f" by {SEAT_NAMES[DECLARER]} | {self.progress(state, len(state.played))}"
            f" | tricks NS {ns}, EW {ew}",
        )
        trick = tricks[-1].by_seat() if tricks else {}
        # The gap keeps West's and East's cards apart when nobody fills the middle.
        middle = _compass({seat: _card_text(card) for seat, card in trick.items()}, Text(" " * 4))
        hands = {seat: _hand_text(state, seat) for seat in Seat}
        return Group(header, _compass(hands, Align.center(middle), min_width=BOARD_COLUMN))

    def record(
        self,
        state: BridgeState,
        outcome: Outcome | None,
        players: tuple[str, str],
        start: int,
    ) -> str:
        """The hand in PBN (Portable Bridge Notation) 2.1: the mandatory tags in the export
        order, then the play, one trick a line in the fixed seat order from the opening leader.
        A hand that stopped early ends its play with `*`; an aborted one has no `Result`
        (docs/game-master/bridge.md#record)."""
        north_south, east_west = players
        tags = {
            "Event": EVENT,
            "Site": "?",
            "Date": "????.??.??",
            "Board": start,
            "West": east_west,
            "North": north_south,
            "East": east_west,
            "South": north_south,
            "Dealer": Seat.N,
            "Vulnerable": "None",
            "Deal": f"{Seat.N}:" + " ".join(_pbn_hand(state.hands[seat]) for seat in Seat),
            # Empty is PBN's "not applicable": the hand is compared by tricks, not scored.
            "Scoring": "",
            "Declarer": DECLARER,
            "Contract": f"{state.contract.level}{state.contract.strain}",
        }
        if outcome is not None and outcome.points is not None:
            tags["Result"] = outcome.points[0]
        lines = [f'[{name} "{value}"]' for name, value in tags.items()]
        lines.append(f'[Play "{OPENING_LEADER}"]')
        columns = [OPENING_LEADER.turned(i) for i in range(len(Seat))]
        for trick in state.tricks:
            cards = trick.by_seat()
            lines.append(" ".join(cards.get(seat, "-") for seat in columns))
        if len(state.played) < DECK_SIZE:
            lines.append("*")
        return "\n".join(lines) + "\n"


def _style(suit: str) -> str:
    return "red" if suit in RED_SUITS else ""


def _card_text(card: str) -> Text:
    return Text(label(card), style=_style(card[0]))


def _hand_text(state: BridgeState, seat: Seat) -> Text:
    """The cards `seat` still holds by suit under its name, which is bold when it plays next."""
    role = {DECLARER: " (declarer)", DUMMY: " (dummy)"}.get(seat, "")
    text = Text(justify="left")
    text.append(SEAT_NAMES[seat] + role, style="bold" if seat is state.to_play else "dim")
    held = state.held(seat)
    for suit in SUITS:
        text.append("\n")
        text.append(SYMBOLS[suit], style=_style(suit))
        text.append(f" {_ranks(held, suit)}")
    return text


def _compass(
    at: Mapping[Seat, RenderableType], middle: RenderableType, min_width: int | None = None
) -> Table:
    """`at` laid out round `middle`: North on top, West left, East right, South at the bottom."""
    grid = Table.grid(padding=(0, 1))
    for _ in range(3):
        grid.add_column(justify="center", vertical="middle", min_width=min_width)
    grid.add_row(Text(), at.get(Seat.N, Text()), Text())
    grid.add_row(at.get(Seat.W, Text()), middle, at.get(Seat.E, Text()))
    grid.add_row(Text(), at.get(Seat.S, Text()), Text())
    return grid


def _pbn_hand(cards: tuple[str, ...]) -> str:
    """`AK2.Q..JT`: spades, hearts, diamonds and clubs, high to low, `T` for the ten."""
    return ".".join("".join(card[1] for card in cards if card[0] == suit) for suit in SUITS)
