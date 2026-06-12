"""Extraction pipeline (M1) — the cascade orchestrator.

    options  →  cashtags (+ collision gate)  →  entity-link  →  relevance gate
    →  LLM inference (cashtag-less)  →  vision  →  stance  →  dedupe

Returns ``list[Mention]`` with both confidences populated. The stance classifier,
relevance gate and vision extractor are injected (rule-based / none / fake by
default, LLM / multimodal in prod), so the whole pipeline is deterministic and
testable without creds.
"""

from __future__ import annotations

import logging

from ..schemas.mention import AssetClass, Mention, ResolutionMethod
from .cashtag import extract_cashtags
from .collision import is_real_ticker
from .entity import link_entities
from .options import parse_options
from .relevance import RelevanceGate
from .stance import RuleBasedStanceClassifier, StanceClassifier
from .symbology import classify_symbol
from .vision import VisionExtractor

log = logging.getLogger(__name__)

# Drop a mention only when the LLM is confidently sure it's NOT about the asset.
_RELEVANCE_DROP_CONFIDENCE = 0.6


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
        relevance: RelevanceGate | None = None,
        inferrer=None,  # M14: MentionInferrer for cashtag-less ticker talk
    ) -> None:
        self._stance = stance or RuleBasedStanceClassifier()
        self._vision = vision
        self._relevance = relevance
        self._inferrer = inferrer

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

        # S2b — LLM relevance gate: drop symbols the post isn't actually
        # discussing as assets ("$RDDT" = Reddit-the-site). Fail-open: any LLM
        # failure keeps every mention.
        if self._relevance is not None and mentions:
            try:
                verdicts = await self._relevance.check(text, [m.symbol for m in mentions])
                kept = []
                for m in mentions:
                    v = verdicts.get(m.symbol)
                    if (
                        v is not None
                        and not v.is_asset_reference
                        and v.confidence >= _RELEVANCE_DROP_CONFIDENCE
                    ):
                        log.info("relevance gate dropped $%s: %s", m.symbol, v.reason or "not an asset reference")
                        continue
                    kept.append(m)
                mentions = kept
                seen = {m.symbol for m in mentions}
            except Exception as exc:  # noqa: BLE001 - degrade, never break extraction
                log.warning("relevance gate failed (%s); keeping all mentions", exc)

        # S2c — M14: cashtag-less inference. Only when the deterministic passes
        # found nothing and there's enough text to mean something ("Micron is
        # going to rip" has no cashtag but is clearly about MU). Relevance is
        # baked into the prompt; fail-open like every LLM stage.
        if self._inferrer is not None and not mentions and len(text.split()) >= 4:
            try:
                from .mentions_llm import to_mentions

                inferred = to_mentions(await self._inferrer(text), source_post_id=source_post_id)
                for m in inferred:
                    if m.symbol not in seen:
                        mentions.append(m)
                        seen.add(m.symbol)
                if inferred:
                    log.info("inferred %s from cashtag-less post", [m.symbol for m in inferred])
            except Exception as exc:  # noqa: BLE001 - degrade, never break extraction
                log.warning("mention inference failed (%s); skipping", exc)

        # S4 — vision, only for image posts that text couldn't resolve.
        if self._vision and media_urls and not mentions:
            for url in media_urls:
                for vm in await self._vision.extract(url, source_post_id=source_post_id):
                    if vm.symbol not in seen:
                        mentions.append(vm)
                        seen.add(vm.symbol)

        # S3 — stance (one post-level read applied to text-derived mentions;
        # vision mentions keep any stance they carried).
        result = await self._stance.classify(text)
        for m in mentions:
            m.conviction = result.conviction  # M15: post-level commitment
            m.is_position = result.is_position
            if m.resolution_method is ResolutionMethod.VISION_OCR:
                continue
            m.stance = result.stance
            m.negation_flag = result.negation_flag
            m.stance_confidence = result.stance_confidence

        return _dedupe(mentions)
