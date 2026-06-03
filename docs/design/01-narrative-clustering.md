---
title: "Design: Narrative Clustering Layer"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M2
modules: [analyze/narratives.py, analyze/discovery.py, analyze/sentiment.py, analyze/digest.py]
---

# Narrative Clustering Layer — Implementation-Ready Design (2026)

**Scope:** the M2 "ANALYZE → narrative" subsystem. Consumes `ticker_mentions` (joined to `instruments`), populates `narratives`, `narrative_members`, and the point-in-time `narrative_snapshots`. Honors **INV-2** (every recommendation input reproducible as-of issuance) and **INV-3** (credibility is a closure-time pure function). Reuses shared thresholds (`θ_call=0.75`, pump `K=3 / W=30m`, `ρmax=0.60`).

**Central design decision:** do **not** re-cluster from scratch each tick. At dozens of tickers/day the hard problem is keeping group *identities* stable. Architecture = **a slow-moving set of anchor narratives (a controlled vocabulary) + a fast, cheap per-tick assignment of instruments to those anchors.** Full re-clustering is a nightly maintenance job, not the hot path.

## 1. Clustering approach — hybrid, split by cadence

| Layer | Engine | Cadence | Job |
|---|---|---|---|
| **Assignment (hot path)** | Embedding nearest-anchor + threshold | Every analyze tick + 15-min heartbeat | Map each active instrument to an existing narrative, or mark `unassigned` |
| **Discovery + labeling (cold path)** | HDBSCAN over unassigned/active → one LLM call to name new clusters + propose merges | Nightly (and on-demand when `unassigned` ≥ 8) | Spawn/rename/merge narratives, regenerate anchor embeddings |

**Why:**
- A vector index (pgvector HNSW) earns its keep at 10^5–10^9 vectors. You have dozens of live instruments and 10–40 narratives → a brute-force cosine scan over anchors is microseconds and exact. **Do not put narrative assignment behind an ANN index.**
- A single LLM "group these into narratives" call is genuinely better than a vector index when the whole candidate set fits in one prompt (it does, at this scale). But LLM non-determinism + label thrash + cost mean it is confined to the **nightly discovery/naming** role, at **temperature 0** with a **pinned model + JSON-schema-constrained output**.
- Embeddings do the per-tick work because they're deterministic, ~$0.00002/instrument, and reproducible (INV-2).

### Embedding model
**Primary: OpenAI `text-embedding-3-small`** at 1536-d, or truncated to **768-d** via the `dimensions` param (Matryoshka) to halve pgvector storage. $0.02/1M tokens; near-top MTEB clustering for short text. Already in the stack via LiteLLM.

**What you embed is an "instrument context document," not the raw tweet:**
```
{canonical_name} ({symbol}, {asset_class}) — {sector/theme tags} —
recent context: "{3–5 most-recent mention snippets, stance-tagged, last 72h}"
```
Cache the embedding keyed on `(instrument_id, rounded-hour, hash(context))`.

### Assignment algorithm (hot path)
```
for each active instrument i (≥1 mention in L_active = 72h):
    e_i  = embed(context_doc(i))                      # cached
    sims = {n: cosine(e_i, n.embedding) for n in active_narratives}
    n*, s* = argmax sims
    if s* >= T_assign (0.62):
        upsert narrative_members(narrative_id=n*, instrument_id=i, weight=w_i, as_of=as_of)
    elif s* >= T_ambiguous (0.50):
        keep_prior_or_assign(i)                       # hysteresis
    else:
        mark i UNASSIGNED                             # nightly discovery candidate
```
An instrument may attach to >1 narrative if multiple anchors clear `T_assign` (one `narrative_members` row each).

## 2. Narrative identity over time (stability)

Identity lives in the durable `narratives.id`/`label`, not in clustering output.

- **Anchor embedding = slow-moving identity:** `n.embedding ← normalize((1-α)·n.embedding + α·centroid(members))`, `α=0.15`, **nightly only**. The hot path reads anchors, never mutates them.
- **Assignment hysteresis:** a ticker in N stays in N while `cosine(i,N) ≥ T_assign − δ` (`δ=0.05`); only switch when a challenger beats the incumbent by margin `m=0.04` AND clears `T_assign`. (Schmitt-trigger; kills per-tick flapping.)
- **Spawn (nightly only):** new narrative iff HDBSCAN finds ≥ `N_min=3` distinct instruments among UNASSIGNED, persisting ≥ 2 consecutive runs, with centroid > `T_distinct=0.30` cosine-distance from every anchor.
- **Merge:** if two anchors `cosine ≥ T_merge=0.80` AND member Jaccard ≥ 0.5 for 2 runs → keep the **older** id (identity wins), repoint members, `MERGED_INTO` in `audit_log`, keep dead label as alias.
- **Split:** members form two sub-clusters with intra-distance > `T_split=0.35`, each ≥ `N_min`, for 2 runs → larger/older-weighted side keeps id+label; smaller spawns new.

All spawn/merge/split happen **only nightly with persistence ≥ 2** → labels never churn intraday.

| Symbol | Meaning | Start |
|---|---|---|
| `T_assign` / `T_ambiguous` | min cosine to join / boundary lower edge | 0.62 / 0.50 |
| `δ` / `m` | hysteresis / switch margin | 0.05 / 0.04 |
| `T_distinct` | min cosine-distance to spawn | 0.30 |
| `T_merge` / `T_split` | merge / split thresholds | 0.80 / 0.35 |
| `N_min` / persistence | min instruments / consecutive runs | 3 / 2 |

## 3. Momentum state — rising / peaking / fading

Per narrative, from the **credibility-weighted mention stream** of its members. Dual-EWMA velocity + acceleration + price-confirmation overlay.

1. **Heat** (Δ=1h bucket): `H_n(t) = Σ cred_w(account) · q(mention)` (`q` damps coordinated/duplicate mentions).
2. **Dual EWMA → velocity:** fast `N_f=6h`, slow `N_s=24h`; `velocity = EWMA_fast − EWMA_slow`; `norm_velocity ν = velocity/(EWMA_slow+ε)` (scale-free).
3. **Acceleration:** EWMA-smoothed `Δν` (`N_a=3h`).
4. **Robust burst z-score:** `z = 0.6745·(H − median_7d)/(MAD_7d+ε)`; require `z ≥ z_min=2.0` (noise floor).
5. **Price confirmation:** credibility-weighted member-basket return > `r_min` (e.g. +1%/day, +3%/3d).
6. **State machine (2-bucket dwell):**
```
RISING  : ν ≥ ν_hi(0.20) AND α > 0      AND z ≥ 2.0
PEAKING : ν ≥ ν_hi        AND α ≤ 0      (velocity high, rolling over)
FADING  : ν < ν_lo(0.05)  OR (α < −0.05 AND ν declining 2+ buckets)
DORMANT : z < 2.0 for ≥ 24h
```

**15-min heartbeat** re-runs momentum with the current clock so quiet narratives decay to fading/dormant even with no new posts.

## 4. Re-cluster cadence & point-in-time reproducibility

| Job | Trigger | Does |
|---|---|---|
| Assignment + momentum (hot) | every tick + 15-min heartbeat | assign; recompute EWMAs/state; **write a `narrative_snapshots` row per narrative** |
| Snapshot-on-issuance | when a recommendation forms | freeze the narrative state that fed the call (INV-2) |
| Discovery/labeling (cold) | nightly 04:00 ET + on-demand (UNASSIGNED ≥ 8) | HDBSCAN + LLM name/merge/split; update anchors α=0.15 |

**Reproducibility (INV-2):** pin one `as_of = ingested_at` per run; `narrative_members`/`narrative_snapshots` **append-only**; snapshot load-bearing values onto the recommendation; LLM confined to nightly identity ops **persisted as data** → replay reads stored snapshots, never re-invokes the LLM. Store embedding-model id + dim per snapshot so a model swap doesn't silently break comparability.

## 5. Credibility-weighting

Reuse `accounts.credibility_score` + point-in-time `account_scores` (closure-time read, INV-3). Do not invent a parallel score.
```
cred_w(account, T) = w_floor + (1 − w_floor)·shrink(s, n)
s = account_scores.decayed_score as-of T   # closure-time correct
shrink(s,n) = s · n/(n + k)                 # empirical-Bayes shrinkage, k=10
w_floor = 0.10
```
- Shrinkage (`k=10`): a new account with one lucky call can't dominate heat.
- Tier prior before any outcomes: HOT→0.6, WARM→0.35, COLD→0.15, blended out as `sample_size` grows.
- **Never use follower count / engagement** — edge is realized-outcome credibility.
- **Coordinated-pump damping** `q(m)`: if ≥ `K=3` distinct low-cred (`cred_w<0.25`) accounts mention a symbol within `W=30m`, cap the burst (`q≈0.3`, counts as ~one mention).
- `narrative_members.weight` = instrument's share of credibility-weighted heat within the narrative.

## 6. Edge cases

| Edge case | Handling |
|---|---|
| Ticker in two narratives ($TSLA = AI + EV) | Allowed (many-to-many); `ρmax=0.60` correlation gate still prevents two calls that are the same bet |
| One-off mention | Fails `N_min` + `z≥2.0` floor → stays UNASSIGNED, decays after 72h |
| Narrative splits | Larger/older side keeps id+label+track record; smaller spawns new |
| Two narratives converge | Merge: older id survives, dead label aliased |
| Boundary flapping | Hysteresis + switch margin + nightly-only identity changes |
| Coordinated pump inflates heat | `q(m)` + `cred_w` shrinkage cap the burst |
| Quiet narrative never fades | 15-min heartbeat decays EWMA heat |
| Model/version swap | Embedding model id+dim stored per snapshot; re-embed is an explicit migration |
| Reproducing a past call | Read `narrative_snapshots` + `narrative_members` as-of T + `account_scores` (closure-time) |

## Build order within M2
1. Embedding + context-doc + assignment to a **manually seeded** set of 8–10 anchor narratives.
2. Momentum EWMAs + state machine + heartbeat + `narrative_snapshots` writes. *(Working credibility-weighted narrative map after this step.)*
3. Nightly HDBSCAN discovery + LLM naming.
4. Merge/split + hysteresis hardening.

**Calibration before go-live:** hand-label one representative trading day, grid-search `T_assign`/`T_ambiguous`/`ν_hi`/`ν_lo`/`z_min` (cosine scales shift with embedding model + context-doc format).

## Sources
- MTEB leaderboard (Mar 2026): https://awesomeagents.ai/leaderboards/embedding-model-leaderboard-mteb-march-2026/ · OpenAI text-embedding-3-small: https://developers.openai.com/api/docs/models/text-embedding-3-small
- HDBSCAN: https://hdbscan.readthedocs.io/en/latest/comparing_clustering_algorithms.html · scikit-learn clustering: https://scikit-learn.org/stable/modules/clustering.html
- PRISM (LLM-guided semantic clustering, 2026): https://arxiv.org/pdf/2604.03180 · ClusterFusion: https://arxiv.org/pdf/2512.04350 · Human-interpretable short-text clustering w/ LLMs: https://royalsocietypublishing.org/doi/10.1098/rsos.241692
- EWMA trend/anomaly detection: https://ieeexplore.ieee.org/document/7729882/ · Burst detection: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3915237/
- pgvector HNSW: https://github.com/pgvector/pgvector · finfluencer quality framework: https://link.springer.com/article/10.1057/s41264-025-00334-7
