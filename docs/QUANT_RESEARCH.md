# Quantitative research and method knowledge

Research prioritizes the latest five years. Older observations may initialize
rolling features, but do not belong in the scored evaluation window. The policy
in `config/research/policy.json` records the return target, cost assumptions and
risk requirements. A 20% net annualized return is a research target, not an
observed result or an automatic promotion rule.

## Knowledge and evidence

`config/research/methods.json` contains source-backed method cards. Each separates
the original finding from our adaptation, required inputs, tests and limitations.
The first collection covers nonlinear characteristics, volatility budgets,
transaction costs, value/momentum, directed economic links, selection bias and
multi-window price/volume tree models.

Apply `database/migrations/quant_method_library_v1.sql` with the database migration
role, then register the reviewed catalog using the configured database account:

```bash
python apps/finagent/scripts/analytics/quant_method_library.py \
  --catalog config/research/methods.json --check
python apps/finagent/scripts/analytics/quant_method_library.py \
  --catalog config/research/methods.json
```

Versions are append-only. `registered_at` controls the knowledge cutoff; a source's
publication year does not mean the local Agent knew it at that date. The Agent's
`finance_research_intelligence` supports `scope=methods` and an optional literal
`method_query`. `scope=experiments` retrieves local result rows with explicit units,
methodology and filters for model, allocation, cohort, cost, horizon, year,
`initial_cash` (CNY) and requested `holdings`. Compare results at matched capital;
different account sizes can have different rounding and liquidity constraints.
Methods and offline results do not become approved forecasts merely by entering SQL.
Experiment tool pages carry shared project methodology once in the catalog
metadata. Each citable row retains units for its own metrics, year-specific
notes, its gate status and a reference to matching shared methodology. Unique
notes remain on the row. Wide pages can still omit duplicate top-level rows to
fit the budget; consumers use complete citation-bound values. Acceptance checks
their expected count, exact metrics/units and EvidenceStore ingestion against
SQL, not the presence of a second duplicate copy in `rows`.

Use `research_asset_registry.py --project PROJECT_NAME` to register one completed
project. Put `EXPERIMENT_CONTRACT.json` at the project root, with artifact paths
relative to that root. Arrays, source archives and weights stay on controlled
storage; small result tables enter SQL. The registry skips dependency folders and
does not follow directory symlinks.

## Reproducible experiments

Create a separate research environment; production Agent processes need neither
LightGBM nor a GPU:

```bash
python -m pip install -c config/research.lock -e '.[research]'
python apps/finagent/scripts/analytics/run_quant_cycle.py \
  --dataset .local/research/market \
  --forecasts .local/research/forecast-features.npz \
  --output .local/research/ridge-cycle
python apps/finagent/scripts/analytics/run_quant_tree_cycle.py \
  --dataset .local/research/market \
  --forecasts .local/research/forecast-features.npz \
  --reference-predictions .local/research/ridge-cycle/PREDICTIONS.npz \
  --output .local/research/tree-cycle
python apps/finagent/scripts/analytics/verify_quant_cycle.py \
  --experiment .local/research/tree-cycle \
  --report .local/research/tree-verification.json
```

The runners derive evaluation folds from the supplied immutable panel's calendar.
Existing archives retain their original windows. Input dates, securities and
forecast dimensions must agree. A new period or policy requires a new protocol
and output directory; completed outputs cannot be resumed or overwritten.
When a panel manifest declares `primary_since`, older rows may initialize rolling
features only. Both linear and tree learners exclude those rows from supervised
training, and the execution simulator rejects evaluation before that boundary.
An early fold with insufficient recent training history fails explicitly.
Default evaluation years come from the frozen calendar and the shared purged
training/validation requirements; they are not fixed to particular years.
For a primary window beginning in late 2021 and ending in October 2026, this
admits 2023–2026. The 2026 return is labeled as a partial-year return and cannot
count toward the required positive complete years. The independent verifier
recomputes that gate and baseline advantages from the saved NAV paths.

The linear cycle tests price, interaction and forecast-availability features.
The tree cycle uses 72 features when valuation/EPS inputs are supplied, two tree
targets and a fixed blend of model ranks. Every test year trains on previous
years only. Label expiry is purged before both validation and test boundaries;
tree iteration counts are selected on historical validation, never the test year.
Reserve-stock outcomes do not enter training labels or their normalization.

Both cycles report all predeclared candidates and benchmarks at 15 and 30 basis
points per side. The cash/share ledger includes fees, corporate actions,
receivables, turnover, stale marks and incomplete fills. Annualized return uses
ACT/365.25 calendar time; Sharpe uses a separately stated 252-session convention.
Bootstrap intervals are exploratory and do not correct for repeated strategy search.

Risk experiments can explicitly set `vol_estimator="portfolio"`. This estimates
the planned stock basket from at most 60 past sessions, requires 40 complete
observations, and mixes sample covariance with 25% diagonal covariance. These
are configurable research parameters, not an implementation of optimal
Ledoit–Wolf shrinkage. The simulator records the estimate and usable observations
at each risk decision; unavailable history produces no new target exposure.
The existing market-volatility estimator remains available as a comparison.
Unfilled orders can make actual holdings differ from the planned risk budget.

### Fixed-prediction portfolio comparisons

`config/research/portfolio.json` declares capital levels, membership scopes and
allocation policies. `run_portfolio_cycle.py` reuses a completed tree cycle's
predictions and training records. It compares 40/100/200 holdings, equal and
inverse-volatility weights, and market/basket risk estimates without refitting
the model. Each capital level gets same-capital benchmarks; `all` includes the
full frozen universe and is an operational projection, not a new holdout.

```bash
python apps/finagent/scripts/analytics/run_portfolio_cycle.py \
  --dataset .local/research/market \
  --reference-experiment .local/research/tree-cycle \
  --capital 1000000 \
  --output .local/research/portfolio-1000000
python apps/finagent/scripts/analytics/verify_quant_cycle.py \
  --experiment .local/research/portfolio-1000000 \
  --report .local/research/portfolio-1000000-verification.json
```

Run the second declared capital in a different output directory. A study must
retain every declared configuration, including failed or unprofitable ones.
The independent verifier checks actual starting cash, policy cost, requested
holdings and selected-symbol membership, as well as returns and frozen source
records. The broad equal-weight benchmark uses fractional shares as an index
reference; model accounts retain round-lot constraints. More holdings, lower
capital or a more complex risk estimator need measured benefits under the same
costs; none implies better returns by itself.

### Rolling model updates within the recent window

`config/research/adaptation.json` declares quarterly tree-blend refits, monthly
ridge refits, and unchanged annual controls. Each refit uses at most the preceding
three years within the five-year primary window. The optional 252-session
half-life changes training-loss weights; every date first receives equal total
influence regardless of its number of eligible stocks. Validation retains equal
date weights, and ridge transforms use only training observations.

The outer training set excludes labels that expire on or after the first test
open. Tree early stopping uses the last 63 eligible historical dates with a
second label purge before that validation block. A new model owns the preceding
close's signal for its first open. Consecutive prediction blocks neither overlap
nor leave gaps. Fold checkpoints retain parameters, model hashes, training
boundaries and prediction hashes; incomplete or altered checkpoints require
review rather than silent reuse.

```bash
python apps/finagent/scripts/analytics/run_adaptation_cycle.py \
  --dataset .local/research/market \
  --forecasts .local/research/forecast-features.npz \
  --reference-experiment .local/research/tree-cycle \
  --study-config config/research/adaptation.json \
  --output .local/research/adaptation
python apps/finagent/scripts/analytics/verify_adaptation_cycle.py \
  --experiment .local/research/adaptation \
  --dataset .local/research/market \
  --reference-experiment .local/research/tree-cycle \
  --report .local/research/adaptation-verification.json
python apps/finagent/scripts/analytics/run_portfolio_cycle.py \
  --dataset .local/research/market \
  --reference-experiment .local/research/adaptation \
  --study-config config/research/adaptation.json \
  --capital 1000000 \
  --output .local/research/adaptation-1000000
python apps/finagent/scripts/analytics/verify_quant_cycle.py \
  --experiment .local/research/adaptation-1000000 \
  --report .local/research/adaptation-1000000-verification.json
```

Repeat the portfolio stage at the second declared capital. The default study has
six scores, three allocations, three membership scopes and two cost levels:
120 replays per capital, including benchmarks. Retain all alternatives. Compare
each fresh model to its same-family annual control, and recency weighting to its
uniform-weight rolling counterpart. The rolling predictor verifier independently
reconstructs training-date ownership and maturity. Its daily rank correlations
use raw realized returns and disclose missing matured labels; overlapping labels
make these diagnostics different from executed portfolio returns.

This is a rolling-refit and loss-weighting experiment motivated by distribution
change. It does not implement the data/model meta-adapters in
[DoubleAdapt](https://arxiv.org/abs/2306.09862). The
[author implementation](https://github.com/SJTU-DMTai/DoubleAdapt) also discusses
label maturity and limitations of its online update interval. Historical
comparisons already inspected during research remain exploratory.

Opening orders use only prior-session liquidity and an observed open. The legacy
`ex_post_capacity_breach` counter compares filled notional with observed turnover
or its conservative daily-low lower bound. Exceeding that lower bound alone does
not prove a breach of share-volume participation. Current security-master flags, historical trading states and
corporate-action revisions still require independent checks. Arithmetic
verification does not certify these inputs or market execution.
When a missing historical price is recovered, subsequent total-return factors
inherit that input's availability time. A late ex-date price cannot change a
factor while leaving its system-knowledge boundary in the past. The PostgreSQL
revision fixture tests this propagation using temporary tables and rollback.

## Fill evidence and account attribution

`run_execution_audit.py` replays the declared annual-model controls from a
completed adaptation experiment. It refuses to overwrite its output directory
and compares every legacy cashbook field against the frozen account. No order,
signal or fee changes are permitted during this diagnostic replay.

```bash
python apps/finagent/scripts/analytics/run_execution_audit.py \
  --dataset .local/research/market \
  --reference .local/research/adaptation \
  --output .local/research/execution-audit
```

Optional `backtest(..., keep_fills=True)` records individual simulated fills,
held corporate actions and terminal shares. The independent audit distinguishes
share participation, observed-notional participation, lower-bound exceedances,
missing volume and inconsistent amount/volume/range evidence. Canonical volume
is in lots of 100 shares. Current-day outcome fields classify completed simulated
fills only; they never affect an opening order. All legacy counters remain
unchanged for reproducibility. Daily-volume compatibility cannot establish
opening-auction capacity, queue priority or actual execution prices.

Asset P&L reconciles purchases, sales, fees, net dividend entitlements and
terminal marked positions to the NAV change. Dividend receivables count once at
entitlement, not again on settlement. The final liquidation fee is a provision,
not an executed sale, and stale terminal marks remain explicit. Results across
capital, cost and allocation settings are correlated comparisons, not separate
independent trials. The fixed five-year input window is unchanged.

## Fixed turnover-rule comparisons

`config/research/turnover.json` freezes comparisons on the existing annual
linear/tree predictions: unchanged execution, a 25% relative target band,
50% partial adjustment, and a wider incumbent rank buffer with the same band.
The complete grid includes both capital levels, three portfolio constructions,
three security scopes and two costs. Both references remain at matched capital
and costs; all results are retained. It does not retrain on observed test returns.

```bash
python apps/finagent/scripts/analytics/run_portfolio_cycle.py \
  --dataset .local/research/market \
  --reference-experiment .local/research/adaptation/predictions \
  --capital 1000000 \
  --study-config config/research/turnover.json \
  --output .local/research/turnover-capital-1000000
python apps/finagent/scripts/analytics/verify_quant_cycle.py \
  --experiment .local/research/turnover-capital-1000000 \
  --report .local/research/turnover-capital-1000000-check.json
```

`adjustment_fraction` moves shares toward each positive target at a fixed rate.
`relative_no_trade_band` suppresses changes within the chosen fraction of target
shares. Zero targets exit fully. Reductions needed to meet the selected gross
allocation or concentration cap bypass both rules; increases share the remaining
gross budget. Liquidity, opening-gap guards and lots still constrain fills.
Partial entry can leave cash and changes exposure, so a lower fee alone does not
establish better performance. Risk is estimated on the selected target basket;
actual weights and unfilled reductions can differ.

`execution_audit=true` adds fill evidence and independent account attribution
to every replay. The verifier checks allocation parameters, cost/turnover and
capacity summaries against the saved policy and fill records. Prior control
comparisons omit only the new default fraction/band when absent in old metadata;
non-default values and changes to any legacy cashbook field fail.

The method catalog records
[Gârleanu and Pedersen's dynamic trading model](https://nbgarleanu.github.io/DynTrad.pdf)
and its [corrigendum](https://nbgarleanu.github.io/DynTrad_corrigendum.pdf).
The implemented rules are heuristics inspired by the trading-cost tradeoff.
They do not reproduce its closed-form optimal aim portfolio, calibrated impact
model or optimal trading speed. Compare outcomes before claiming improvement.

The separate raw-history collector defaults to the recent five years, includes
known delisting evidence, preserves empty/error responses and provides a normal
SIGTERM stop path. It does not update canonical prices, enable a scheduler, or
turn negative early QFQ prices into return labels. Source archives require
provider comparison and corporate-action validation before use.

## Delisting and suspension evidence

Missing quotes alone do not establish a delisting or suspension. Reviewed
exchange evidence uses the actual delisting date, inclusive of that day, and
keeps a gap between the last known quote and delisting unresolved. Historical
stocks and stranded positions are retained; the last quote is not a liquidation
price. Research still uses the five-year primary window.

`import_delisting_evidence.py` validates saved SSE JSON or SZSE XLSX responses
against a request manifest. Its `reviewed-delisting-v1` manifest has `items`
containing `exchange`, `symbol`, `delisted_date`, `reviewed`, and a `source`
object with `format`, `file`, `url`, `sha256`, `observed_at`. Formats are
`sse_delisted_json` and `szse_delisted_xlsx`. The original official URL and
timezone-aware observation time must be retained. Source files stay outside Git.

```bash
python apps/finagent/scripts/analytics/import_delisting_evidence.py \
  --manifest .local/lifecycle/delisting-manifest.json \
  --output-dir .local/lifecycle/preflight
# Add --apply only after reviewing the validated source dates and preflight.
```

Imports archive previous master/version rows, reject conflicting prices on or
after delisting, and record current import time as system availability. They do
not create historical prices or claim that newly found evidence was known to the
system in the past. Repeat imports do not rewrite the same evidence.

`resolve_market_nontrading_gaps.py` supports closed suspension intervals and
dated continuation confirmations. A continuation is bounded at the confirmation
date; it does not certify later days or imply a delisting. A resumption does not
automatically certify ST history or all trading rules.

Lifecycle consumers apply both valid-date and knowledge cutoffs before resolving
versions. Reviewed evidence or conflicts outrank inferred baselines; within that
tier, later available revisions win. Baseline refreshes append versions and
retain earlier system knowledge. A scoped `--asset-ids-json` repair does not
update whole-market coverage watermarks. Tests include actual PostgreSQL
temporary tables and transaction rollback when `FINAGENT_DB_TESTS=1`.

## Development checks

```bash
make test-agent lint doctor
```

The standard gate includes causality, label-purge, missing-forecast, source-parse
and knowledge-cutoff tests. Live SQL/tool acceptance should use the configured
read-only account. It must not send group-chat test messages or place orders.

Primary references: [Gu, Kelly and Xiu](https://www.nber.org/papers/w25398),
[Moreira and Muir](https://www.nber.org/papers/w22208),
[trading-cost research](https://www.aqr.com/insights/research/working-paper/trading-costs-of-asset-pricing-anomalies),
[Qlib source](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py),
[equal-weight portfolio comparison](https://doi.org/10.1093/rfs/hhm075),
[delisting bias](https://doi.org/10.1111/j.1540-6261.1997.tb03818.x),
and [selection-bias research](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).


## Causal market-risk forecasts

`config/research/market_risk.json` fixes the stock scores and compares portfolio
covariance risk, historical 20-session market volatility, exponentially weighted
risk, and a quarterly fitted HAR-style daily-return model. A separate HAR
construction removes the trend restriction; no construction uses leverage.
Run the existing `run_portfolio_cycle.py` command with this study configuration.
The source window and frozen reference predictions are verified before fitting.

`market_risk_forecast.py` uses log averages of daily squared market returns over
1, 5 and 22 sessions to predict the next 20 sessions' mean squared return. This
is a daily-data approximation, not Corsi's intraday HAR-RV implementation.
Membership is the prior session's eligible universe. Low breadth and missing
returns remain missing. Training uses at most 756 preceding sessions, inside
the primary five-year window, and only labels ending strictly before the first
signal of the next quarter. Standardization, regularization, residual smearing
and log prediction bounds use only that training set. A fixed penalty is used;
no test-return search or full-sample risk normalization occurs.

The market aggregate includes public price history for the full frozen universe;
it is neither a tradable index nor a market signal independent of reserve
stocks. Its close-to-close squared-return target is a risk proxy, not the
next-open account return. Unknown future outcomes are never scored as zero.
`RISK_PREDICTION_RESULTS.csv` compares all methods on identical matured dates,
with yearly and overall rows, overlapping and fixed nonoverlapping windows.
QLIKE here is `actual/predicted - log(actual/predicted) - 1`; smaller is better.
Prediction accuracy and net trading performance are separate acceptance tests.

The runner saves aggregate inputs, forecasts, every training fold and a receipt.
`verify_quant_cycle.py` reconstructs all risk forecasts and scores, checks the
training boundaries, and verifies each account's prior-session risk estimate
and target exposure in addition to NAV, fees and fills. Missing forecasts are
explicitly counted and cannot silently fall back to another risk model.
Old policy defaults and previously audited accounts must remain unchanged.

[Corsi (2009)](https://statmath.wu.ac.at/~hauser/LVs/FinEtricsQF/References/Corsi2009JFinEtrics_LMmodelRealizedVola.pdf)
provides the multi-horizon motivation.
[Moreira and Muir's working paper](https://conference.nber.org/confer/2016/LTAMs16/Moreira_Muir.pdf)
studies volatility management, while
[Cederburg et al. (2020)](https://www.sciencedirect.com/science/article/pii/S0304405X2030132X)
reports mixed performance across strategies. These sources motivate tests, not
a claim that this adaptation improves A-share returns.

## Prediction horizon and selection cadence

`config/research/horizons.json` separates the training label from portfolio
selection frequency. It declares annual 5-session fits, unchanged annual
20-session controls and a fixed equal-weight blend of each family's two
contemporaneous ranks. Missing components remain missing. No mixture weight is
chosen on test returns. The three-year rolling history, purged 63-date validation,
tree settings and five-year primary boundary remain unchanged. Shorter labels
mature earlier, so their training sets can include 15 more recent signal dates.

```bash
python apps/finagent/scripts/analytics/run_horizon_cycle.py \
  --dataset .local/research/market \
  --forecasts .local/research/forecast-features.npz \
  --reference-experiment .local/research/adaptation \
  --study-config config/research/horizons.json \
  --output .local/research/horizons
python apps/finagent/scripts/analytics/verify_adaptation_cycle.py \
  --experiment .local/research/horizons \
  --dataset .local/research/market \
  --reference-experiment .local/research/adaptation \
  --report .local/research/horizons-verification.json
python apps/finagent/scripts/analytics/run_portfolio_cycle.py \
  --dataset .local/research/market \
  --reference-experiment .local/research/horizons \
  --study-config config/research/horizons.json \
  --capital 1000000 \
  --output .local/research/horizons-1000000
python apps/finagent/scripts/analytics/verify_quant_cycle.py \
  --experiment .local/research/horizons-1000000 \
  --report .local/research/horizons-1000000-verification.json
```

Repeat at the other declared capital. Each capital has 156 accounts: six
scores, four allocations and two benchmarks across three cohorts and two costs.
Compare 5/20-session selection at fixed signal and allocation; separately
compare signals at fixed selection interval. Selection intervals are not forced
holding periods: rank retention and blocked fills can extend actual holdings.
Equal-weight and covariance-risk allocations retain the same cost and turnover
rules as their previous controls. Include fill traces and audited cash books.

Account tables retain `horizon=20` for the nonoverlapping NAV-win-rate test and
add `prediction_horizons` (`5`, `20`, `5+20`, or `not_applicable` for benchmarks)
and `rebalance_sessions`. Yearly rows retain both. The verifier compares these
to the declared model and actual policy, including default selection intervals.
Prediction diagnostics are different: their `horizon` names the realized label
under evaluation. They report both 5/20-session raw next-open returns on mature
complete windows. The next execution open determines calendar-year ownership;
the last signal without a subsequent open is excluded. These correlations use
overlapping labels and cannot substitute for executed net account performance.

[Nechvátalová (2024)](https://journal.fsv.cuni.cz/article/multi-horizon-equity-returns-predictability-via-machine-learning/)
motivates separating forecast horizon, turnover and net performance. Our fixed
rank average does not reproduce its double-sort or buy/hold-spread strategies.
[De Groot, Huij and Zhou](https://repub.eur.nl/pub/25718/AnotherLook_2011.pdf)
further motivates explicit costs and retention rules. Neither paper establishes
that faster forecasts or selection improve this A-share account.

## Stock weights and total exposure

`config/research/weight_exposure.json` freezes three tree score families and
crosses equal/inverse-volatility stock weights with four exposure rules: full
target, trend-only, and trend plus 24%/30% covariance-volatility budgets. All eight
core cells refresh targets every five sessions, select every twenty, and retain
the same 25% relative no-trade band. Thus weighting, trend and volatility-budget
contrasts can be evaluated separately. A volatility goal is not a drawdown cap.

The ninth allocation preserves the legacy full equal-weight account, which has
twenty-session refresh and no target band. Its comparison to the new full target
changes both refresh cadence and the band, so it is explicitly a combined-rule
contrast. Fill constraints and incumbent retention can also change realized
constituents even when score arrays and selection rules are identical.

Run `run_portfolio_cycle.py` with this study and the completed horizon-prediction
directory as reference, then run `verify_quant_cycle.py` as above. For each
declared capital also run:

```bash
python apps/finagent/scripts/analytics/verify_portfolio_exposure.py \
  --experiment .local/research/weight-exposure-1000000 \
  --dataset .local/research/market \
  --report .local/research/exposure-1000000-verification.json
```

The 348 accounts include 174 per capital, with both same-capital benchmarks.
The runner can select a declared subset of frozen prediction families while
preserving the original prediction file and each selected family's label horizon.
Relabeling a reference family is rejected. No fitting occurs in this study.

The exposure verifier checks every model account's decision calendar, prior
signal date, selected stocks, risk-history coverage and target allocation. It
uses a separate sorted-saturation weight solution and centered weighted-return
covariance calculation. Missing history cannot become a fallback risk estimate.
Target-rule verification complements cash/fill audits; it does not assert that
blocked orders attained targets or that future risk stays below the goal.

[Hjalmarsson and Manchev](https://www.federalreserve.gov/pubs/ifdp/2009/981/ifdp981.htm)
distinguish expected-return prediction from portfolio-weight choice and retain
a strong equal-weight comparison. This factorial study uses fixed rules rather
than reproducing their characteristic-based optimizer. All model choices still
come from inspected historical research and require subsequent independent
validation before production use.

## Financial history and revision timing

Financial facts extracted from current filings, historical vendor metrics, and
figures parsed from research reports have different coverage and provenance.
Their catalog entries point to their actual tables. The `financials_raw` records
in `market.events_raw` preserve the source payload and observation time.

`build_financial_history_panel.py` reads a frozen, checksummed local export and
the same market/security snapshot used by the account studies. It provides two
views: a public reconstruction delayed past the latest notice/update/source
date, and a system view that additionally requires actual ingestion. Both use
15:00 Beijing decisions, the latest visible financial period and revision, and
a declared maximum age measured from period end. Ambiguous same-clock versions
and missing fields remain missing; an older report cannot silently fill them.

The five financial values are reported gross margin, parent-profit/revenue,
reported cumulative ROE, revenue growth, and parent-profit growth. Percentage
points become fractions. Reported duration and age accompany the values; ROE is
not silently annualized. Cash per share stays in the source while its share
basis remains unverified. The collector uses the vendor's `XSMLL` gross-margin
field and retains `BPS` and `MGJYXJJE`, rather than expecting unavailable fields.

```sh
.venv/bin/python apps/finagent/scripts/analytics/build_financial_history_panel.py \
  --source "$FROZEN_FINANCIAL_SOURCE" --dataset "$FROZEN_MARKET_PANEL" \
  --output "$FINANCIAL_PANEL_OUTPUT" --max-age-days 550
.venv/bin/python apps/finagent/scripts/analytics/verify_financial_history_panel.py \
  --source "$FROZEN_FINANCIAL_SOURCE" --dataset "$FROZEN_MARKET_PANEL" \
  --panel "$FINANCIAL_PANEL_OUTPUT" --report "$FINANCIAL_VERIFICATION_REPORT"
```

The verifier checks accepted values against raw payloads, accounts for every
rejected source row, and independently reconstructs every selected version and
feature with per-period as-of arrays. It also recomputes annual coverage using
the original eligibility rules. Coverage counts are eligible security-days,
not trades, independent trials, or return measurements. Only the primary
five-year window can supply training labels and scored returns.

[Quality minus junk](https://link.springer.com/article/10.1007/s11142-018-9470-2)
motivates examining profitability, growth and safety together. These inputs do
not reproduce its complete quality factor, five-year growth definition or
long/short portfolio. A subsequent feature ablation must establish whether the
new information improves costed account results. Vendor revision dates still
do not certify an archived historical system record.


## Controlled financial feature ablation

`config/research/financial_ablation.json` freezes four input groups: the original
72-feature market/EPS model, added profitability, added reported growth, and
both additions. The baseline prediction array is copied unchanged from the
verified annual model. New groups retain the same annual training splits,
20-session labels, purged validation, model parameters and three-component
ridge/return-tree/rank-tree blend. Only discovery securities supply labels.

Each new financial field has a contemporaneous cross-sectional rank and an
availability mask. Missing observations remain missing before the existing
learner handles them; they do not remove a stock from market eligibility.
Reported months divided by 12 and report age divided by 550 add context with
their own masks. They neither annualize cumulative ROE nor mix financial
periods. The groups contain 82, 80 and 86 columns respectively.

```sh
.venv/bin/python apps/finagent/scripts/analytics/run_financial_ablation.py \
  --dataset "$FROZEN_MARKET_PANEL" --forecasts "$FROZEN_EPS_FEATURES" \
  --reference-experiment "$VERIFIED_HORIZON_PREDICTIONS" \
  --financial-panel "$FINANCIAL_PANEL_OUTPUT" \
  --financial-verification "$FINANCIAL_VERIFICATION_REPORT" \
  --study-config config/research/financial_ablation.json \
  --output "$FINANCIAL_PREDICTIONS"
.venv/bin/python apps/finagent/scripts/analytics/verify_financial_ablation.py \
  --experiment "$FINANCIAL_PREDICTIONS" \
  --dataset "$FROZEN_MARKET_PANEL" --forecasts "$FROZEN_EPS_FEATURES" \
  --reference-experiment "$VERIFIED_HORIZON_PREDICTIONS" \
  --financial-panel "$FINANCIAL_PANEL_OUTPUT" \
  --financial-verification "$FINANCIAL_VERIFICATION_REPORT" \
  --report "$FINANCIAL_PREDICTION_VERIFICATION"
```

The verifier checks every training boundary and prediction checkpoint, rebuilds
financial columns using independent SciPy ranks, and reloads all saved trees
and ridge parameters to regenerate predictions. The original market/EPS
columns and baseline score arrays must remain unchanged. Financial source,
market, forecast, code and study fingerprints are retained with each run.

Replay these frozen predictions with `run_portfolio_cycle.py` using the same
study configuration, separately for each declared capital. Four models and
three existing portfolio rules plus two benchmarks produce 168 accounts across
two capital levels, three cohorts and two fees. Compare additions against the
original model at the same allocation/capital/cohort/cost. Preserve annual
results, including an explicitly partial current year, and all losing cases.

These are historically reconstructed financial inputs. Better ranking metrics
do not establish better costed account returns; neither establishes an
untouched prospective result. Repeatedly inspected years remain exploratory,
and the balanced return/drawdown/win-rate gates do not change.
