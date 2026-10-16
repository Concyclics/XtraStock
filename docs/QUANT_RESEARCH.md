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
notes remain on the row. This keeps the normal five-result page and its evidence
inside the tool budget as study contracts grow.

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

The method catalog also records
[Gârleanu and Pedersen's dynamic trading model](https://nbgarleanu.github.io/DynTrad.pdf)
and its [corrigendum](https://nbgarleanu.github.io/DynTrad_corrigendum.pdf).
Partial adjustment toward a target is a proposed follow-up experiment, not an
implemented optimal-trading algorithm or a measured return improvement here.

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
