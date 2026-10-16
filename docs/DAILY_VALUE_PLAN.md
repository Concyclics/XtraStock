# Daily valuation plans and limit execution

The daily candidate product has three separate records: dated fundamental
evidence, immutable recommendations, and simulated execution. A candidate is
not a holding, a valuation scenario is not a price forecast, and a proposed
limit is not a fill. This implementation has no brokerage connectivity.

## Configuration and deployment

`apps/finagent/config/daily_value_plan.json` contains the research and deployment
policy. Database credentials and endpoints continue to come from the existing
private configuration. No private connections belong in this JSON or Git.

Apply `database/migrations/daily_value_plan_v1.sql` before deploying the jobs.
The migration is additive and repeatable. Plans, revisions, events and evidence
cannot be updated or deleted; corrections must append new records. Daily plan
generation has a database lock and one frozen version per strategy/date.

The existing pipeline controller owns these stages, in Beijing time:

| Stage | Earliest time | Dependency |
| --- | --- | --- |
| Plan generation | 09:06 on weekdays | Successfully published daily report |
| Observed earnings refresh | 18:20 | Incremental research-report collection |
| Simulated settlement | 20:25 on weekdays | Completed market ingestion |

QQ delivery runs through the existing report connector after the matching
report was successfully sent to each group. Later polling can deliver a plan
that completed after the report. `publish_enabled` defaults to false. Enabling
it alone does not authorize recommendations: an approved run and a current,
configuration-bound acceptance receipt are also required. Ambiguous network
delivery is recorded as `uncertain` and is not automatically resent.

## Earnings and valuation evidence

`collect_earnings_observations.py` reads the source API's explicit `currentYear`.
Its rolling forecast fields become knowable at **fetch time**, even when the
associated report is old. They are excluded from historical backtests.
Unchanged observations are not inserted again. A source response can support
an observation whose corpus document is still missing; that relationship is
reported separately and does not imply a recovered report body.

`backfill_earnings_documents.py` parses existing recovered document bodies. It
requires explicit fiscal years and units, records conflicting/empty results,
and resumes by document, parser version and body hash. It does not start OCR or
external model jobs. Extraction failures are not numeric zeroes. Source history
is reconstructed from dated documents; it is never described as a historical
system snapshot.

Each institution receives one latest vote per fiscal year. Zero and negative
EPS invalidate that institution's older positive forecast. Conflicts and share
basis changes remain gaps. EPS revision compares the same institution and
fiscal year in two dated reports; refreshing a rolling API is not a revision.

Current forward P/E is recalculated from the dated current unadjusted close
and compatible EPS. The original report's quoted P/E is retained as source
evidence, not displayed as current market valuation. The initial sector model
requires three institutions and eight other sector peers, excludes financial
industries needing dedicated valuation, and caps the comparison multiple.
Its bear/base/bull values are valuation scenarios without assigned probabilities
or promised hit dates. Cash-flow/financial quality remains a separate input.

## Plan ledger and execution

`run_daily_value_plan.py` defaults to a dry run. `--apply` requires the current
date's completed report. A degraded report permits only local shadow recording;
approved recommendations require a fully validated published report. It stores the source cutoff, configuration
version, predecessor plan, entry limit/expiry, reasons, invalidation conditions,
and coverage. No existing plan is overwritten. Removing an unbought candidate
cancels interest; a held position needs a separate review. Personal break-even
prices remain empty without actual cost/position evidence.

`limit_execution.py` is shared by strict paper settlement and historical
replay. The limit must be penetrated by a tick; a touch is not assumed to win a
queue. Buy/sell prices respect their limits and explicit fees/slippage. A
publication after 09:15 is eligible only at a later verified session because
daily bars cannot reconstruct the remaining auction/intraday path. Both entry
and actual delivery times are preserved. Orders may remain unfilled, partially
fill, expire or be cancelled. Intraday sale proceeds cannot finance a purchase
whose ordering is unknown. T+1, suspension and price limits are checked.

The optional barrier diagnostic chooses the adverse exit if a daily bar crosses
both stop and target without a known path. The initial live simulated account
uses daily decisions and time exits. It does not claim stop-loss execution from
an intraday path that was never observed. Missing held marks or unsupported
corporate actions abort settlement and retain a review event without a NAV.

## Five-year acceptance

`run_value_limit_study.py` accepts a frozen five-year market dataset and a hashed
dated-document earnings export. It reports all fixed rule variants, 20/60-session
horizons, 15/30 bps per-side costs, and all-universe/reserved-security cohorts.
The relative-valuation strategy and absolute-P/E control have separate matched
baselines. Equal-weight controls use the same eligibility and twenty slots;
they are not full-market index returns. Warm-up rows are not scored. Every order,
partial fill, expiry, fee, corporate action and stale held mark remains auditable.

Research daily bars still lack fully verified historical ST/price-limit queues,
delisting settlements and historical system knowledge. Research reconstruction
can estimate outcomes but cannot pass the strict execution/PIT gates by itself.
Current API observations never enter its historical signals.

Publication requires all of the following, without lowering failed gates:

- Net CAGR above 20%, drawdown at most 25%, and non-overlapping 20-session NAV
  win rate at least 55%; three positive complete calendar years.
- Positive return and win-rate advantages against matched controls, with higher
  transaction costs and reserved securities evaluated separately.
- Verified execution and point-in-time data; calibrated forecasts before any
  expected return, probability or hit-time estimate is published.
- At least twenty forward shadow sessions and thirty mature simulated trades.

The configuration-bound approval receipt is separate from experiment output.
An experiment never enables publication automatically. The existing Agent and
daily report can read observed P/E, EPS coverage, reasons and labelled valuation
scenarios through `value_plan_reader.py` while buy/sell publication is disabled.

Trading-rule references: [SSE 2026 trading rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
and [SZSE rule catalog](https://www.szse.cn/lawrules/index.html). Historical
rule changes must be represented as dated evidence; a present-day rule cannot
silently be applied to every past security/session.

## Validation and rollback

Run the daily-value-plan, limit-replay, paper-execution, delivery, forecast and
controller tests, then the repository quality gate. Tests use simulated groups
and do not send QQ messages. Live acceptance must verify database read/write
permissions, ledger immutability, idempotent repeated runs and source coverage.

Rollback stops only the three new controller stages and sets plan publication
to false. Preserve the additive tables, source caches and append-only records.
Restoring prior application files must not remove saved observations or plans.
