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
methodology and filters for model, allocation, cohort, cost, horizon and year.
Methods and offline results do not become approved forecasts merely by entering SQL.

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

The current runners evaluate fixed 2022–2025 folds on supplied immutable panels.
They do not silently extend those results to 2026. Input dates, securities and
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

Opening orders use only prior-session liquidity and an observed open. The
reported same-day capacity breaches are unresolved execution errors, not proven
auction fills. Current security-master flags, historical trading states and
corporate-action revisions still require independent checks. Arithmetic
verification does not certify these inputs or market execution.
When a missing historical price is recovered, subsequent total-return factors
inherit that input's availability time. A late ex-date price cannot change a
factor while leaving its system-knowledge boundary in the past. The PostgreSQL
revision fixture tests this propagation using temporary tables and rollback.

The separate raw-history collector defaults to the recent five years, includes
known delisting evidence, preserves empty/error responses and provides a normal
SIGTERM stop path. It does not update canonical prices, enable a scheduler, or
turn negative early QFQ prices into return labels. Source archives require
provider comparison and corporate-action validation before use.

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
and [selection-bias research](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
