---
title: "Design: Ticker Extraction + Disambiguation Pipeline"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M1
modules: [extract/cashtag.py, extract/ner_llm.py, extract/stance.py, extract/symbology.py, extract/vision.py, schemas/mention.py]
---

# Ticker Extraction + Disambiguation Pipeline (2026)

Posts → structured `Mention` records. Staged cheap→expensive. Each post yields **N mentions**; each carries asset class, resolution provenance, stance with its own confidence, and options detail.

## 0. Output schema (the contract)
```python
class AssetClass(str, Enum): EQUITY="equity"; ETF="etf"; OPTION="option"; CRYPTO="crypto"; INDEX="index"; FX="fx"; UNKNOWN="unknown"
class ResolutionMethod(str, Enum):
    CASHTAG_EXACT="cashtag_exact"; CASHTAG_DISAMBIG="cashtag_disambiguated"; NER_ENTITY_LINK="ner_entity_link"
    LLM_INFERENCE="llm_inference"; VISION_OCR="vision_ocr"; ALIAS_TABLE="alias_table"; REJECTED="rejected"
class Stance(str, Enum): BULLISH="bullish"; BEARISH="bearish"; NEUTRAL="neutral"; UNCLEAR="unclear"
class OptionRight(str, Enum): CALL="C"; PUT="P"

class OptionDetail(BaseModel):
    right: OptionRight; strike: Optional[float]=None; expiry: Optional[date]=None
    expiry_raw: Optional[str]=None; is_leaps: bool=False; dte_relative: Optional[int]=None; occ_symbol: Optional[str]=None

class Mention(BaseModel):
    symbol: str; asset_class: AssetClass; resolution_method: ResolutionMethod
    stance: Stance; negation_flag: bool=False
    mention_confidence: float=Field(ge=0,le=1); stance_confidence: float=Field(ge=0,le=1)
    option_detail: Optional[OptionDetail]=None
    surface_text: str; char_span: tuple[int,int]; source_post_id: str
    is_quoted_signal: bool=False; thread_root_id: Optional[str]=None; rationale: Optional[str]=None
    @field_validator("symbol")
    @classmethod
    def upper(cls, v): return v.strip().upper()
```
**Two confidences are non-negotiable and orthogonal.** `mention_confidence` = "is this really symbol X?"; `stance_confidence` = "given X, is the direction right?". An inverted stance is a wrong trade even when the symbol is certain → they must be independently gateable (θ/φ).

## 1. The cascade
```
S0 normalize/segment → S1 REGEX (cashtags, $-amount filter, options parser, collision gate)
   → unambiguous: emit ; collision/ambiguous ↓ ; no cashtag but company/product ↓
S2 NER + ENTITY LINKING (GLiNER2 + EDGAR/Nasdaq/Wikidata alias table) → high-conf: emit ; else ↓
S3 LLM DISAMBIGUATION + STANCE (instructor + Pydantic, k-sample) → residuals, cross-asset, every non-trivial stance
   → image attached & unresolved ↓
S4 VISION (image posts only: chart screenshots) → S5 CALIBRATION + GATING → final Mentions
```
Each candidate span exits at the cheapest stage that resolves it confidently. **GLiNER2 runs on CPU ~130–208 ms, ~2.6× faster than GPT-4o**, within ~10 F1 of it → right "middle" tier to control the LLM bill.

## 2. S1 regex
```python
CASHTAG_RE = re.compile(r'(?<![A-Za-z0-9])\$([A-Za-z]{1,6})(\.[A-Z])?\b')
DOLLAR_AMOUNT_RE = re.compile(r'\$\s?\d[\d,]*(\.\d+)?\s?([kKmMbBtT]|bn|mn)?\b')
```
Rules: digit-led ⇒ amount not ticker (`$4200`, `$3.50`); letters-only 1–6 ⇒ cashtag candidate → collision gate; `.class` preserved (`$BRK.B`); stray uppercase words become tickers only via alias table + finance context.

**Options shorthand parser** (`$SPY 600c 0DTE`, `$NVDA Jan'27 200 LEAPS`):
```python
OPT_SHORTHAND_RE = re.compile(r"""\$?(?P<root>[A-Za-z]{1,6})\s+
    (?:(?P<strike1>\d{1,5}(?:\.\d+)?)\s?(?P<right1>[cCpP])\b
      |(?P<right2>[cCpP])\s?(?P<strike2>\d{1,5}(?:\.\d+)?)\b)(?P<rest>.*)$""", re.VERBOSE)
OSI_RE = re.compile(r'^(?P<root>[A-Z]{1,6})\s*(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$')
```
`0DTE`→post date; `weekly`/`monthly` no year→next standard expiry (3rd Friday); always keep `expiry_raw`. Strike with no right + bullish stance → do **not** assume call; emit `option_detail=None`, low `mention_confidence`, let S3 decide.

**Common-word collision gate** (real US tickers that are also English words → don't emit at S1; route to local rule or S3):
`$ALL` (Allstate), `$ON` (ON Semi), `$IT` (Gartner), `$SO` (Southern), `$ARE` (Alexandria), `$BE` (Bloom), `$ANY`, `$NOW` (ServiceNow), `$REAL` (RealReal), `$OR`, **`$AI` (C3.ai — high collision)**, `$DD` (DuPont/"due diligence"), `$GO`, `$BIG`, `$HAS` (Hasbro), `$ONE`, `$OUT`, `$LOVE`, `$PLAY` (Dave&Buster), `$CASH`, `$WORK`, `$LIFE`, `$FUN` (Cedar Fair), `$SEE`, `$RUN` (Sunrun), `$TRUE` (TrueCar), `$WELL`, `$GOOD`, `$PAY`, `$CAR`, `$TWO`, `$MOON`, **`$YOLO` (real ETF *and* slang)**. Plus crypto/equity homonyms: `$ETH $BTC $SOL $ADA $LINK $COMP $UNI $APE`. **Maintain as versioned YAML** (live Nasdaq/NYSE directory ∩ English wordlist, hand-curated quarterly).

Local disambiguation (avoid an LLM call for easy collisions):
```python
def cashtag_is_real_ticker(token, post):  # returns (True|False|None, conf)
    if token not in COLLISION_SET: return True, 0.97
    fin = any(s in post.lower() for s in FIN_LEXICON)   # calls/puts/earnings/PT/shares/long/short/eps...
    if fin or len(CASHTAG_RE.findall(post)) > 1: return True, 0.85
    if used_as_lowercase_word(token, post) and not fin: return False, 0.6
    return None, 0.5    # punt to S3
```

## 3. S2 NER + entity linking (cashtag-less)
**GLiNER2** primary (schema-driven, multi-task, CPU, 2048-token); spaCy `en_core_web_trf` fallback.
```python
schema = {"entities": {"company":"...", "product":"drugs/devices implying a maker", "person":"execs/founders",
                       "sector":"thematic baskets (GLP-1 names, hyperscalers)"},
          "classification": {"finance_relevant":["yes","no"], "coarse_tone":["bullish","bearish","neutral"]}}
```
**Entity→ticker** via an **alias table** from SEC EDGAR (issuer↔ticker↔CIK) + Nasdaq/NYSE directories + **Wikidata** (aliases, products, executives, "manufacturer/parent/brand" edges). Examples: "Nvidia"→NVDA (edgar, 0.99); "Ozempic maker"→NVO (Wikidata `manufacturer` edge, 0.8, provenance=edge id); "Zuck's company"→META (person→employer edge, cap `mention_confidence ≤ 0.9`, indirect). Unresolved (baskets, novel) → escalate to S3. Every emit records `source` + matched span.

## 4. S3 LLM disambiguation + stance
`instructor` + Pydantic + provider-native structured outputs (OpenAI Structured Outputs <0.1% fail; Gemini `responseSchema`; Claude tool-schema).

**Cross-asset policy table** (given to the model as rules):
| Case | Policy |
|---|---|
| `$ETH` vs `ETHA`/`ETHE` | `$ETH`=crypto default; ETF only on explicit ETF cue/ticker; both present → separate mentions |
| `$BTC` vs `IBIT`/`FBTC` | bare=crypto; ETF only on explicit cue |
| `$MSTR` equity vs "BTC proxy" | **always equity**; BTC-proxy = `rationale` tag, not a 2nd crypto mention |
| `$COMP/$LINK/$UNI/$SOL` | crypto lexicon (chain/wallet/staking/gas/DEX)→crypto; equities lexicon (earnings/shares/EPS)→equity |
Silent context → most-liquid asset wins, cap `mention_confidence ≈ 0.7`.

**Stance (LLM, NOT FinBERT** — FinBERT fails on slang/sarcasm; LLM beats it on FPB):
1. Stance = author's positioning, not surface polarity ("$X is NOT a buy" → bearish/neutral).
2. Explicit **negation scope** + `negation_flag` + name the negated span.
3. **Sarcasm/irony** named sub-task (💀🤡, "imagine being long $X here", "great job buying the top") → invert literal + **lower `stance_confidence`**.
4. Force `neutral` (questions/news) or `unclear` (irresolvable irony) — honest abstention.

**Calibration:** self-consistency (k=3–5 samples, agreement = confidence proxy — beats raw logprobs/verbalized) + verbalized score + token logprobs where available (OpenAI/Gemini; **Claude lacks logprobs in 2026** → route stance to OpenAI/Gemini or rely on self-consistency). Then **temperature/isotonic** post-hoc per field; monitor **ECE** so θ/φ gates mean what they say.

## 5. S4 vision (image posts only)
Fires when `post.has_media and (any unresolved or no text ticker)`. Multimodal LLM via `instructor` → same `Mention` schema, `resolution_method=vision_ocr`, `rationale` quotes on-image text. Cheap pre-filter: OCR → run S1 regex over OCR text. Cap vision `mention_confidence` lower; cross-check against alias table; unknown → reject.

## 6. Multi-ticker, threads, quote-tweets
- N mentions per post (dedup by `(symbol, asset_class, option_detail)`, keep highest-conf provenance).
- **Threads:** group by `conversation_id`; a reply ("adding here") inherits the nearest-ancestor symbol (`thread_root_id`, lower confidence, `rationale="inherited"`). Root author = signal owner unless reply is a different author.
- **Quote-tweets:** parse both; tag whose. Endorsement ("this 👆")→propagate quoted stance to quoter (small haircut); contradiction ("lol no")→override/invert; empty→keep quoted as `is_quoted_signal=True`, no fresh stance. Never merge two authors' stances.

## 7. End-to-end pseudo-code
```python
def extract_mentions(post, thread_ctx) -> list[Mention]:
    mentions=[]; text=normalize(post.text)
    # S1
    for sp in find_cashtags(text)+find_option_strings(text):
        if sp.is_option: mentions.append(make_mention(sp.root,OPTION,CASHTAG_EXACT,option_detail=parse_option(sp,post.timestamp),mconf=0.9)); continue
        ok,conf = cashtag_is_real_ticker(sp.token,text)
        if ok is True:
            if is_cross_asset_ambiguous(sp.token,text): residual.append(sp); continue
            mentions.append(make_mention(sp.token, asset_class_of(sp.token), CASHTAG_EXACT if conf>0.95 else CASHTAG_DISAMBIG, mconf=conf))
        elif ok is False: continue
        else: residual.append(sp)
    # S2
    for e in gliner2.extract(text,NER_SCHEMA)["entities"]:
        hit=link_entity(e.text,e.kind)
        if hit and hit.score>=TAU_LINK and not indirect_reference(e):
            mentions.append(make_mention(hit.symbol,hit.asset_class,NER_ENTITY_LINK,mconf=hit.score,surface=e.text,source=hit.source))
        else: residual.append(e)
    # S3
    if residual or needs_stance(mentions):
        mentions = merge(mentions, llm_resolve_and_stance(text,residual,mentions,post.quoted_text,thread_ctx,CROSS_ASSET_POLICY).mentions)
    # S4
    if post.has_media and (any_unresolved(mentions) or not mentions):
        mentions = merge(mentions, vision_extract(post.media, schema=Mention))
    mentions = attribute_thread_and_quotes(mentions, post, thread_ctx)
    # S5
    for m in mentions:
        m.mention_confidence = calibrator_mention.predict(features(m))
        m.stance_confidence  = calibrator_stance.predict(features(m))
    return [m for m in mentions if m.resolution_method != REJECTED]
```
Every LLM-emitted symbol validated against the alias table (Provenance gate); unknown → `REJECTED`.

## Recommended stack
| Stage | Pick |
|---|---|
| S1 regex | hand-rolled `re` + versioned collision YAML + OSI parser |
| S2 NER | **GLiNER2** (`knowledgator/gliner2-*`), spaCy `en_core_web_trf` fallback |
| Entity→ticker | SEC EDGAR + Nasdaq/NYSE + **Wikidata** + `rapidfuzz`/FAISS |
| S3 LLM | **`instructor` + Pydantic**; OpenAI Structured Outputs / Gemini responseSchema / Claude tool-schema |
| Stance | LLM instruction-rich prompt, **not FinBERT** |
| S4 vision | multimodal LLM via instructor; OCR + regex fallback |
| Calibration | self-consistency + verbalized + logprobs → temperature/isotonic, monitor ECE |

## Sources
- GLiNER2: https://arxiv.org/html/2507.18546v1 · repo: https://github.com/urchade/GLiNER · vs LLM zero-shot: https://ubiai.tools/comparing-gliner-with-llm-zero-shot-labeling-for-named-entity-recognition/
- instructor: https://python.useinstructor.com/ · structured outputs 2026: https://logic.inc/resources/structured-outputs-guide
- Cashtag collision (Owda et al.): https://www.sciencedirect.com/science/article/pii/S0957417419301812 · crypto cashtags: https://arxiv.org/html/2312.11531v1
- OSI symbology: https://databento.com/docs/examples/options/equity-options-introduction
- FinSentLLM beats FinBERT: https://arxiv.org/pdf/2509.12638 · verbalized confidence: https://arxiv.org/html/2412.14737v2 · calibrating verbalized probs: https://arxiv.org/pdf/2410.06707
- NASDAQ+Wikidata ticker resolution: https://www.johnsnowlabs.com/finance-nlp-1-6-sec-schedules-nasdaq-and-wikidata-integration-and-much-more/
