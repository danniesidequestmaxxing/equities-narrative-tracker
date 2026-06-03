---
title: "Design: Social-Signal Analytics (sentiment, contrarian, pump)"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M2
modules: [analyze/sentiment.py, analyze/contrarian.py, analyze/pump.py]
---

# Social-Signal Analytics (2026)

**Input stream contract:** `Mention = { symbol, account_id, credibility∈[0,1], ts, stance∈{-1,0,+1}, stance_confidence∈[0,1] }`. The pump detector also needs raw account attrs (`account_age_days, follower_count, following_count, has_default_avatar, prior_mention_count`).

**Unifying principle:** these signals are weak and adversarial. Raw aggregated sentiment has near-random standalone predictive power and is gamed. Value comes from (a) credibility-weighting to denoise, (b) using extremes *contrarianly and gated on price*, (c) explicitly modeling manipulation. **None should be a standalone trigger.** All three ride a shared per-symbol event-time EWMA state.

## 1. Aggregate per-ticker sentiment
```
w_i     = credibility_i^γ · stance_confidence_i · exp(−λ·(t − ts_i))
S(s,t)  = Σ_i w_i·stance_i / (Σ_i w_i + k)      ∈ (−1,+1)
```
- **`γ` (credibility exponent, 1.5):** sharpens the credibility advantage so a few high-quality accounts aren't drowned by a swarm. First cheap defense against pumps.
- **`λ` via half-life `h`:** `λ = ln2/h`; 6–12h intraday/swing, up to 24h slower.
- **`k` (shrinkage prior, 1.0–3.0):** the most-omitted term — pulls thinly-covered tickers toward neutral. Without it a single confident post yields `S=±1.0`.

**Always emit alongside:**
```
Conf(s,t)  = Σw / (Σw + k)                 # coverage-based confidence
N_eff(s,t) = (Σw)² / Σw²                    # Kish effective sample size
```
**Require `N_eff ≥ 5`** — if 95% of weight is one account, `N_eff≈1` even at high raw count. That is *not* consensus.

**Windowing — continuous EWMA over event time** (no hard windows; smooth, O(1) incremental):
```python
on new mention i for s:
    d=exp(-λ·(ts_i-last_ts[s])); w0=cred^γ·conf
    A[s]=A[s]*d + w0*stance_i; B[s]=B[s]*d + w0; Q[s]=Q[s]*d*d + w0*w0; last_ts[s]=ts_i
read at t: d=exp(-λ(t-last_ts)); S=A*d/(B*d+k); Conf=B*d/(B*d+k); N_eff=(B*d)²/(Q*d²)
```
Keep **two horizons** (fast 3h, slow 24h); their difference is free sentiment-momentum. Clamp `Δ=max(0, ts−last_ts)` for out-of-order events.

## 2. Contrarian sentiment-extreme detection
**Not a function of level — a function of being statistically extreme vs its own history AND price disagreeing.** Euphoria persists; fade only when price stops confirming.

Operate on the slow `S` series sampled to a per-symbol ring buffer (N≈300 @ 20-min ≈ 4 days). Three detectors:

**(a) Robust z-score (MAD, not mean/σ):**
```
med=median(S_hist); MAD=median(|S_hist−med|); z_S=0.6745·(S_now−med)/MAD
```
Extreme-bullish `z_S ≥ +3.5`, bearish `≤ −3.5` (Iglewicz–Hoaglin); ±2.5–3.0 = watch tier.

**(b) Percentile gate:** `p_S ≥ 0.95` (euphoric) / `≤ 0.05` (capitulation) — distribution-free qualifier.

**(c) Rate-of-change (blow-off):** `z_ΔS` of `dS/dt`. High level + high positive velocity = euphoric blow-off; high level + decelerating = the higher-probability *fade* setup.

**Gating logic (the crux — extreme = state, not trade):**
```
Euphoria_State  = p_S≥0.95 AND z_S≥3.0 AND n_eff≥N_min
Contrarian_Short= Euphoria_State AND (sentiment_rolling_over OR bearish_price_divergence)
Contrarian_Long = Capitulation_State AND (sentiment_turning_up OR bullish_price_divergence)
```
Divergence = price new-high while sentiment/momentum lower-high. **Fire on the turn, not the level.** Size ∝ `min(|z_S|, z_cap)`. Cooldown 1× half-life.

| Param | Default |
|---|---|
| history buffer | 300 pts @ 20-min |
| robust-z fire | `|z| ≥ 3.0–3.5` |
| percentile gate | ≥0.95 / ≤0.05 |
| `N_min` | 8–15 |
| min abs sentiment | `|S| ≥ 0.3` |
| cooldown | 1× half-life |

**Fade vs real momentum discriminators:** credibility composition (high-cred-driven euphoria = real narrative, don't fade; low-cred swarm = fade safer), price confirmation, breadth/persistence (first push to 95th pct in an uptrend = continuation; Nth re-test with weakening velocity = fade). De-trend `S` (z-score the residual after the slow EWMA) to measure *abnormal*, not *trend*. Suppress in earnings/catalyst windows.

## 3. Coordinated-pump detection
Refines the plan's crude "K low-cred accounts in W" into a **multi-feature score** contrasted against a **per-ticker baseline** (measures abnormality, not popularity).

**Features (window `W=15–60m` vs baseline `B=7–30d` decayed):**
1. **Burst (Poisson surprise):** `poisson_surprise=(n_W−μ_s)/√μ_s` — **hard gate ≥4** (nothing fires without a real burst). Necessary, not sufficient (news bursts too).
2. **Credibility distribution (the discriminator):** `cred_median`, `low_cred_frac` (cred<0.2), avg credibility. Pump = bottom-heavy.
3. **New-vs-established:** `new_acct_frac` (age<30d), default-avatar frac, low-follower frac (<50), `following/followers > 5` frac, `first_time_frac`.
4. **Synchronized timing:** `inter_arrival_cv = std(Δt)/mean(Δt)` (<<1 = synchronized), `peak_concentration` (max in any 60s / n_W), `content_dup_frac` (simhash/minhash).
5. **Account overlap / cluster recurrence (strongest, slightly heavier):** co-mention graph → community detection (offline nightly); `cluster_concentration`, `known_pumper_frac`. Hardest to evade (reusing accounts is what makes a pump cheap).
6. **Target-quality prior:** low float/mcap/OTC raises prior (multiplier, not gate).

**Scoring (A: cold-start weighted logistic; B: trained classifier with labels):**
```
PumpScore = sigmoid(β0 + β1·z(surprise) + β2·low_cred_frac + β3·new_acct_frac
                   + β4·content_dup_frac + β5·(1−inter_arrival_cv) + β6·cluster_conc
                   + β7·known_pumper_frac + β8·target_illiquidity)  · 1[surprise ≥ 4]
# starts: β1≈1.0 β2≈1.5 β6≈1.5 β7≈2.0 β3≈1.0 β4≈1.2 β5≈1.0 β8≈0.5
# flag 0.70 (alert) / 0.85 (act)
```
(B) Random-forest / GBT on the same vector reaches ~0.94 F1 in the P&D literature, flags within tens of seconds. **Feed PumpScore back into §1 as a `(1−PumpScore)` downweight** so a detected pump can't masquerade as bullish consensus.

| Param | Default |
|---|---|
| window `W` | 20–30m (+5-min fast lane) |
| baseline `B` | 14–30d, EWMA half-life ~7d |
| `z_burst_min` | 4.0 |
| `c_low` / `A_new` / `F_min` | 0.2 / 30d / 50 |
| flag / act | 0.70 / 0.85 |
| cluster recompute | nightly offline; O(1) lookup online |

**Genuine narrative vs pump:**
| Signal | Real narrative | Pump |
|---|---|---|
| Credibility mix | includes credible accounts | bottom-heavy |
| Account age | established | fresh, default avatars |
| Account overlap | diverse, low concentration | recurring cluster |
| Content | varied, primary sources | copypasta |
| Timing | organic ramp | synchronized "go" burst |
| Target | any cap, often liquid | low-float / OTC |
| Aftermath | persists | spike → fast dump |

## Cross-cutting
- **Never a standalone trigger.** §1 = denoised feature; §2 = contrarian/risk gate requiring price confirmation; §3 = veto/avoid filter. Trust §1 bullishness *only if* §3 says "not a pump" and §2 says "not a blow-off extreme."
- **Calibrate then monitor drift** — tactics shift fast.
- **Shared incremental aggregator** (sentiment, volume, baseline EWMA) built once.

## Sources
- Sentiment aggregation / decay: https://arxiv.org/html/2504.10078v1 · https://www.shadecoder.com/topics/social-media-sentiment-for-trading-a-comprehensive-guide-for-2025 · EWMM: https://arxiv.org/html/2404.08136v1
- Contrarian extremes: https://research.lighthousemacro.com/p/sentiment-and-positioning-the-contrarian-216 · Levkovich Panic/Euphoria: https://sentimentrader.com/blog/paniceuphoria-model-is-an-indicator-worth-watching · modified z-score: https://www.statology.org/modified-z-score/
- Pump detection: https://www.mdpi.com/1999-5903/15/8/267 · https://dl.acm.org/doi/abs/10.1145/3561300 · https://arxiv.org/pdf/2412.18848 · CIB: https://arxiv.org/html/2410.22716v2 · Kleinberg bursts: https://nikkimarinsek.com/blog/kleinberg-burst-detection-algorithm
