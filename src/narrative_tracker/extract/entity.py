"""Cashtag-less entity linking (M1).

Resolves company / product / person references with no ``$`` ("Nvidia",
"Ozempic maker", "Zuck's company") to tickers via the seed alias table, with
provenance. Indirect (product/person) references get a confidence haircut.

Production augments this with a GLiNER2 NER pass (CPU, finds candidate spans
before linking) — the ``NerProvider`` protocol is the seam; M1 links directly
off the alias table, which is fully deterministic and testable.
"""

from __future__ import annotations

from typing import Protocol

from ..schemas.mention import AssetClass, Mention, ResolutionMethod
from . import symbology


class NerProvider(Protocol):  # pragma: no cover - seam for GLiNER2 in prod
    def entities(self, text: str) -> list[str]: ...


def link_entities(text: str, *, source_post_id: str = "") -> list[Mention]:
    """Return mentions for alias-table hits in ``text``."""
    out: list[Mention] = []
    for surface, entry in symbology.find_aliases(text):
        confidence = 0.8 if entry.indirect else 0.9
        out.append(
            Mention(
                symbol=entry.symbol,
                asset_class=AssetClass(entry.asset_class),
                resolution_method=ResolutionMethod.NER_ENTITY_LINK,
                mention_confidence=confidence,
                surface_text=surface,
                source_post_id=source_post_id,
                rationale=f"alias:{entry.source}",
            )
        )
    return out
