"""Recommender types: the locked risk config, a candidate call, and gate I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..schemas.call import Direction


@dataclass
class RiskConfig:
    """Conservative preset (the locked v1 parameters from the plan)."""

    pmin: float = 5.0                     # min price
    vmin_usd: float = 10_000_000          # min 20-day avg $ volume
    smax_pct: float = 0.5                 # max bid/ask spread %
    min_market_cap: float = 500_000_000
    theta_call: float = 0.75              # mention-confidence bar
    phi_call: float = 0.70                # stance-confidence bar
    numeric_sanity_pct: float = 5.0       # entry must be within X% of price
    conflict_eps: float = 0.15            # neutral band on net stance
    pump_act_threshold: float = 0.85      # suppress at/above this pump score
    rho_max: float = 0.60                 # correlation cap (informational here)
    staleness_equity_s: int = 900         # 15-min feed floor
    staleness_crypto_s: int = 60
    catalyst_h: float = 24.0              # suppress directional within H hours
    risk_per_trade_pct: float = 1.0
    rr_target: float = 2.0                # target = entry +/- rr_target * risk
    atr_stop_mult: float = 1.5


@dataclass
class Candidate:
    symbol: str
    asset_class: str
    direction: Direction
    entry: float
    stop: float
    target: float
    confidence: float                     # aggregate mention confidence
    stance: str                           # bullish | bearish
    stance_confidence: float
    negation_flag: bool
    extracted_symbols: set[str]           # provenance (symbols actually in posts)
    net_cred_weighted_stance: float
    pump_score: float
    narrative: str | None
    source_accounts: list[str] = field(default_factory=list)


@dataclass
class GateContext:
    snapshot: object                      # MarketSnapshot
    config: RiskConfig
    now: datetime
    live_call_symbols: set[str] = field(default_factory=set)
    selected_narratives: set[str] = field(default_factory=set)
    budget_ok: bool = True
    audit_writable: bool = True
    killswitch_engaged: bool = False


@dataclass
class GateResult:
    name: str
    passed: bool
    measured: dict = field(default_factory=dict)
