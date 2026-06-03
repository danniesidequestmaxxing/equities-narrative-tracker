"""M2: sentiment, contrarian, pump, narratives, momentum, digest."""

from narrative_tracker.analyze.analyzer import Analyzer
from narrative_tracker.analyze.narratives import NarrativeTracker, assign_narratives
from narrative_tracker.analyze.sentiment import (
    SentimentAggregator,
    contrarian_signal,
    coordinated_pump_score,
    credibility_prior,
)

# --- sentiment -------------------------------------------------------------


def test_sentiment_bullish_is_positive():
    agg = SentimentAggregator()
    t = 1000.0
    for i in range(5):
        agg.update(symbol="NVDA", stance="bullish", stance_confidence=0.9, credibility=0.6, ts=t + i)
    r = agg.read("NVDA", t + 5)
    assert r["S"] > 0.3
    assert r["n_eff"] >= 4


def test_sentiment_neff_guard_single_voice():
    agg = SentimentAggregator()
    agg.update(symbol="X", stance="bullish", stance_confidence=0.9, credibility=0.6, ts=1.0)
    assert agg.read("X", 1.0)["n_eff"] < 2  # one loud voice != consensus


def test_credibility_prior_tiers():
    assert credibility_prior("HOT") > credibility_prior("WARM") > credibility_prior("COLD")


# --- contrarian ------------------------------------------------------------


def test_contrarian_euphoria_extreme():
    history = [0.2] * 40
    sig = contrarian_signal(history, 0.9, n_eff=10)
    assert sig and sig["state"] == "euphoria" and sig["side"] == "contrarian_short"


def test_contrarian_needs_coverage():
    assert contrarian_signal([0.2] * 40, 0.9, n_eff=3) is None  # n_eff too low


# --- pump ------------------------------------------------------------------


def test_pump_detected_for_coordinated_low_cred_burst():
    mentions = [
        {"credibility": 0.05, "account_age_days": 5, "content_dup": True, "cluster_id": "c1", "known_pumper": True}
        for _ in range(20)
    ]
    res = coordinated_pump_score(mentions, baseline_rate=1.0)
    assert res["flag"] in ("ALERT", "ACT") and res["score"] >= 0.7


def test_pump_not_flagged_for_credible_burst():
    mentions = [
        {"credibility": 0.7, "account_age_days": 2000, "content_dup": False, "cluster_id": None, "known_pumper": False}
        for _ in range(20)
    ]
    assert coordinated_pump_score(mentions, baseline_rate=1.0)["flag"] is None


def test_pump_requires_burst():
    res = coordinated_pump_score([{"credibility": 0.1}] * 2, baseline_rate=5.0)
    assert res["reason"] == "no_burst"


# --- narratives ------------------------------------------------------------


def test_assign_by_ticker():
    assert "AI infrastructure" in assign_narratives("NVDA")


def test_assign_by_keyword():
    assert "Nuclear / uranium" in assign_narratives("XYZ", "uranium demand surging")


def test_momentum_rising_then_fading():
    nt = NarrativeTracker()
    t = 1000.0
    for i in range(10):
        nt.add("AI infrastructure", weight=0.5, ts=t + i)
    assert nt.momentum("AI infrastructure", t + 10) == "rising"
    assert nt.momentum("AI infrastructure", t + 30 * 3600) in ("fading", "dormant")


# --- analyzer + digest -----------------------------------------------------


def test_analyzer_digest_mentions_narrative_and_ticker():
    a = Analyzer()
    t = 1000.0
    for i in range(6):
        a.ingest(
            symbol="NVDA",
            text="$NVDA ai gpu demand insane",
            stance="bullish",
            stance_confidence=0.9,
            credibility=0.6,
            ts=t + i,
        )
    mdv2, plain = a.digest(
        cadence_label="Daily", date_label="03 Jun 2026", now=t + 6, posts_count=6, accounts_count=1
    )
    assert "NVDA" in plain
    assert "AI infrastructure" in plain
    assert "not financial advice" in mdv2
