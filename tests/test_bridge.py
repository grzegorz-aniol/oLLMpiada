import asyncio
import dataclasses
import json
import re

import pytest
from rich.console import Console

from ollmpiada.competition import Competition, CompetitionSetupError, prompt_version
from ollmpiada.game_master import GameStarted, TurnBasedMaster
from ollmpiada.games.base import SAMPLE_SEED, IllegalMove, Outcome, Rejection, Side
from ollmpiada.games.bridge import (
    RANKS,
    SUITS,
    BridgeGame,
    BridgeState,
    Contract,
    Seat,
    from_deal,
    hcp,
    label,
)
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players.base import TurnAnswer
from ollmpiada.players.llm import llm_message
from ollmpiada.tui import LiveView
from tests.fakes import PickingPlayer, ScriptedPlayer

game = BridgeGame()
DECK = sorted(suit + rank for suit in SUITS for rank in RANKS)


def hand(text: str) -> tuple[str, ...]:
    """`AK2.Q..JT` (spades.hearts.diamonds.clubs, as in PBN) -> `SA SK S2 HQ CJ CT`."""
    suits = text.split(".")
    return tuple(suit + rank for suit, ranks in zip(SUITS, suits, strict=True) for rank in ranks)


def deal(n: str, e: str, s: str, w: str) -> dict[Seat, tuple[str, ...]]:
    hands = {Seat.N: hand(n), Seat.E: hand(e), Seat.S: hand(s), Seat.W: hand(w)}
    assert sorted(card for cards in hands.values() for card in cards) == DECK
    assert all(len(cards) == 13 for cards in hands.values())
    return hands


# North-South 25 HCP (North 13, South 12) and no suit of 8 cards.
NO_FIT = deal("AK32.K32.K32.432", "JT9.JT9.QJT.AKQJ", "Q54.AQ54.A54.765", "876.876.9876.T98")
# North-South 26 HCP with 8 hearts and 8 diamonds; North holds 5 hearts and fewer HCP.
HEART_FIT = deal("32.T9876.AK32.32", "QJT9.J54.876.AKQ", "AK4.AKQ.QJT9.654", "8765.32.54.JT987")
# North-South 24 HCP with 9 clubs (North 5) and 8 spades.
CLUB_FIT = deal("AK32.32.32.AKQ32", "T98.AKQJT.KQJ.76", "QJ54.54.A54.JT98", "76.9876.T9876.54")
# North-South 27 HCP with 8 spades and 8 diamonds, each 4-4; South holds every point.
EVEN_TRUMPS = deal("5432.32.5432.432", "T98.AKQJ.T98.QJT", "AKQJ.654.AKQJ.AK", "76.T987.76.98765")
# Every seat holds one whole suit, so every pair has 20 HCP.
ONE_SUIT_EACH = deal("AKQJT98765432...", "..AKQJT98765432.", ".AKQJT98765432..", "...AKQJT98765432")
# A hand to play with hearts as trumps: North and East hold one spade each, South no diamond.
PLAY = deal("2.T92.87654.5432", "3.873.QJT92.9876", "654.AKQJ6..AKQJT", "AKQJT987.54.AK3.")


def play(state: BridgeState, *answers: str) -> BridgeState:
    for answer in answers:
        state = game.apply(state, game.parse_move(state, answer))
    return state


def play_first_cards(state: BridgeState, count: int) -> BridgeState:
    """Play the first legal card of each seat until `count` cards are on the table."""
    while len(state.played) < count:
        state = game.apply(state, next(iter(game.legal_moves(state))))
    return state


# Deal


def test_a_deal_is_deterministic_for_a_seed():
    assert game.new_state(42) == game.new_state(42)
    assert game.new_state(42).hands != game.new_state(43).hands


def test_calls_outside_a_game_lead_from_the_sample_deal():
    request = TurnBasedMaster(game).start_request(max_turns=None)
    assert request.legal_moves == game.legal_moves(game.new_state(SAMPLE_SEED))
    assert request.legal_moves != game.legal_moves(game.new_state(SAMPLE_SEED + 1))


def test_a_deal_without_a_declaring_pair_is_dealt_again(monkeypatch):
    deals = []

    def tie_first(hands):
        deals.append({seat: tuple(cards) for seat, cards in hands.items()})
        return None if len(deals) == 1 else from_deal(hands)

    monkeypatch.setattr("ollmpiada.games.bridge.from_deal", tie_first)
    state = game.new_state(7)
    assert len(deals) == 2 and deals[0] != deals[1]
    assert state == from_deal(deals[1])
    deals.clear()
    assert game.new_state(7) == state


@pytest.mark.parametrize("seed", range(200))
def test_every_deal_has_north_south_declaring_from_south(seed):
    state = game.new_state(seed)
    assert sorted(card for cards in state.hands.values() for card in cards) == DECK
    assert all(len(cards) == 13 for cards in state.hands.values())
    assert hcp(state.hands[Seat.N] + state.hands[Seat.S]) > 20
    trump = state.contract.trump
    south, north = (
        (sum(card[0] == trump for card in state.hands[seat]), hcp(state.hands[seat]))
        for seat in (Seat.S, Seat.N)
    )
    assert south >= north if trump else south[1] >= north[1]
    assert state.played == ()


# Contract rule


def test_no_eight_card_fit_plays_no_trumps_from_the_stronger_hand():
    state = from_deal(NO_FIT)
    assert state.contract == Contract(3, "NT")
    assert str(state.contract) == "3NT"
    # North held more HCP, so the table turns half round: North's hand is South's now.
    assert state.hands == {
        Seat.S: NO_FIT[Seat.N],
        Seat.N: NO_FIT[Seat.S],
        Seat.W: NO_FIT[Seat.E],
        Seat.E: NO_FIT[Seat.W],
    }


def test_a_major_fit_beats_an_equal_minor_and_more_trumps_beat_more_hcp():
    state = from_deal(HEART_FIT)
    assert state.contract == Contract(4, "H")
    assert str(state.contract) == "4♥"
    assert state.hands[Seat.S] == HEART_FIT[Seat.N]


def test_the_longest_fit_wins_over_a_shorter_major():
    assert from_deal(CLUB_FIT).contract == Contract(3, "C")


def test_equal_trumps_go_to_the_hand_with_more_hcp():
    state = from_deal(EVEN_TRUMPS)
    assert state.contract == Contract(4, "S")
    assert state.hands == EVEN_TRUMPS


def test_twenty_hcp_each_gives_no_table():
    assert from_deal(ONE_SUIT_EACH) is None


@pytest.mark.parametrize("steps", range(4))
def test_the_declarer_ends_up_south_wherever_the_deal_seated_them(steps):
    turned = {seat.turned(steps): cards for seat, cards in CLUB_FIT.items()}
    state = from_deal(turned)
    assert state == from_deal(CLUB_FIT)
    assert list(state.hands) == list(Seat)


# Suit lengths per seat in suit order: North-South hold 7-7-6-6 (no fit) or 8 spades.
FLAT = {Seat.N: (4, 3, 3, 3), Seat.E: (3, 3, 4, 3), Seat.S: (3, 4, 3, 3), Seat.W: (3, 3, 3, 4)}
SPADE_FIT = {Seat.N: (5, 3, 3, 2), Seat.E: (3, 3, 3, 4), Seat.S: (3, 4, 3, 3), Seat.W: (2, 3, 4, 4)}


def deal_with(pair_hcp: int, shapes: dict[Seat, tuple[int, ...]]) -> dict[Seat, list[str]]:
    """A deal in which North-South hold `pair_hcp` HCP and every hand has its `shapes`."""
    hands: dict[Seat, list[str]] = {seat: [] for seat in Seat}
    left = pair_hcp
    for i, suit in enumerate(SUITS):
        ns = []
        for card in (suit + rank for rank in "AKQJ"):
            if hcp([card]) <= left:
                ns.append(card)
                left -= hcp([card])
        spots = [suit + rank for rank in RANKS[4:]]
        ns += spots[: shapes[Seat.N][i] + shapes[Seat.S][i] - len(ns)]
        ew = [suit + rank for rank in RANKS if suit + rank not in ns]
        hands[Seat.N] += ns[: shapes[Seat.N][i]]
        hands[Seat.S] += ns[shapes[Seat.N][i] :]
        hands[Seat.E] += ew[: shapes[Seat.E][i]]
        hands[Seat.W] += ew[shapes[Seat.E][i] :]
    assert left == 0
    return hands


@pytest.mark.parametrize(
    ("pair_hcp", "nt_level", "suit_level"),
    [(21, 1, 2), (22, 1, 2), (23, 2, 3), (24, 2, 3), (25, 3, 4), (29, 3, 4), (30, 4, 5),
     (32, 4, 5), (33, 6, 6), (36, 6, 6), (37, 7, 7), (40, 7, 7)],
)  # fmt: skip
def test_the_level_follows_the_pair_hcp(pair_hcp, nt_level, suit_level):
    assert from_deal(deal_with(pair_hcp, FLAT)).contract == Contract(nt_level, "NT")
    assert from_deal(deal_with(pair_hcp, SPADE_FIT)).contract == Contract(suit_level, "S")


# Play


def test_side_to_move_follows_the_seat_and_every_turn_is_move_only():
    state = BridgeState(PLAY, Contract(2, "H"))
    assert state.to_play is Seat.W
    assert game.side_to_move(state) is Side.SECOND
    state = play(state, "♠A")
    assert state.to_play is Seat.N  # dummy, played by the declaring side
    assert game.side_to_move(state) is Side.FIRST
    assert game.move_only(1) and game.move_only(40)
    assert not game.turn_limit


def test_legal_moves_are_labelled_by_suit_then_high_to_low():
    state = BridgeState(PLAY, Contract(2, "H"))
    assert list(game.legal_moves(state).items()) == [
        ("SA", "♠A"), ("SK", "♠K"), ("SQ", "♠Q"), ("SJ", "♠J"), ("ST", "♠10"), ("S9", "♠9"),
        ("S8", "♠8"), ("S7", "♠7"), ("H5", "♥5"), ("H4", "♥4"), ("DA", "♦A"), ("DK", "♦K"),
        ("D3", "♦3"),
    ]  # fmt: skip


def test_following_suit_ruffing_and_overruffing_in_trumps():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A")
    assert list(game.legal_moves(state)) == ["S2"]
    state = play(state, "♠2", "♠3", "♠4")
    assert state.tricks[0].winner is Seat.W
    assert state.to_play is Seat.W  # the trick winner leads
    state = play(state, "♠K")
    assert len(game.legal_moves(state)) == 12  # North has no spade left: any card
    state = play(state, "♥2", "♥3")  # North ruffs, East overruffs
    assert list(game.legal_moves(state)) == ["S6", "S5"]
    state = play(state, "♠5")
    assert state.tricks[1].winner is Seat.E
    assert state.to_play is Seat.E
    assert state.taken == (0, 2)
    # A higher card of a suit neither led nor trumps takes nothing.
    state = play(state, "♦2", "♣A", "♦3", "♦4")
    assert state.tricks[2].winner is Seat.N
    assert state.to_play is Seat.N
    assert state.taken == (1, 2)


def test_without_trumps_the_highest_card_of_the_suit_led_wins():
    state = play(BridgeState(PLAY, Contract(2, "NT")), "♠A", "♠2", "♠3", "♠4", "♠K", "♥2", "♥3")
    state = play(state, "♠5")
    assert state.tricks[1].winner is Seat.W
    assert state.to_play is Seat.W
    assert state.taken == (0, 2)


@pytest.mark.parametrize(
    ("contract", "outcome"),
    [
        (Contract(7, "S"), Outcome(Side.FIRST, "made", points=(13, 0))),
        (Contract(1, "NT"), Outcome(Side.SECOND, "down", points=(0, 13))),
    ],
)
def test_the_hand_ends_after_52_cards_made_or_down(contract, outcome):
    # North's spades win every trick as trumps; without trumps West's clubs do.
    state = play_first_cards(BridgeState(ONE_SUIT_EACH, contract), 51)
    assert game.outcome(state) is None
    state = play_first_cards(state, 52)
    assert game.outcome(state) == outcome


def test_a_forfeit_mid_hand_gives_the_opponents_every_trick_left():
    # West and East took a trick each; the third trick is started.
    state = play(
        BridgeState(PLAY, Contract(2, "H")), "♠A", "♠2", "♠3", "♠4", "♠K", "♥2", "♥3", "♠5", "♦2"
    )
    assert game.forfeit(state, Side.FIRST, "illegal answers") == Outcome(
        Side.SECOND, "illegal answers", points=(0, 13)
    )
    assert game.forfeit(state, Side.SECOND, "illegal answers") == Outcome(
        Side.FIRST, "illegal answers", points=(11, 2)
    )


def test_a_contract_already_made_stays_made_when_the_declarer_forfeits():
    state = play_first_cards(BridgeState(ONE_SUIT_EACH, Contract(1, "S")), 28)
    assert state.taken == (7, 0)
    assert game.forfeit(state, Side.FIRST, "illegal answers") == Outcome(
        Side.FIRST, "illegal answers", points=(7, 6)
    )


# Parsing


@pytest.mark.parametrize(
    "text",
    ["SA", "AS", "sa", "as", "♠A", "A♠", "♠a", "♠\ufe0fA", "♤A", "**♠A**.", "`SA`", "spades A",
     "A of spades", "ace of spades", "Ace of Spades", "spade ace"],
)  # fmt: skip
def test_parse_reads_a_card_in_any_order_case_or_spelling(text):
    assert game.parse_move(BridgeState(PLAY, Contract(2, "H")), text) == "SA"


@pytest.mark.parametrize(
    "text", ["ST", "TS", "♠10", "10♠", "♠T", "t♠", "♠ 10", "10 of spades", "ten of spades"]
)
def test_parse_reads_the_ten_as_10_or_t(text):
    assert game.parse_move(BridgeState(PLAY, Contract(2, "H")), text) == "ST"


@pytest.mark.parametrize("text", ["the ace", "S1", "SAK", "♠♥", "", "pass"])
def test_parse_rejects_what_names_no_card(text):
    with pytest.raises(IllegalMove, match="is not a card; name one like ♠A or ♥10"):
        game.parse_move(BridgeState(PLAY, Contract(2, "H")), text)


def test_parse_rejects_a_card_not_in_hand():
    state = BridgeState(PLAY, Contract(2, "H"))
    with pytest.raises(IllegalMove, match="^♥3 is not in your hand$"):
        game.parse_move(state, "♥3")
    state = play(state, "♠A", "♠2", "♠3", "♠4")
    with pytest.raises(IllegalMove, match="^♠A is not in your hand$"):
        game.parse_move(state, "♠A")  # already played


def test_parse_rejects_a_card_that_does_not_follow_suit():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A", "♠2", "♠3", "♠4", "♠K", "♥2", "♥3")
    state = play(state, "♠5", "♦2", "♣A")
    with pytest.raises(IllegalMove, match="^you must follow ♦: you hold ♦A, ♦K, ♦3$"):
        game.parse_move(state, "♠Q")


def test_parse_names_dummy_when_the_declarer_plays_from_it():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A")
    with pytest.raises(IllegalMove, match="^♥A is not in dummy's hand$"):
        game.parse_move(state, "♥A")
    with pytest.raises(IllegalMove, match="^you must follow ♠: dummy holds ♠2$"):
        game.parse_move(state, "♥2")


# View and prompts

LABELS = {label(card): card for card in DECK}
LABEL = re.compile(r"[♠♥♦♣](?:10|[2-9AKQJ])")
SUIT_LINE = re.compile(r"^ +([♠♥♦♣]) ((?:10|[2-9AKQJ])(?: (?:10|[2-9AKQJ]))*)$", re.MULTILINE)


def shown(text: str) -> set[str]:
    """Every card a prompt names: as a label (`♠A`) or in a hand's suit line (`  ♠ A K 10`)."""
    cards = {LABELS[found] for found in LABEL.findall(text)}
    for symbol, ranks in SUIT_LINE.findall(text):
        cards |= {LABELS[symbol + rank] for rank in ranks.split()}
    return cards


def prompt_of(state: BridgeState):
    return game.prompt(game.context(state, turn=1, max_turns=None))


def every_turn(state: BridgeState):
    """Each state of a scripted hand with a card to play: every seat plays its first legal card."""
    while game.outcome(state) is None:
        yield state
        state = game.apply(state, next(iter(game.legal_moves(state))))


@pytest.mark.parametrize("seed", range(3))
def test_every_prompt_shows_exactly_the_cards_the_seat_to_play_may_know(seed):
    turns = list(every_turn(game.new_state(seed)))
    assert len(turns) == 52
    for state in turns:
        seat = state.to_play
        own = Seat.S if seat is Seat.N else seat
        known = set(state.held(own)) | set(state.played)
        if state.played:
            known |= set(state.held(Seat.N))
        hidden = {card for other in Seat for card in state.held(other)} - known
        text = prompt_of(state).text
        assert shown(text) == known, (seat, len(state.played))
        assert not shown(text) & hidden


def test_dummy_is_hidden_until_the_opening_lead():
    state = BridgeState(PLAY, Contract(2, "H"))
    before = game.describe(state)
    assert "North, dummy: faced after the opening lead." in before
    assert not shown(before) & set(PLAY[Seat.N])
    after = game.describe(play(state, "♠A"))
    assert set(PLAY[Seat.N]) <= shown(after)


def test_the_view_names_the_seat_contract_hands_and_tricks():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A", "♠2", "♠3", "♠4", "♠K")
    view = game.describe(state)
    assert "Contract: 2♥ by South, trumps ♥." in view
    assert "North-South need 8 tricks to make it; East-West need 6 to beat it." in view
    assert "Tricks won: North-South 0, East-West 1." in view
    assert "You are South, the declarer, playing from dummy (North)" in view
    assert "South, your hand:\n  ♠ 6 5\n  ♥ A K Q J 6\n  ♦ -\n  ♣ A K Q J 10" in view
    assert "North, dummy:\n  ♠ -\n  ♥ 10 9 2\n" in view
    assert "  1. West ♠A, North ♠2, East ♠3, South ♠4; won by West" in view
    assert view.endswith("Trick 2 so far: West ♠K.")

    defender = game.describe(play(state, "♥2"))
    assert "You are East, a defender with West: play from your hand." in defender
    assert "East, your hand:" in defender and "South, your hand" not in defender
    in_no_trumps = game.describe(BridgeState(PLAY, Contract(3, "NT")))
    assert "Contract: 3NT by South, no trumps." in in_no_trumps
    assert "Completed tricks: none.\nTrick 1: West leads." in in_no_trumps


def test_the_system_part_depends_only_on_the_side_to_play():
    systems = {}
    for state in every_turn(game.new_state(0)):
        systems.setdefault(game.side_to_move(state), set()).add(prompt_of(state).system)
    assert all(len(texts) == 1 for texts in systems.values())


def test_prompts_of_the_two_pairs_differ_only_in_the_pair_names():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A")
    declarer = game.context(state, turn=1, max_turns=None, rejection="♥A is not in dummy's hand")
    defender = dataclasses.replace(declarer, side=Side.SECOND)
    ours, theirs = game.prompt(declarer), game.prompt(defender)
    assert ours.user == theirs.user
    our_words, their_words = (re.findall(r"\w+", p.system) for p in (ours, theirs))
    assert len(our_words) == len(their_words)
    differing = {(a, b) for a, b in zip(our_words, their_words, strict=True) if a != b}
    assert differing == {("North", "East"), ("South", "West"), ("East", "North"), ("West", "South")}


def test_the_prompt_asks_for_a_card_and_never_offers_resigning_or_draws():
    prompt = prompt_of(BridgeState(PLAY, Contract(2, "H")))
    assert "as East-West against another AI model" in prompt.system
    assert "compared by the tricks they take with the same cards" in prompt.system
    assert "The cards you may play now: ♠A, ♠K, ♠Q, ♠J, ♠10, ♠9, ♠8, ♠7, ♥5, ♥4, ♦A, ♦K, ♦3\n" in (
        prompt.user
    )
    assert "Choose exactly one card from this list" in prompt.user
    assert "Before answering, check that your card appears in it, spelled as listed." in prompt.user
    assert not re.search(r"resign|draw offer|\$", prompt.user)


def test_the_llm_answer_format_points_to_a_section_of_the_message():
    prompt = prompt_of(BridgeState(PLAY, Contract(2, "H")))
    named = re.search(r"listed under ([A-Z][A-Z ]+[A-Z])", prompt.answer_format)
    assert named is not None and named[1] == "LEGAL CARDS"
    assert named[1] in re.findall(r"^[A-Z][A-Z ]+$", llm_message(prompt), flags=re.MULTILINE)


def test_no_text_a_bridge_model_reads_speaks_of_moves_or_actions():
    state = play(BridgeState(PLAY, Contract(2, "H")), "♠A")
    prompt = game.prompt(game.context(state, turn=1, max_turns=None, rejection="-"))
    texts = [prompt.system, prompt.user, prompt.questions.move, prompt.questions.piece]
    texts += [game.rejection(rule) for rule in Rejection]
    assert not [t for t in texts if re.search(r"\b(moves?|action)\b", t, re.IGNORECASE)]
    # The answer format names the answer's one field, `move`, and nothing else of moves.
    assert re.findall(r"\b(moves?|action)\b", prompt.answer_format) == ["move"]


def test_rejections_and_the_retry_name_a_card_not_a_move():
    answers = [TurnAnswer(action="resign"), TurnAnswer(action="move", move=""), "pass"]
    north_south = ScriptedPlayer("north-south", [])
    east_west = ScriptedPlayer("east-west", answers)
    result = asyncio.run(
        TurnBasedMaster(game).play((north_south, east_west), max_turns=None, backoff_s=(0,))
    )
    rejections = [attempt.rejection for attempt in result.turns[0].attempts]
    assert rejections == [
        "bridge has no resigning or draw offers; play a card",
        "the answer names no card; play a card",
        "'pass' is not a card; name one like ♠A or ♥10",
    ]
    retry = east_west.requests[1].prompt.user
    assert retry.rstrip().endswith("Answer again with a legal card.")
    assert result.outcome == Outcome(Side.FIRST, "illegal answers", points=(13, 0))


def test_prompt_version_renders_for_bridge():
    version = prompt_version(game)
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert version != prompt_version(ChessGame())


# Board, move list and record

# Two tricks to East-West, then East leads to the third: West wins the first with ♠A, and
# East overruffs North in the second.
TWO_TRICKS = ("♠A", "♠2", "♠3", "♠4", "♠K", "♥2", "♥3", "♠5", "♦2")


def test_the_board_shows_the_contract_the_hands_left_and_the_trick_in_play():
    state = play(BridgeState(PLAY, Contract(2, "H")), *TWO_TRICKS[:6])
    console = Console(record=True, width=100)
    console.print(game.render(state))
    board = console.export_text()

    assert "Contract 2♥ by South | trick 2 of 13 | tricks NS 0, EW 1" in board
    lines = [line.split() for line in board.splitlines()]
    # Played cards are gone from the hands: West led ♠A and ♠K, South played ♠4.
    assert ["West", "East"] in lines
    assert ["♠", "Q", "J", "10", "9", "8", "7", "♥2", "♠", "-"] in lines  # West, North's ♥2, East
    assert ["♥", "5", "4", "♠K", "♥", "8", "7", "3"] in lines  # West, its lead, East
    assert ["South", "(declarer)"] in lines and ["♠", "6", "5"] in lines
    assert ["North", "(dummy)"] in lines and ["♥", "10", "9"] in lines
    red = {
        segment.text.strip()
        for segment in console.render(game.render(state))
        if segment.style is not None
        and segment.style.color is not None
        and segment.style.color.name == "red"
    }
    assert red == {"2♥", "♥", "♦", "♥2"}


def test_the_move_list_marks_each_trick_with_its_number_and_leader():
    state = BridgeState(PLAY, Contract(2, "H"))
    marks = []
    for answer in TWO_TRICKS:
        marks.append(game.move_mark(state))
        state = play(state, answer)
    assert marks == ["T1 W:", None, None, None, "T2 W:", None, None, None, "T3 E:"]


def test_the_live_status_line_counts_tricks_not_moves_per_side(tmp_path):
    view = LiveView(TurnBasedMaster(game), visual=False)
    statuses = []

    def handle_and_render(event):
        view.handle(event)
        console = Console(record=True, width=140)
        console.print(view)
        statuses.append(console.export_text().splitlines()[0])

    competition = Competition(
        view.master,
        (PickingPlayer("model-a", 0), PickingPlayer("model-b", -1)),
        games=2,
        max_turns=None,
        runs_dir=tmp_path,
        seed=2,
        publish=handle_and_render,
        backoff_s=(0,),
    )
    asyncio.run(competition.run())

    # Moves per side would read "move 24" after 46 cards, beside a board at trick 12.
    assert "Games 1 of 2 | game 2, trick 12 of 13 | running" in statuses
    assert "Games 1 of 2 | game 2, trick 13 of 13 | running" in statuses
    assert not any("move" in status for status in statuses)


PBN_TWO_TRICKS_FORFEITED = """\
[Event "oLLMpiada"]
[Site "?"]
[Date "????.??.??"]
[Board "2"]
[West "model-b"]
[North "model-a@high"]
[East "model-b"]
[South "model-a@high"]
[Dealer "N"]
[Vulnerable "None"]
[Deal "N:2.T92.87654.5432 3.873.QJT92.9876 654.AKQJ6..AKQJT AKQJT987.54.AK3."]
[Scoring ""]
[Declarer "S"]
[Contract "2H"]
[Result "0"]
[Play "W"]
SA S2 S3 S4
SK H2 H3 S5
- - D2 -
*
"""


def test_the_record_is_pbn_with_the_play_in_seat_columns_from_the_opening_leader():
    state = play(BridgeState(PLAY, Contract(2, "H")), *TWO_TRICKS)
    outcome = game.forfeit(state, Side.FIRST, "illegal answers")
    assert game.record_extension == "pbn"
    assert game.record(state, outcome, ("model-a@high", "model-b"), 2) == PBN_TWO_TRICKS_FORFEITED


def test_an_aborted_hand_has_no_result_and_a_finished_one_no_end_mark():
    state = play(BridgeState(PLAY, Contract(2, "H")), *TWO_TRICKS)
    aborted = game.record(state, None, ("a", "b"), 1)
    assert "[Result" not in aborted
    assert aborted.endswith('[Play "W"]\nSA S2 S3 S4\nSK H2 H3 S5\n- - D2 -\n*\n')

    state = play_first_cards(BridgeState(ONE_SUIT_EACH, Contract(7, "S")), 52)
    finished = game.record(state, game.outcome(state), ("a", "b"), 1)
    assert '[Contract "7S"]\n[Result "13"]\n[Play "W"]\n' in finished
    play_lines = finished.split('[Play "W"]\n')[1].splitlines()
    assert len(play_lines) == 13 and "*" not in play_lines


# A competition end to end


def test_a_competition_rejects_an_odd_number_of_games(tmp_path):
    players = (PickingPlayer("model-a", 0), PickingPlayer("model-b", -1))
    with pytest.raises(CompetitionSetupError, match="pick an even number of games, not 3"):
        Competition(TurnBasedMaster(game), players, games=3, max_turns=None, runs_dir=tmp_path)


def test_a_duplicate_pair_plays_one_deal_at_two_tables_and_scores_the_trick_difference(tmp_path):
    # Seed 2 deals 3♦. Model-a plays the highest listed card and declares at the first table
    # for 9 tricks; model-b plays the lowest and declares at the second for 7.
    a, b = PickingPlayer("model-a", 0), PickingPlayer("model-b", -1)
    starts = []

    def keep_starts(event):
        if isinstance(event, GameStarted):
            starts.append(event.state)

    competition = Competition(
        TurnBasedMaster(game),
        (a, b),
        games=2,
        max_turns=None,
        runs_dir=tmp_path,
        seed=2,
        publish=keep_starts,
        backoff_s=(0,),
    )
    state = asyncio.run(competition.run())

    assert state.status == "finished"
    assert starts[0] == starts[1] and starts[0].contract == Contract(3, "D")
    assert [r.players for r in state.results] == [("model-a", "model-b"), ("model-b", "model-a")]
    assert [(r.result, r.reason) for r in state.results] == [("9-4", "made"), ("7-6", "down")]
    t1, t2 = 9, 7
    stats_a, stats_b = state.stats["model-a"], state.stats["model-b"]
    assert (stats_a.points, stats_b.points) == (t1 + (13 - t2), (13 - t1) + t2)
    assert stats_a.points - stats_b.points == 2 * (t1 - t2)

    run_dir = competition.run_dir
    first, second = ((run_dir / f"game-00{n}.pbn").read_text() for n in (1, 2))
    assert '[Board "1"]' in first and '[Board "1"]' in second
    assert '[North "model-a"]' in first and '[North "model-b"]' in second
    deal = re.compile(r'^\[Deal ".+"\]$', re.MULTILINE)
    assert len(deal.findall(first)) == 1 and deal.findall(first) == deal.findall(second)
    assert '[Result "9"]' in first and '[Result "7"]' in second

    tables = [json.loads((run_dir / f"game-00{n}.json").read_text())["turns"] for n in (1, 2)]
    assert [len(turns) for turns in tables] == [52, 52]
    # Every seat holds one card in the last trick, so the game master plays it without a call.
    assert all(turn["attempts"] == [] for turns in tables for turn in turns[-4:])
    assert all(len(request.legal_moves) > 1 for request in a.requests + b.requests)
    calls = sum(bool(turn["attempts"]) for turns in tables for turn in turns)
    # Each player's first request is its warm-up, which statistics leave out.
    assert stats_a.calls + stats_b.calls == calls == len(a.requests) + len(b.requests) - 2
