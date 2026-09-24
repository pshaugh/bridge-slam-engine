!pip install -q endplay

import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple

import endplay.dds as dds
from endplay.types import Deal, Denom, Player

# ============================================================================
# 1. CORE DATA TYPES & HAND MODELS
# ============================================================================
SUITS = ["S", "H", "D", "C"]
HONOR_POINTS = {"A": 4, "K": 3, "Q": 2, "J": 1}
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VAL = {r: i for i, r in enumerate(RANKS)}

STRAIN_TO_DENOM = {
    "S": Denom.spades,
    "H": Denom.hearts,
    "D": Denom.diamonds,
    "C": Denom.clubs,
    "NT": Denom.nt,
}


class EngineState(Enum):
    GF_BASE = auto()
    SLAM_PROBE = auto()
    KEYCARD_GATE = auto()
    COMMIT = auto()
    ABORT = auto()


class ContractBlueprint(Enum):
    BLUEPRINT_A_SIDE_SUIT = auto()
    BLUEPRINT_B_CROSS_RUFF = auto()
    BLUEPRINT_C_HIGH_POWER = auto()


@dataclass
class Hand:
    holding: Dict[str, str]

    def length(self, suit: str) -> int:
        return len(self.holding.get(suit, ""))

    def hcp_in_suit(self, suit: str) -> int:
        return sum(HONOR_POINTS.get(card, 0) for card in self.holding.get(suit, ""))

    def total_hcp(self) -> int:
        return sum(self.hcp_in_suit(s) for s in SUITS)

    def pattern(self) -> List[int]:
        return sorted([self.length(s) for s in SUITS], reverse=True)

    def count_losers(self) -> int:
        """
        Standard Bridge Losing Trick Count (LTC):
        Evaluates missing top honors (A, K, Q) in the first 1-3 cards of each suit.
        """
        losers = 0
        for s in SUITS:
            cards = self.holding.get(s, "")
            l = len(cards)
            if l == 0:
                continue
            elif l == 1:
                if "A" not in cards:
                    losers += 1
            elif l == 2:
                top2 = sum(1 for c in ["A", "K"] if c in cards)
                losers += 2 - top2
            else:
                top3 = sum(1 for c in ["A", "K", "Q"] if c in cards)
                losers += 3 - top3
        return losers

    def has_control(self, suit: str, level: int = 1) -> bool:
        cards = self.holding.get(suit, "")
        if level == 1:
            return "A" in cards or len(cards) == 0
        elif level == 2:
            return "K" in cards or len(cards) == 1
        return False

    def count_keycards(self, trump_suit: str) -> int:
        kc = sum(1 for s in SUITS if "A" in self.holding.get(s, ""))
        if trump_suit and trump_suit != "NT" and "K" in self.holding.get(trump_suit, ""):
            kc += 1
        return kc

    def has_trump_queen(self, trump_suit: str) -> bool:
        if not trump_suit or trump_suit == "NT":
            return True
        return "Q" in self.holding.get(trump_suit, "")


def find_best_fit(n_hand: Hand, s_hand: Hand) -> str:
    best_score = -1.0
    best_suit = SUITS[0]

    for s in SUITS:
        comb_len = n_hand.length(s) + s_hand.length(s)
        comb_hcp = n_hand.hcp_in_suit(s) + s_hand.hcp_in_suit(s)
        score = comb_len * 3.0 + comb_hcp * 0.5
        if score > best_score:
            best_score = score
            best_suit = s

    return best_suit


# ============================================================================
# 2. HAZARD DETECTION SIEVE
# ============================================================================
@dataclass
class HazardReport:
    wasted_hcp: int = 0
    cashing_gap_penalty: float = 0.0
    mirror_penalty: float = 0.0
    trump_leak_penalty: float = 0.0
    dpi_total: float = 0.0
    fatal_reason: Optional[str] = None

    @property
    def is_fatal(self) -> bool:
        return self.fatal_reason is not None or self.dpi_total >= 2.5


class HazardSieve:
    @staticmethod
    def evaluate(
        h1: Hand,
        h2: Hand,
        trump_suit: Optional[str],
        splinter_suit: Optional[str],
        bypassed_suits: Set[str],
        blueprint: ContractBlueprint,
    ) -> HazardReport:
        report = HazardReport()

        # Hazard 1a: Bypassed suits missing quick control
        for s in bypassed_suits:
            if not h1.has_control(s, level=1) and not h2.has_control(s, level=1):
                report.cashing_gap_penalty += 3.0
                report.fatal_reason = f"Hazard 1a: Bypassed suit {s} missing 1st-round control"

        # Hazard 1b: Fast Loser Sieve with Tightened Singleton King Check
        if trump_suit and trump_suit != "NT":
            for s in SUITS:
                if s != trump_suit:
                    has_first = h1.has_control(s, level=1) or h2.has_control(s, level=1)
                    has_second = h1.has_control(s, level=2) or h2.has_control(s, level=2)
                    comb_cards = h1.holding.get(s, "") + h2.holding.get(s, "")

                    is_bare_king = (
                        "K" in comb_cards
                        and len(comb_cards) >= 3
                        and "A" not in comb_cards
                        and (h1.length(s) == 1 or h2.length(s) == 1)
                    )

                    if not has_first and (not has_second or is_bare_king):
                        report.cashing_gap_penalty += 4.0
                        report.fatal_reason = f"Hazard 1b: Suit {s} has two fast cashing losers (bare K/no Ace)"

        # Hazard 2: Wasted honors opposite shortage
        if (
            splinter_suit
            and blueprint != ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            and blueprint != ContractBlueprint.BLUEPRINT_C_HIGH_POWER
        ):
            wasted = h1.hcp_in_suit(splinter_suit) + h2.hcp_in_suit(splinter_suit)
            if "A" in h1.holding.get(splinter_suit, "") or "A" in h2.holding.get(splinter_suit, ""):
                wasted -= 4
            wasted = max(0, wasted)
            report.wasted_hcp = wasted
            if wasted >= 4:
                report.fatal_reason = "Hazard 2: Wasted honors opposite shortage"

        # Hazard 3: Trump Fit Quality & Missing Ace Dual-Leak Guard
        if trump_suit and trump_suit != "NT":
            total_trump = h1.length(trump_suit) + h2.length(trump_suit)
            trumps_combined = h1.holding.get(trump_suit, "") + h2.holding.get(trump_suit, "")
            comb_hcp = h1.total_hcp() + h2.total_hcp()
            top_honors = sum(1 for c in ["A", "K", "Q"] if c in trumps_combined)

            if total_trump < 8:
                report.trump_leak_penalty += 3.0
                report.fatal_reason = "Hazard 3: Trump fit under 8 cards"
            elif total_trump == 8:
                if "Q" not in trumps_combined:
                    has_spots = "J" in trumps_combined and "T" in trumps_combined
                    if not (has_spots and comb_hcp >= 32):
                        report.trump_leak_penalty += 3.0
                        report.fatal_reason = "Hazard 3: 8-card fit missing Queen without spots/HCP"
                if "A" not in trumps_combined:
                    is_even_44 = (h1.length(trump_suit) == 4 and h2.length(trump_suit) == 4)
                    if not is_even_44 and comb_hcp < 29:
                        report.trump_leak_penalty += 3.0
                        report.fatal_reason = "Hazard 3: 5-3 trump fit missing Ace under 29 HCP"
            elif total_trump == 9:
                if top_honors < 2:
                    report.trump_leak_penalty += 3.0
                    report.fatal_reason = "Hazard 3: 9-card fit missing two top honors (vulnerable to KQ offside)"

            # Guard: Missing Trump Ace + Outside Missing Honor Leak
            if "A" not in trumps_combined:
                for s in SUITS:
                    if s != trump_suit:
                        side_c = h1.holding.get(s, "") + h2.holding.get(s, "")
                        side_len = h1.length(s) + h2.length(s)
                        if side_len >= 4 and "A" not in side_c and "K" not in side_c:
                            report.trump_leak_penalty += 3.0
                            report.fatal_reason = f"Hazard 3: Missing trump Ace while side suit {s} also has a slow loser"

        # Hazard 4: Mirror / Misfit & Strict A/K NT Stopper Architecture
        shape_symmetry = sum(abs(h1.length(s) - h2.length(s)) for s in SUITS)
        if min(h1.pattern()) >= 2 and min(h2.pattern()) >= 2:
            if blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF:
                report.mirror_penalty = 5.0
                report.fatal_reason = "Hazard 4: Cross-Ruff chosen but hands are flat"
            elif (
                blueprint == ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
                and shape_symmetry <= 4
                and (h1.total_hcp() + h2.total_hcp()) < 34
            ):
                report.mirror_penalty = 1.5
                report.fatal_reason = "Hazard 4: Flat mirror profile under 34 HCP"
            elif blueprint == ContractBlueprint.BLUEPRINT_C_HIGH_POWER:
                for s in SUITS:
                    comb_suit = h1.holding.get(s, "") + h2.holding.get(s, "")
                    has_ace = "A" in comb_suit
                    has_protected_king = "K" in comb_suit and (h1.length(s) >= 2 or h2.length(s) >= 2)
                    if not (has_ace or has_protected_king):
                        report.mirror_penalty += 3.0
                        report.fatal_reason = f"Hazard 4: Suit {s} lacks an A or protected K stopper for 6NT"

        report.dpi_total = (
            report.wasted_hcp
            + report.cashing_gap_penalty
            + report.mirror_penalty
            + report.trump_leak_penalty
        )
        return report


# ============================================================================
# 3. POTENTIAL TRICK VALUE (PTV) ENGINE
# ============================================================================
class PTVCalculator:
    @staticmethod
    def calculate(h1: Hand, h2: Hand, strain: str, blueprint: ContractBlueprint) -> float:
        ptv = 0.0
        total_trump = 0 if strain == "NT" else h1.length(strain) + h2.length(strain)

        if blueprint == ContractBlueprint.BLUEPRINT_A_SIDE_SUIT:
            ptv += max(h1.length(strain), h2.length(strain))
            if total_trump == 10:
                ptv += 1.0
            elif total_trump >= 11:
                ptv += 2.0

            candidates = [s for s in SUITS if s != strain]
            candidates.sort(key=lambda s: h1.length(s) + h2.length(s), reverse=True)
            side = candidates[0]
            side_combined = h1.length(side) + h2.length(side)
            side_cards = h1.holding.get(side, "") + h2.holding.get(side, "")

            short_hand = h2 if h1.length(strain) >= h2.length(strain) else h1
            long_hand = h1 if short_hand == h2 else h2

            is_mirror_side = h1.length(side) == h2.length(side) and side_combined <= 8

            if side_combined >= 5:
                top_honors = sum(1 for c in ["A", "K", "Q"] if c in side_cards)
                dummy_holds_source = short_hand.length(side) > long_hand.length(side)
                dummy_has_outside_entry = any(
                    ("A" in short_hand.holding.get(s, "") or "K" in short_hand.holding.get(s, ""))
                    for s in SUITS if s != side and s != strain
                )

                if is_mirror_side and top_honors < 3:
                    ptv += max(0, top_honors - 1)
                elif dummy_holds_source and not dummy_has_outside_entry and "A" not in short_hand.holding.get(side, ""):
                    ptv += max(0, top_honors - 1)
                elif "A" not in side_cards and "K" not in side_cards:
                    ptv += max(0, top_honors - 1)
                else:
                    ptv += max(top_honors, side_combined - 2)

            for s in candidates:
                if s != side and ("A" in h1.holding.get(s, "") or "A" in h2.holding.get(s, "")):
                    ptv += 1.0

            for s in candidates:
                if short_hand.length(s) <= 1:
                    ptv += 2 - short_hand.length(s)

        elif blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF:
            ptv += total_trump
            for s in SUITS:
                if s != strain:
                    if "A" in h1.holding.get(s, "") or "A" in h2.holding.get(s, ""):
                        ptv += 1.0
                    if "K" in h1.holding.get(s, "") or "K" in h2.holding.get(s, ""):
                        ptv += 0.5
                    for hand in [h1, h2]:
                        if hand.length(s) == 0:
                            ptv += 1.5
                        elif hand.length(s) == 1:
                            ptv += 0.75
            trumps_combined = h1.holding.get(strain, "") + h2.holding.get(strain, "")
            if sum(1 for c in ["A", "K", "Q"] if c in trumps_combined) < 2:
                ptv -= 2.0

        elif blueprint == ContractBlueprint.BLUEPRINT_C_HIGH_POWER:
            comb_hcp = h1.total_hcp() + h2.total_hcp()
            ptv += comb_hcp / 2.7

            for s in SUITS:
                comb_suit = h1.holding.get(s, "") + h2.holding.get(s, "")
                comb_len = h1.length(s) + h2.length(s)
                honors = sum(1 for c in ["A", "K", "Q", "J"] if c in comb_suit)
                if honors >= 3 and comb_len >= 7:
                    ptv += 1.0
                if ("K" in comb_suit or "Q" in comb_suit) and ("T" in comb_suit or "9" in comb_suit):
                    ptv += 0.25
                if comb_len >= 8:
                    ptv += 0.75

        return round(ptv, 2)


# ============================================================================
# 4. STATE MACHINE & AUCTION EXECUTOR
# ============================================================================
@dataclass
class AuctionContext:
    north: Hand
    south: Hand
    agreed_strain: str
    blueprint: ContractBlueprint
    splinter_suit: Optional[str] = None
    bypassed_suits: Set[str] = field(default_factory=set)
    state: EngineState = EngineState.GF_BASE
    final_contract: Optional[str] = None
    log: List[str] = field(default_factory=list)


class SlamEngineStateMachine:
    def __init__(self, ctx: AuctionContext):
        self.ctx = ctx

    def run(self) -> Tuple[EngineState, str]:
        c = self.ctx
        comb_hcp = c.north.total_hcp() + c.south.total_hcp()
        comb_losers = c.north.count_losers() + c.south.count_losers()
        total_trump = (
            0
            if c.agreed_strain == "NT"
            else c.north.length(c.agreed_strain) + c.south.length(c.agreed_strain)
        )
        ptv = PTVCalculator.calculate(c.north, c.south, c.agreed_strain, c.blueprint)
        keycards = c.north.count_keycards(c.agreed_strain) + c.south.count_keycards(c.agreed_strain)

        # 1. High Power NT Constraints
        if c.agreed_strain == "NT":
            max_single_length = max(max(c.north.pattern()), max(c.south.pattern()))
            if comb_losers >= 13:
                c.state = EngineState.ABORT
                c.final_contract = "3NT"
                return (c.state, c.final_contract)
            if max_single_length <= 4 and comb_hcp < 33:
                c.state = EngineState.ABORT
                c.final_contract = "3NT"
                return (c.state, c.final_contract)
            if comb_hcp < 31:
                c.state = EngineState.ABORT
                c.final_contract = "3NT"
                return (c.state, c.final_contract)

        # 2. Strict Losing Trick Count (LTC) Gate for Suit Slams
        if c.agreed_strain != "NT":
            if comb_losers > 12:
                c.state = EngineState.ABORT
                c.final_contract = self._get_game_clamp()
                return (c.state, c.final_contract)

        # 3. 8-Card Minor Fit Safeguard
        if c.agreed_strain in ["C", "D"] and total_trump == 8:
            candidates = [s for s in SUITS if s != c.agreed_strain]
            max_side_len = max(c.north.length(s) + c.south.length(s) for s in candidates)
            if comb_hcp < 29 and max_side_len < 6:
                c.state = EngineState.ABORT
                c.final_contract = "3NT" if min(c.north.pattern()) >= 2 and min(c.south.pattern()) >= 2 else f"5{c.agreed_strain}"
                return (c.state, c.final_contract)

        # Gate 1: Baseline Power with 5-Keycard 11-LTC Override
        if c.agreed_strain != "NT":
            candidates = [s for s in SUITS if s != c.agreed_strain]
            has_running_side = any((c.north.length(s) + c.south.length(s) >= 6) for s in candidates) if candidates else False

            # Check 12-LTC borderline overbids
            if comb_losers == 12 and comb_hcp < 26 and not has_running_side:
                c.state = EngineState.ABORT
                c.final_contract = self._get_game_clamp()
                return (c.state, c.final_contract)

            # 5-Keycard 11-LTC Override: 5 keycards with <= 11 LTC is an ironclad slam
            is_five_kc_cold = (comb_losers <= 11 and total_trump >= 8 and keycards >= 5 and comb_hcp >= 22)
            is_pure_11_ltc = (comb_losers <= 11 and total_trump >= 9 and keycards >= 4 and comb_hcp >= 23)
            passes_power = is_five_kc_cold or is_pure_11_ltc or (comb_hcp >= 23 and ptv >= 11.0)

            if not passes_power:
                c.state = EngineState.ABORT
                c.final_contract = self._get_game_clamp()
                return (c.state, c.final_contract)

        # Gate 2: Hazard Sieve
        c.state = EngineState.SLAM_PROBE
        hazard = HazardSieve.evaluate(
            c.north,
            c.south,
            c.agreed_strain,
            c.splinter_suit,
            c.bypassed_suits,
            c.blueprint,
        )
        if hazard.is_fatal:
            c.state = EngineState.ABORT
            c.final_contract = self._get_game_clamp()
            return (c.state, c.final_contract)

        # Gate 3: Keycard Gate
        c.state = EngineState.KEYCARD_GATE
        trump_q = c.north.has_trump_queen(c.agreed_strain) or c.south.has_trump_queen(c.agreed_strain)

        if total_trump >= 10 or c.blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF or c.agreed_strain == "NT":
            trump_q = True

        min_kc = 4 if c.agreed_strain != "NT" else 3
        if keycards < min_kc:
            c.state = EngineState.ABORT
            c.final_contract = f"5{c.agreed_strain}" if c.agreed_strain != "NT" else "4NT"
            return (c.state, c.final_contract)

        if c.agreed_strain != "NT" and keycards == 4 and not trump_q and total_trump <= 9:
            c.state = EngineState.ABORT
            c.final_contract = f"5{c.agreed_strain}"
            return (c.state, c.final_contract)

        # Gate 4: Commit
        c.state = EngineState.COMMIT
        if keycards >= 5 and trump_q and ptv >= 15.0 and comb_losers <= 10:
            c.final_contract = f"7{c.agreed_strain}"
        else:
            c.final_contract = f"6{c.agreed_strain}"
        return (c.state, c.final_contract)

    def _get_game_clamp(self) -> str:
        s = self.ctx.agreed_strain
        return f"4{s}" if s in ["H", "S"] else ("3NT" if s == "NT" else f"5{s}")


# ============================================================================
# 5. BENCHMARK HARNESS WITH BO HAGLUND DDS
# ============================================================================
def deal_random_board() -> Tuple[Dict[str, Hand], str]:
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)

    hands_dict = {}
    pbn_parts = []
    player_keys = ["N", "E", "S", "W"]

    for i, p in enumerate(player_keys):
        dealt = deck[i * 13 : (i + 1) * 13]
        suit_holdings = {s: [] for s in SUITS}
        for r, s in dealt:
            suit_holdings[s].append(r)

        for s in SUITS:
            suit_holdings[s].sort(key=lambda r: RANK_VAL[r], reverse=True)

        hand_obj = Hand(holding={s: "".join(suit_holdings[s]) for s in SUITS})
        hands_dict[p] = hand_obj

        pbn_hand = ".".join(["".join(suit_holdings[s]) for s in SUITS])
        pbn_parts.append(pbn_hand)

    full_pbn = f"N:{pbn_parts[0]} {pbn_parts[1]} {pbn_parts[2]} {pbn_parts[3]}"
    return hands_dict, full_pbn


def run_benchmark(samples=200):
    stats = {
        "qualified": 0,
        "made_slam": 0,
        "went_down_slam": 0,
        "safely_stopped": 0,
        "missed_cold_slam": 0,
        "A": 0,
        "B": 0,
        "C": 0,
    }

    print("=" * 75)
    print(f" RUNNING DDS BENCHMARK ({samples} Slam-Viable Boards) ")
    print("=" * 75)

    while stats["qualified"] < samples:
        hands, pbn_str = deal_random_board()
        n_hand = hands["N"]
        s_hand = hands["S"]

        if n_hand.total_hcp() + s_hand.total_hcp() < 21:
            continue

        stats["qualified"] += 1

        best_suit = find_best_fit(n_hand, s_hand)

        ptv_a = PTVCalculator.calculate(n_hand, s_hand, best_suit, ContractBlueprint.BLUEPRINT_A_SIDE_SUIT)
        ptv_b = PTVCalculator.calculate(n_hand, s_hand, best_suit, ContractBlueprint.BLUEPRINT_B_CROSS_RUFF)
        ptv_c = PTVCalculator.calculate(n_hand, s_hand, "NT", ContractBlueprint.BLUEPRINT_C_HIGH_POWER)

        # Precise Mutual Shortness: Requires shortness in DISTINCT suits
        short_n = {s for s in SUITS if n_hand.length(s) <= 1}
        short_s = {s for s in SUITS if s_hand.length(s) <= 1}
        has_cross_shortness = len(short_n - short_s) >= 1 and len(short_s - short_n) >= 1
        can_cross_ruff = min(n_hand.length(best_suit), s_hand.length(best_suit)) >= 4

        is_flat = min(n_hand.pattern()) >= 2 and min(s_hand.pattern()) >= 2

        if ptv_c > ptv_a and ptv_c > ptv_b and is_flat:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_C_HIGH_POWER
            strain = "NT"
            stats["C"] += 1
        elif ptv_b > ptv_a and has_cross_shortness and can_cross_ruff:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            strain = best_suit
            stats["B"] += 1
        else:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
            strain = best_suit
            stats["A"] += 1

        ctx = AuctionContext(n_hand, s_hand, strain, chosen_blueprint)
        engine = SlamEngineStateMachine(ctx)
        _, final_bid = engine.run()

        if final_bid == "3NT":
            bid_level = 3
        elif final_bid in ["4S", "4H", "4NT"]:
            bid_level = 4
        elif final_bid in ["5S", "5H", "5D", "5C", "5NT"]:
            bid_level = 5
        else:
            bid_level = int(final_bid[0])

        needed_tricks = bid_level + 6

        deal = Deal(pbn_str)
        denom = STRAIN_TO_DENOM[strain]
        table = dds.calc_dd_table(deal)
        actual_tricks = table[Player.north, denom]

        if bid_level >= 6:
            if actual_tricks >= needed_tricks:
                stats["made_slam"] += 1
            else:
                stats["went_down_slam"] += 1
        else:
            if actual_tricks >= 12:
                stats["missed_cold_slam"] += 1
            else:
                stats["safely_stopped"] += 1

        if stats["qualified"] % 50 == 0:
            print(f" ... solved {stats['qualified']}/{samples} boards")

    print("-" * 75)
    print(f"Total Qualified Deals Evaluated: {stats['qualified']}")
    print(f"Route Choices -> Blueprint A: {stats['A']} | Blueprint B: {stats['B']} | Blueprint C: {stats['C']}")
    print(f"✅ Slams Bid & Made (Cold)     : {stats['made_slam']}")
    print(f"✅ Safe Stops (Avoided Down)   : {stats['safely_stopped']}")
    print(f"❌ Slams Bid Down (Overbid)    : {stats['went_down_slam']}")
    print(f"❌ Missed Cold Slams (Underbid): {stats['missed_cold_slam']}")
    print("-" * 75)
    accuracy = ((stats["made_slam"] + stats["safely_stopped"]) / stats["qualified"]) * 100
    print(f"🎯 REAL DOUBLE-DUMMY ACCURACY  : {accuracy:.2f}%")
    print("=" * 75)


# ============================================================================
# 6. OVERBID DIAGNOSTIC INSPECTOR
# ============================================================================
def inspect_overbid_boards(target_overbids=3):
    found = 0
    evaluated = 0

    print("=" * 75)
    print(f" HUNTING FOR {target_overbids} OVERBID BOARDS TO INSPECT...")
    print("=" * 75)

    while found < target_overbids:
        hands, pbn_str = deal_random_board()
        n_hand = hands["N"]
        s_hand = hands["S"]

        if n_hand.total_hcp() + s_hand.total_hcp() < 21:
            continue

        evaluated += 1
        if evaluated % 50 == 0:
            print(f" ... checked {evaluated} boards, found {found}/{target_overbids}")

        best_suit = find_best_fit(n_hand, s_hand)

        ptv_a = PTVCalculator.calculate(n_hand, s_hand, best_suit, ContractBlueprint.BLUEPRINT_A_SIDE_SUIT)
        ptv_b = PTVCalculator.calculate(n_hand, s_hand, best_suit, ContractBlueprint.BLUEPRINT_B_CROSS_RUFF)
        ptv_c = PTVCalculator.calculate(n_hand, s_hand, "NT", ContractBlueprint.BLUEPRINT_C_HIGH_POWER)

        short_n = {s for s in SUITS if n_hand.length(s) <= 1}
        short_s = {s for s in SUITS if s_hand.length(s) <= 1}
        has_cross_shortness = len(short_n - short_s) >= 1 and len(short_s - short_n) >= 1
        can_cross_ruff = min(n_hand.length(best_suit), s_hand.length(best_suit)) >= 4

        is_flat = min(n_hand.pattern()) >= 2 and min(s_hand.pattern()) >= 2

        if ptv_c > ptv_a and ptv_c > ptv_b and is_flat:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_C_HIGH_POWER
            strain = "NT"
        elif ptv_b > ptv_a and has_cross_shortness and can_cross_ruff:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            strain = best_suit
        else:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
            strain = best_suit

        ctx = AuctionContext(n_hand, s_hand, strain, chosen_blueprint)
        engine = SlamEngineStateMachine(ctx)
        _, final_bid = engine.run()

        if final_bid == "3NT":
            bid_level = 3
        elif final_bid in ["4S", "4H", "4NT"]:
            bid_level = 4
        elif final_bid in ["5S", "5H", "5D", "5C", "5NT"]:
            bid_level = 5
        else:
            bid_level = int(final_bid[0])

        needed_tricks = bid_level + 6

        deal = Deal(pbn_str)
        denom = STRAIN_TO_DENOM[strain]
        table = dds.calc_dd_table(deal)
        actual_tricks = table[Player.north, denom]

        if bid_level >= 6 and actual_tricks < needed_tricks:
            found += 1
            print(f"\n🔍 [OVERBID CASE #{found}]")
            print(f"  Bid Contract : {final_bid} (Needed {needed_tricks} tricks)")
            print(f"  DDS Result   : {actual_tricks} tricks (Down {needed_tricks - actual_tricks}!)")
            print(f"  Architecture : {chosen_blueprint.name}")
            print(f"  Combined HCP : {n_hand.total_hcp() + s_hand.total_hcp()} HCP | Combined Losers: {n_hand.count_losers() + s_hand.count_losers()} LTC")
            print(f"  North Hand   : ♠{n_hand.holding['S']} ♥{n_hand.holding['H']} ♦{n_hand.holding['D']} ♣{n_hand.holding['C']}")
            print(f"  South Hand   : ♠{s_hand.holding['S']} ♥{s_hand.holding['H']} ♦{s_hand.holding['D']} ♣{s_hand.holding['C']}")
            print(f"  East Hand    : ♠{hands['E'].holding['S']} ♥{hands['E'].holding['H']} ♦{hands['E'].holding['D']} ♣{hands['E'].holding['C']}")
            print(f"  West Hand    : ♠{hands['W'].holding['S']} ♥{hands['W'].holding['H']} ♦{hands['W'].holding['D']} ♣{hands['W'].holding['C']}")
            print(f"  Full PBN     : {pbn_str}")
            print("-" * 75)


# Run both benchmark and inspector
run_benchmark(samples=200)
inspect_overbid_boards(target_overbids=3)
