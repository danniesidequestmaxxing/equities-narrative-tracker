---
title: "Design: Credibility Scoring + Multi-Account Attribution"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M4
modules: [score/credibility.py, score/attribution.py, score/benchmark.py]
---

# Credibility Scoring + Multi-Account Attribution (2026)

The feedback-loop core. Everything reduces to a **pure recomputation** over the closed-outcome set `C(T) = { o : o.closed_at ≤ T }`.

## 0. Data model & primitives

Per closed call `c`: `closed_at`; `R_c` (R-multiple = realized P&L ÷ initial risk); `dir_c ∈ {+1,−1}`; `bench_R_c`; `R⊥_c = R_c − β_a·bench_R_c` (benchmark-neutral); `contribs_c` = list of `(account a, stance s_{a,c} ∈ {+1,−1}, conf_{a,c}, mention_time)`; `pump_c ∈ [0,1]`.

**Two signals per account, kept separate** (collapsing them is the most common error):
- **hit** = `1[R⊥_c > 0]` → Bayesian win-rate (robust, bounded, great at small n).
- **magnitude** = `R⊥_c` → expectancy (captures fat right tails). `Expectancy = p·AvgWin_R + (1−p)·AvgLoss_R` is what compounds capital.

## 1. Credibility estimator — shrinkage expectancy × Beta-Binomial reliability gate × magnitude lower bound

Against the alternatives: raw expectancy = catastrophic variance at small n; Wilson LB = binary-only (keep as secondary tiebreak); Bayesian win-rate = right hit-rate but win-rate ≠ profit; empirical-Bayes shrinkage = right *framework*, apply it to expectancy.

**1a. Win-rate (Beta-Binomial, EB prior):**
```
p̂_a = (α₀ + Σ w_i·hit_i) / (α₀ + β₀ + Σ w_i)
# Method-of-moments across population: μ,σ² of per-account win rates
K = α₀+β₀ = μ(1−μ)/σ² − 1     ;   α₀ = μK , β₀ = (1−μ)K
# clamp K ∈ [20,60]. New account starts at p̂ = μ.
```

**1b. Magnitude (shrinkage expectancy / James-Stein):**
```
E_a = Σ w_i R⊥_i / Σ w_i ;  n_a = Σ w_i
Ê_a = E₀ + (n_a/(n_a + k_E))·(E_a − E₀)
k_E = σ²_within/σ²_between   (default k_E = 10). New account: Ê = E₀ ≈ 0.
```

**1c. Magnitude-aware lower bound (Lo Sharpe SE with skew/kurtosis):**
```
SR_a   = Ê_a / sd(R⊥)
SE(SR) = sqrt( (1 − κ·SR + (γ−1)/4·SR²) / n_a )   # κ=skew, γ=kurtosis
SR_lo  = SR_a − z·SE(SR)        # z=1.0 default (~84% one-sided); 1.64 = 95%, punitive
Êlo_a  = SR_lo · sd(R⊥)
```
Skew/kurtosis terms matter because R-multiple distributions are right-skewed and fat-tailed — naive SE understates exactly for lottery-ticket accounts.

**1d. Final score:**
```
Cred_a(T) = g(p̂_a) · max(Êlo_a, 0) · ReliabilityGate_a + floor
ReliabilityGate_a = n_a/(n_a + m)   # m≈5
g(p̂_a) = p̂_a^θ                      # θ≈0.7, mild consistency premium
```
`max(Êlo_a,0)`: no demonstrated edge → contributes at floor, never negative weight (negative info handled by attribution sign, §3). Monotone, bounded, graceful at n→0.

## 2. Time decay
```
w_decay(o;T) = 2^(−(T − o.closed_at)/H) ≡ exp(−λΔt), λ = ln2/H
```
Pure function of `(closed_at, T)` → exact recomputation at any T. **Half-life by horizon:** intraday 21d, **swing 90d**, **position 180–270d**. *(This project chose Longer/position → use H≈180d.)* Guardrails: cap lookback ~3·H; keep an undecayed `n_raw` and gate on both `n_eff` and `n_raw ≥ 8`.

## 3. Multi-account attribution — credibility × stance-confidence × recency, signed alignment

Shapley is the "correct" ideal but needs a coalition value function you don't have and is O(2^N); the weighted scheme below is its linear approximation, O(N).
```
raw_i = Cred_i(T) · conf_{i,c} · 2^(−Δmention_i/Hₐ) · align_{i,c}
align_{i,c} = +1   if stance == dir_c
            = −η   if stance == −dir_c   (η ∈ [0,1], default 1)
```
Normalize **within agreers and opposers separately**:
```
A = Σ_{aligned} raw_i ;  D = Σ_{opposed} |raw_i|
attr_i =  R⊥_c·(raw_i/A)      for aligned
attr_i = (−R⊥_c)·(|raw_i|/D)  for opposed   # inverse outcome
```
- Opposed account + call wins → −share (correctly drops). Opposed + call loses → +share (correctly faded). **An account is scored on the trade it advocated, not the trade the system took** → the system *learns from contrarians*.
- Cold-start fallback (Cred undefined for all): `raw_i = conf_i·2^(−Δmention/Hₐ)` (equal-ish).
- **Double-count guard:** same account multiple mentions pre-close → collapse to one contributor (max conf, earliest mention).

## 4. Coordinated-pump down-weighting (asymmetric)
Detector emits `pump_c` and per-(account,call) `coord_{a,c}`.
```
trust_{a,c} = 1 − coord_{a,c}
attr'_i = attr_i · trust   if attr_i > 0      # damp suspect wins
attr'_i = attr_i           if attr_i ≤ 0      # FULL blame survives
w_i → w_i · trust_{i,c}                        # also damp decay weight feeding §1
```
Manufacturing a coordinated burst to ride a self-pop caps upside but not downside → **−EV to game**. Persistent-actor escalation: standing multiplier `(1 − coord_rate_a)` on `Cred_a`. Keep `coord` continuous, not a binary ban.

## 5. Benchmark-relative scoring
```
R⊥_c = R_c − β_a·bench_R_c        # β_a default 1.0; SPY for equities, BTC for crypto
bench_R_c signed by dir_c
```
A short that profits while SPY rallied is rewarded; a long that won only on a market gap gets `R⊥≈0`. Free taxonomy: `corr(R_a, bench_R)≈1` ⇒ beta proxy (down-weight); ≈0 with `Ê⊥>0` ⇒ genuine selection alpha. Everything downstream operates on `R⊥` → benchmark-relativity is structural.

## 6. Pure recomputation + closure-time invariant (reference)
```python
PARAMS = dict(H_decay=180, K_clamp=(20,60), k_E=10, z=1.0, m_reliab=5,
              N_min=8, Ha=3, eta=1.0, theta=0.7, floor=1e-3)

def recompute_credibility(all_calls, T, P=PARAMS):
    closed = [c for c in all_calls if c.closed_at and c.closed_at <= T]   # INV-3 filter
    if not closed:
        return defaultdict(lambda: P['floor'])
    for c in closed:
        c.R_perp = c.R - beta_of(c.ticker, asof=c.closed_at) * c.bench_R_signed
    mu, var = pop_winrate_moments(closed)
    K = clamp(mu*(1-mu)/var - 1, *P['K_clamp']); a0, b0 = mu*K, (1-mu)*K
    E0 = pop_mean_expectancy(closed)

    acct_samples = defaultdict(list)
    for c in closed:
        contribs = dedupe_to_one_per_account(c.contribs)
        raw = {}
        for x in contribs:
            fm = 2 ** (-(days(x.mention_time, c.open_time))/P['Ha'])
            algn = +1 if x.stance == c.dir else -P['eta']
            raw[x.acct] = max(cred_at(x.acct, c.open_time), P['floor']) * x.conf * fm * algn
        A = sum(v for v in raw.values() if v > 0); Dn = sum(-v for v in raw.values() if v < 0)
        for x in contribs:
            r = raw[x.acct]
            if r >= 0 and A > 0:  attr = c.R_perp*(r/A)
            elif r < 0 and Dn > 0: attr = (-c.R_perp)*(-r/Dn)
            else: continue
            trust = 1 - coord_membership(x.acct, c)
            if attr > 0: attr *= trust
            w = 2**(-(days(c.closed_at, T))/P['H_decay']) * trust
            acct_samples[x.acct].append((attr, w))

    cred = {}
    for acct, s in acct_samples.items():
        sw = sum(w for _, w in s); nraw = len(s)
        if nraw < P['N_min'] or sw <= 0: cred[acct] = P['floor']; continue
        wins = sum(w for r, w in s if r > 0); p_hat = (a0+wins)/(a0+b0+sw)
        E_a = sum(r*w for r, w in s)/sw; E_sh = E0 + (sw/(sw+P['k_E']))*(E_a-E0)
        sd = weighted_std(s) or 1e-6; SR = E_sh/sd
        kap, gam = weighted_skew(s), weighted_kurt(s)
        SE = sqrt(max(1 - kap*SR + (gam-1)/4*SR*SR, 1e-9)/sw); E_lo = (SR - P['z']*SE)*sd
        gate = sw/(sw+P['m_reliab']); coord_pen = 1 - coord_rate(acct, s)
        cred[acct] = (p_hat**P['theta']) * max(E_lo, 0.0) * gate * coord_pen + P['floor']
    return defaultdict(lambda: P['floor'], cred)
```
**INVARIANT:** `Cred(account, T)` is a pure function of `{closed_at ≤ T}`. No mutable running state; `Recompute(T1)` and `Recompute(T2)` independent; replay in any order yields identical Cred for the same T.

### Edge cases
| Case | Handling |
|---|---|
| Account suspended mid-trade | Irrelevant — score is a function of closed outcomes, not status. Apply liveness flag only at signal-generation, never edit history |
| Never-closed / expired calls | Not in C(T). **Auto-close at horizon** (book mark-to-market R) so ghosting a loser realizes it |
| One account, many simultaneous calls | Each is an independent sample; pump/coord trust catches synchronized bulk; optionally cluster same-window calls, weight `1/√k` |
| Single contributor, single call | Gets 100% R⊥, but `nraw<N_min` → sits at floor (no lucky one-shot elevation) |
| All contributors below floor | Effectively equal-weight (documented fallback) |
| Degenerate variance / div-zero | sd/A/D epsilon-guarded; `max(E_lo,0)`; sqrt arg floored |

## Parameter cheat-sheet (position-horizon defaults)
| Symbol | Role | Default |
|---|---|---|
| H | decay half-life | **180d** (position) |
| K | win-rate prior strength | clamp [20,60] |
| k_E | expectancy shrinkage | 10 |
| z | lower-bound aggressiveness | 1.0 |
| m | reliability gate | 5 |
| N_min | min undecayed samples | 8 |
| Hₐ | first-mover half-life | 3d |
| η | opposite-stance penalty | 1.0 |
| θ | win-rate tilt | 0.7 |

## Sources
- R-multiples/expectancy: https://traderlion.com/risk-management/r-and-r-multiples/ · https://www.pnlledger.com/expectancy-r-multiples-the-plain-english-guide/
- Empirical-Bayes shrinkage: http://varianceexplained.org/r/empirical_bayes_baseball/ · https://m-clark.github.io/posts/2019-06-21-empirical-bayes/ · https://www2.stat.duke.edu/~pdh10/Teaching/732/Notes/shrinkage.pdf
- Wilson LB: https://www.evanmiller.org/how-not-to-sort-by-average-rating.html
- Sharpe SE w/ skew+kurtosis (Lo): https://www.researchgate.net/publication/228139699_The_Statistics_of_Sharpe_Ratios
- Time-decay: https://adriennevermorel.com/notes/time-decay-attribution-model/ · Shapley MTA: https://www.treasuredata.com/blog/multi-touch-attribution-mta-with-shapley-values-tells-marketers-what-works-best
- Coordinated behavior (2025): https://dl.acm.org/doi/10.1145/3696410.3714698
