"""Analyzer facade (M2): ties sentiment + narratives together and builds digests."""

from __future__ import annotations

from .digest import build_digest
from .narratives import NarrativeTracker, assign_narratives
from .sentiment import SentimentAggregator, contrarian_signal


class Analyzer:
    def __init__(self) -> None:
        self.sentiment = SentimentAggregator()
        self.narratives = NarrativeTracker()

    def ingest(
        self,
        *,
        symbol: str,
        text: str,
        stance: str,
        stance_confidence: float,
        credibility: float,
        ts: float,
    ) -> None:
        self.sentiment.update(
            symbol=symbol,
            stance=stance,
            stance_confidence=stance_confidence,
            credibility=credibility,
            ts=ts,
        )
        for label in assign_narratives(symbol, text):
            self.narratives.add(label, weight=credibility, ts=ts)

    def hot_tickers(self, now: float, *, top: int = 10) -> list[dict]:
        rows = []
        for sym in self.sentiment.symbols():
            r = self.sentiment.read(sym, now)
            rows.append(
                {
                    "symbol": sym,
                    "S": r["S"],
                    "conf": r["conf"],
                    "n_eff": r["n_eff"],
                    "contrarian": contrarian_signal(
                        self.sentiment.history(sym), r["S"], r["n_eff"]
                    ),
                }
            )
        rows.sort(key=lambda x: abs(x["S"]) * x["n_eff"], reverse=True)
        return rows[:top]

    def narrative_states(self, now: float) -> list[dict]:
        return [
            {"label": label, "momentum_state": self.narratives.momentum(label, now)}
            for label in self.narratives.labels()
        ]

    def digest(
        self,
        *,
        cadence_label: str,
        date_label: str,
        now: float,
        posts_count: int = 0,
        accounts_count: int = 0,
    ) -> tuple[str, str]:
        return build_digest(
            cadence_label=cadence_label,
            date_label=date_label,
            narratives=self.narrative_states(now),
            hot_tickers=self.hot_tickers(now),
            posts_count=posts_count,
            accounts_count=accounts_count,
        )
