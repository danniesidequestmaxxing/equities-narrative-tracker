"""Extraction pipeline (M1) — the cascade orchestrator.

    options  →  cashtags (+ collision gate)  →  entity-link  →  vision  →  stance  →  dedupe

Returns ``list[Mention]`` with both confidences populated. The stance classifier
and vision extractor are injected (rule-based / fake by default, LLM / multimodal
in prod), so the whole pipeline is deterministic and testable without creds.
"""

from __future__ import annotations

from ..schemas.mention import AssetClass, Mention, ResolutionMethod
from .cashtag import extract_cashtags
from .collision import is_real_ticker
from .entity import link_entities
from .options import parse_options
from .stance import RuleBasedStanceClassifier, StanceClassifier
from .symbology import classify_symbol
from .vision import VisionExtractor


def _option_signature(m: Mention) -> tuple:
    sig: tuple = (m.symbol, m.asset_class.value)
    if m.option_detail:
        sig += (
            m.option_detail.right.value,
            m.option_detail.strike,
            m.option_detail.expiry_raw,
        )
    return sig


def _dedupe(mentions: list[Mention]) -> list[Mention]:
    best: dict[tuple, Mention] = {}
    for m in mentions:
        sig = _option_signature(m)
        if sig not in best or m.mention_confidence > best[sig].mention_confidence:
            best[sig] = m
    return list(best.values())


class ExtractionPipeline:
    def __init__(
        self,
        *,
        stance: StanceClassifier | None = None,
        vision: VisionExtractor | None = None,
    ) -> None:
        self._stance = stance or RuleBasedStanceClassifier()
        self._vision = vision

    async def extract(
        self,
        *,
        text: str,
        media_urls: list[str] | None = None,
        source_post_id: str = "",
    ) -> list[Mention]:
        mentions: list[Mention] = []
        seen: set[str] = set()

        # S1a — options (so "$SPY 600c" is an OPTION, not a bare equity).
        option_roots: set[str] = set()
        for root, detail, surface in parse_options(text):
            mentions.append(
                Mention(
                    symbol=root,
                    asset_class=AssetClass.OPTION,
                    resolution_method=ResolutionMethod.CASHTAG_EXACT,
                    option_detail=detail,
                    mention_confidence=0.9,
                    surface_text=surface,
                    source_post_id=source_post_id,
                )
            )
            option_roots.add(root)

        # S1b — cashtags with the collision gate.
        cashtags = extract_cashtags(text)
        for c in cashtags:
            symbol = c["symbol"]
            if symbol in option_roots:
                continue  # already captured as an option
            verdict, confidence = is_real_ticker(
                symbol, text, other_cashtags=len(cashtags) - 1
            )
            if verdict is False:
                continue  # the English word, not a ticker
            method = (
                ResolutionMethod.CASHTAG_EXACT
                if verdict is True and confidence >= 0.9
                else ResolutionMethod.CASHTAG_DISAMBIG
            )
            mentions.append(
                Mention(
                    symbol=symbol,
                    asset_class=AssetClass(classify_symbol(symbol, text)),
                    resolution_method=method,
                    mention_confidence=confidence,
                    surface_text=c["surface"],
                    source_post_id=source_post_id,
                )
            )
        seen = {m.symbol for m in mentions}

        # S2 — cashtag-less company/product/person references.
        for m in link_entities(text, source_post_id=source_post_id):
            if m.symbol not in seen:
                mentions.append(m)
                seen.add(m.symbol)

        # S4 — vision, only for image posts that text couldn't resolve.
        if self._vision and media_urls and not mentions:
            for url in media_urls:
                for vm in await self._vision.extract(url, source_post_id=source_post_id):
                    if vm.symbol not in seen:
                        mentions.append(vm)
                        seen.add(vm.symbol)

        # S3 — stance (one post-level read applied to text-derived mentions;
        # vision mentions keep any stance they carried).
        result = self._stance.classify(text)
        for m in mentions:
            if m.resolution_method is ResolutionMethod.VISION_OCR:
                continue
            m.stance = result.stance
            m.negation_flag = result.negation_flag
            m.stance_confidence = result.stance_confidence

        return _dedupe(mentions)
