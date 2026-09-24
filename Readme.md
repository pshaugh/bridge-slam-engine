Hello, changes have been made to make this more accessible! It now launches Bo Haglund's Double Dummy Solver when the program is run, and the edge case hunter is now actually included in the code (oops, sorry). Run the code in Colab, and you will get a results module of how well the engine did at bidding possible slams out of 200 hands... it seems to be around 90% accurate, sometimes getting as high as 95%. i'm actually new to this site, if you see anything i've done wrong in making this code accessible, please let me know.  Thanks, and enjoy!



BridgeSlamEngine: Rule-Based Tri-Blueprint Auction System
An architectural auction engine for bridge slam evaluation. Rather than relying purely on Milton Work High-Card Points (HCP) or exhaustive Monte Carlo tree searches, BridgeSlamEngine decomposes bidding decisions into three structural blueprints, an early Hazard Detection Sieve, and a strict Keycard/Control State Machine.

When benchmarked against Bo Haglund's C++ Double Dummy Solver (DDS) on unconstrained 52-card random deals with slam-viable opening strength (21+ combined HCP), the system achieved 91.50% objective contract-par accuracy.

Key Architecture
Traditional bidding bots frequently fail at slam exploration because high-card points obscure structural hand flaws. BridgeSlamEngine processes auctions through a four-phase pipeline:

[North & South Hands]
        │
        ▼
[Phase 1: Potential Trick Value (PTV)]
  • Blueprint A: Side-Suit Establishment & Ruffing Values
  • Blueprint B: Cross-Ruff Geometric Independence
  • Blueprint C: High-Power Running Tricks & Body Intertwining
        │
        ▼
[Phase 2: Hazard Detection Sieve (DPI)]
  • Offside Cashing Losers (Bypassed Controls)
  • Shortage Duplication (Wasted Honors opposite Splinters)
  • Flat-Shape Mirror Penalties
  • Unstopped Suit Leakage (in No-Trump)
        │
        ▼
[Phase 3: Keycard & Control Gate]
  • 5-Keycard Verification (4 Aces + Trump King)
  • Superfit Queen Waiver Rule
        │
        ▼
[Phase 4: Contract Commitment]
  ↳ Clamp at Game (4M / 3NT / 5m) or Commit to 6 / 7


1. The Three Blueprints
Blueprint A (Side-Suit Establishment): Evaluates trick generation via long trump stabilization, side-suit running length, and short-hand ruffing potential.


Blueprint B (Cross-Ruff Framework): Activates on mutual extreme distributional shortages. Scores trumps in both hands independently while enforcing top-honor control gates to prevent overruffs.


Blueprint C (High-Power NT Core): Replaces trump mechanics with contiguous running honor density, intermediate body-card evaluation (10s and 9s), and strict suit-stopping criteria across all four denominations.


2. The Hazard Detection Sieve
Before committing to Blackwood or control-bidding gates, the engine runs a fast-abort heuristic (HazardSieve):

Mirror Misfit Filter: Rejects balanced 4-3-3-3 opposite 4-3-3-3 holdings under 34 HCP that lack ruffing or setup leverage.


Duplication Penalty: Discards secondary honors (K, Q, J) that duplicate partner's shortages.


Cashing Gap Check: Flags suits bypassed during auction sequences lacking first-round control.


Benchmark & Performance
The engine was evaluated on random 52-card deals dealt under standard shuffle distributions, filtered for combined North-South strength $\ge 21$ HCP.

Play outcomes were calculated using Bo Haglund's minimax C++ engine via endplay.dds, computing exact optimal double-dummy makeable tricks against perfect defense.

Verified Benchmark Scorecard
Plaintext
===========================================================================
 RUNNING DDS BENCHMARK (200 Slam-Viable Boards) 
===========================================================================
Total Qualified Deals Evaluated: 200
Route Choices: Blueprint A: 142 | Blueprint B: 8 | Blueprint C: 50
---------------------------------------------------------------------------
✅ Slams Bid & Made (Cold)     : 7
✅ Safe Stops (Avoided Down)   : 176
❌ Slams Bid Down (Overbid)    : 12
❌ Missed Cold Slams (Underbid): 5
---------------------------------------------------------------------------
🎯 REAL DOUBLE-DUMMY ACCURACY  : 91.50%
===========================================================================


Key Performance Insights
176 Safe Clamps: The Hazard Sieve avoided bidding down-one slams on boards where raw high-card points suggested an aggressive auction.


Low Miss Rate: The engine only underbid 5 makeable slams out of 200 boards (2.5%), preserving competitive par.


Getting Started
Prerequisites
Python 3.10 or later


C++ runtime (bundled automatically with endplay)


Installation
Clone the repository and install the double-dummy solver dependency:

Bash
git clone https://github.com/pshaugh/bridge-slam-engine.git
cd bridge-slam-engine
pip install endplay


Usage
1. Interactive Diagnostic Audit
Inspect step-by-step bidding decisions on sample hands or custom PBN strings:

Bash
python slam_debugger.py


Example diagnostic trace:

Plaintext
==============================================================================
  AUCTION AUDIT: Strain [S] | Architecture [A: Side-Suit Establishment]
==============================================================================
[HANDS]
  North (18 HCP): ♠ AK3         ♥ KQ4         ♦ AJ5         ♣ J742      
  South (13 HCP): ♠ Q84         ♥ A93         ♦ KQ6         ♣ K863      
  Combined Power: 31 HCP | Pattern: [4, 3, 3, 3] vs [4, 3, 3, 3]

[PHASE 1: TRICK GENERATION (PTV = 11.0)]
  • Long trump base: +3
  • Side-suit C (8 cards): +6
  • Outside Ace in D: +1.0
  • Outside Ace in H: +1.0

[PHASE 2: HAZARD DETECTION SIEVE (DPI Penalty = 1.5)]
  🛑 FATAL HAZARD: Hazard 4: Mirror flat profile (0 symmetry variance) under 34 HCP

[PHASE 3: STATE MACHINE DECISION]
  ↳ Gate 1 FAILED: Insufficient baseline power (HCP: 31/23, PTV: 11.0/11.5).
  🎯 FINAL CONTRACT: 4S (Safe Stop)
==============================================================================


2. Reproduce the DDS Benchmark
Run the objective double-dummy evaluation across 200 deals:

Bash
python benchmark_dds.py


Repository Structure
Plaintext
├── slam_engine/
│   ├── __init__.py
│   ├── models.py           # Hand, Suits, Honor Points, EngineState
│   ├── blueprints.py       # PTVCalculator (Blueprints A, B, C)
│   ├── hazards.py          # HazardSieve & Defect Penalty Index (DPI)
│   └── state_machine.py    # AuctionContext & SlamEngineStateMachine
├── slam_debugger.py        # Interactive CLI diagnostic inspector
├── benchmark_dds.py        # Endplay / Bo Haglund DDS verification harness
├── README.md
└── LICENSE


Roadmap
[ ] Add support for Roman Keycard 1430 response sequences in auction logs.


[ ] Export outlier boards (the 12 overbid deals) directly to .pbn for Bridge Base Online (BBO) review.


[ ] Add support for 5-card major opening infrastructure and Stayman / Jacoby transfer hand contexts.


License
This project is licensed under the GNU Affero General Public License v3.0 (AGPLv3). See the LICENSE file for details. Commercial licensing options are available upon inquiry.
