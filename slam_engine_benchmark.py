import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple

# Double Dummy Solver library
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

    def has_control(self, suit: str, level: int = 1) -> bool:
        cards = self.holding.get(suit, "")
        if level == 1:
            return "A" in cards or len(cards) == 0
        elif level == 2:
            return "K" in cards or len(cards) == 1
        return False

    def count_keycards(self, trump_suit: str) -> int:
        kc = sum(1 for s in SUITS if "A" in self.holding.get(s, ""))
        if (
            trump_suit
            and trump_suit != "NT"
            and "K" in self.holding.get(trump_suit, "")
        ):
            kc += 1
        return kc

    def has_trump_queen(self, trump_suit: str) -> bool:
        if not trump_suit or trump_suit == "NT":
            return True
        return "Q" in self.holding.get(trump_suit, "")


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

        # Hazard 1: Bypassed suits missing quick control
        for s in bypassed_suits:
            if not h1.has_control(s, level=1) and not h2.has_control(s, level=1):
                report.cashing_gap_penalty += 3.0
                report.fatal_reason = (
                    f"Hazard 1: Bypassed suit {s} missing 1st-round control"
                )

        # Hazard 2: Wasted honors opposite shortage
        if (
            splinter_suit
            and blueprint != ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            and blueprint != ContractBlueprint.BLUEPRINT_C_HIGH_POWER
        ):
            wasted = h1.hcp_in_suit(splinter_suit) + h2.hcp_in_suit(
                splinter_suit
            )
            if "A" in h1.holding.get(splinter_suit, "") or "A" in h2.holding.get(
                splinter_suit, ""
            ):
                wasted -= 4
            wasted = max(0, wasted)
            report.wasted_hcp = wasted
            if wasted >= 4:
                report.fatal_reason = f"Hazard 2: Wasted honors opposite shortage"

        # Hazard 3: Trump Fit Quality
        if trump_suit and trump_suit != "NT":
            total_trump = h1.length(trump_suit) + h2.length(trump_suit)
            trumps_combined = (
                h1.holding.get(trump_suit, "") + h2.holding.get(trump_suit, "")
            )
            if total_trump < 8:
                report.trump_leak_penalty += 3.0
                report.fatal_reason = "Hazard 3: Trump fit under 8 cards"
            elif total_trump == 8 and (
                "Q" not in trumps_combined and "J" not in trumps_combined
            ):
                report.trump_leak_penalty += 1.0

        # Hazard 4: Mirror / Misfit Architecture
        shape_symmetry = sum(abs(h1.length(s) - h2.length(s)) for s in SUITS)
        if min(h1.pattern()) >= 2 and min(h2.pattern()) >= 2:
            if blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF:
                report.mirror_penalty = 5.0
                report.fatal_reason = (
                    "Hazard 4: Cross-Ruff chosen but hands are flat"
                )
            elif (
                blueprint == ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
                and shape_symmetry <= 4
                and (h1.total_hcp() + h2.total_hcp()) < 34
            ):
                report.mirror_penalty = 1.5
                report.fatal_reason = f"Hazard 4: Flat mirror profile under 34 HCP"
            elif blueprint == ContractBlueprint.BLUEPRINT_C_HIGH_POWER:
                for s in SUITS:
                    comb_suit = (
                        h1.holding.get(s, "") + h2.holding.get(s, "")
                    )
                    if not any(c in comb_suit for c in ["A", "K", "Q"]):
                        report.mirror_penalty += 3.0
                        report.fatal_reason = (
                            f"Hazard 4: High Power system lacks a stopper in suit {s}"
                        )

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
    def calculate(
        h1: Hand,
        h2: Hand,
        strain: str,
        blueprint: ContractBlueprint,
    ) -> float:
        ptv = 0.0
        total_trump = (
            0 if strain == "NT" else h1.length(strain) + h2.length(strain)
        )

        if blueprint == ContractBlueprint.BLUEPRINT_A_SIDE_SUIT:
            ptv += max(h1.length(strain), h2.length(strain))
            if total_trump == 10:
                ptv += 1.0
            elif total_trump >= 11:
                ptv += 2.0

            candidates = [s for s in SUITS if s != strain]
            candidates.sort(
                key=lambda s: h1.length(s) + h2.length(s), reverse=True
            )
            side = candidates[0]
            side_combined = h1.length(side) + h2.length(side)

            if side_combined >= 5:
                top_honors = sum(
                    1
                    for c in ["A", "K", "Q"]
                    if c in (h1.holding.get(side, "") + h2.holding.get(side, ""))
                )
                ptv += max(top_honors, side_combined - 2)

            for s in candidates:
                if s != side and (
                    "A" in h1.holding.get(s, "") or "A" in h2.holding.get(s, "")
                ):
                    ptv += 1.0

            short_hand = h2 if h1.length(strain) >= h2.length(strain) else h1
            for s in candidates:
                if short_hand.length(s) <= 1:
                    ptv += 2 - short_hand.length(s)

        elif blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF:
            ptv += total_trump
            for s in SUITS:
                if s != strain:
                    if "A" in h1.holding.get(s, "") or "A" in h2.holding.get(
                        s, ""
                    ):
                        ptv += 1.0
                    if "K" in h1.holding.get(s, "") or "K" in h2.holding.get(
                        s, ""
                    ):
                        ptv += 0.5
                    for hand in [h1, h2]:
                        if hand.length(s) == 0:
                            ptv += 1.5
                        elif hand.length(s) == 1:
                            ptv += 0.75
            trumps_combined = (
                h1.holding.get(strain, "") + h2.holding.get(strain, "")
            )
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
                if ("K" in comb_suit or "Q" in comb_suit) and (
                    "10" in comb_suit or "9" in comb_suit
                ):
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
        total_trump = (
            0
            if c.agreed_strain == "NT"
            else c.north.length(c.agreed_strain)
            + c.south.length(c.agreed_strain)
        )
        ptv = PTVCalculator.calculate(
            c.north, c.south, c.agreed_strain, c.blueprint
        )

        is_superfit = total_trump >= 10 and comb_hcp >= 19
        hcp_floor = (
            23
            if c.blueprint != ContractBlueprint.BLUEPRINT_C_HIGH_POWER
            else 29
        )

        # Gate 1: Baseline Power
        if not (
            (comb_hcp >= hcp_floor or ptv >= 14.0 or is_superfit)
            and ptv >= 11.5
        ):
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
        keycards = c.north.count_keycards(
            c.agreed_strain
        ) + c.south.count_keycards(c.agreed_strain)
        trump_q = c.north.has_trump_queen(
            c.agreed_strain
        ) or c.south.has_trump_queen(c.agreed_strain)

        if (
            is_superfit
            or c.blueprint == ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            or c.agreed_strain == "NT"
        ):
            trump_q = True

        kc_threshold = 3
        if keycards <= kc_threshold:
            c.state = EngineState.ABORT
            c.final_contract = (
                f"5{c.agreed_strain}" if c.agreed_strain != "NT" else "4NT"
            )
            return (c.state, c.final_contract)

        # Gate 4: Commit
        c.state = EngineState.COMMIT
        if keycards >= 5 and trump_q and ptv >= 15.0:
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

        hand_obj = Hand(
            holding={
                s: "".join(suit_holdings[s]).replace("T", "10") for s in SUITS
            }
        )
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

        # Blueprint selector
        ptv_a = PTVCalculator.calculate(
            n_hand, s_hand, "S", ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
        )
        ptv_b = PTVCalculator.calculate(
            n_hand, s_hand, "S", ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
        )
        ptv_c = PTVCalculator.calculate(
            n_hand, s_hand, "NT", ContractBlueprint.BLUEPRINT_C_HIGH_POWER
        )

        has_shortness = (
            min(n_hand.pattern()) <= 1 or min(s_hand.pattern()) <= 1
        )
        is_flat = min(n_hand.pattern()) >= 2 and min(s_hand.pattern()) >= 2

        if ptv_c > ptv_a and ptv_c > ptv_b and is_flat:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_C_HIGH_POWER
            strain = "NT"
            stats["C"] += 1
        elif ptv_b > ptv_a and has_shortness:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_B_CROSS_RUFF
            strain = "S"
            stats["B"] += 1
        else:
            chosen_blueprint = ContractBlueprint.BLUEPRINT_A_SIDE_SUIT
            strain = "S"
            stats["A"] += 1

        # Auction decision
        ctx = AuctionContext(n_hand, s_hand, strain, chosen_blueprint)
        engine = SlamEngineStateMachine(ctx)
        _, final_bid = engine.run()

        # Parse contract
        if final_bid == "3NT":
            bid_level = 3
        elif final_bid in ["4S", "4H", "4NT"]:
            bid_level = 4
        elif final_bid in ["5S", "5H", "5D", "5C", "5NT"]:
            bid_level = 5
        else:
            bid_level = int(final_bid[0])

        needed_tricks = bid_level + 6

        # Double-dummy solve via endplay C library
        deal = Deal(pbn_str)
        denom = STRAIN_TO_DENOM[strain]
        # 1. Calculate the double-dummy table for the deal
        table = dds.calc_dd_table(deal)

        # 2. Look up the tricks for North in your agreed strain
        actual_tricks = table[denom, Player.north]
        # Double-dummy solve via endplay C library
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
    print(
        f"Route Choices -> Blueprint A: {stats['A']} | Blueprint B: {stats['B']} | Blueprint C: {stats['C']}"
    )
    print(f"✅ Slams Bid & Made (Cold)     : {stats['made_slam']}")
    print(f"✅ Safe Stops (Avoided Down)   : {stats['safely_stopped']}")
    print(f"❌ Slams Bid Down (Overbid)    : {stats['went_down_slam']}")
    print(f"❌ Missed Cold Slams (Underbid): {stats['missed_cold_slam']}")
    print("-" * 75)
    accuracy = (
        (stats["made_slam"] + stats["safely_stopped"]) / stats["qualified"]
    ) * 100
    print(f"🎯 REAL DOUBLE-DUMMY ACCURACY  : {accuracy:.2f}%")
    print("=" * 75)


# Run 200 boards
run_benchmark(samples=200)
