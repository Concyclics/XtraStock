"""Replay fixed prior accounts and independently audit their fill evidence.

Every pre-existing cashbook field must match, including legacy diagnostics.
Daily volume evidence is ex-post only and cannot repair an auction assumption.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import itertools
import json
from pathlib import Path
import shutil
import time

import numpy as np
import pandas as pd

from execution_diagnostics import audit_execution
from strategy_dataset import checksum, dump
from strategy_research import Policy, backtest, load_dataset, prepare_features


def compare_cashbook(actual, original):
    original = {key: value for key, value in original.items() if key != 'protocol_sha256'}
    actual = {key: value for key, value in actual.items() if key not in ('execution_trace', 'execution_audit', 'protocol_sha256')}
    # New optional policy fields preserve the exact old decision when defaulted.
    # Only an absent old field with that exact default can be omitted; a changed
    # fraction/band or any old policy value must still fail the regression.
    actual['policy'] = dict(actual['policy'])
    for key, default in (('adjustment_fraction', 1.), ('relative_no_trade_band', 0.)):
        if key not in original['policy'] and actual['policy'].get(key) == default:
            actual['policy'].pop(key)
    if actual != original:
        changed = sorted(key for key in set(actual) | set(original) if actual.get(key) != original.get(key))
        raise ValueError('Cashbook changed while tracing: ' + ', '.join(changed))


def run(args):
    out, reference = args.output.resolve(), args.reference.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    state = {'status': 'running', 'started_at': started, 'completed': 0}
    dump(out / 'RUN_STATE.json', state)
    try:
        study = json.loads(args.study.read_text())
        predictions = reference / 'predictions/PREDICTIONS.npz'
        prediction_hash = checksum(predictions)
        ref_spec = json.loads((reference / 'predictions/PROTOCOL.json').read_text())
        receipt = json.loads((reference / 'predictions/PREDICTION_RECEIPT.json').read_text())
        complete = json.loads((reference / 'predictions/COMPLETE.json').read_text())
        if receipt['sha256'] != prediction_hash or receipt['protocol_sha256'] != checksum(reference / 'predictions/PROTOCOL.json'):
            raise ValueError('Prediction provenance failed')
        if complete['protocol_sha256'] != receipt['protocol_sha256']:
            raise ValueError('Reference training is incomplete')
        for name, expected in ref_spec['source'].items():
            if checksum(args.dataset / name) != expected:
                raise ValueError('Frozen market changed: ' + name)
        source = Path(__file__).parent
        code_names = ('run_execution_audit.py', 'execution_diagnostics.py', 'strategy_research.py', 'strategy_dataset.py')
        spec = {'study': study, 'source': ref_spec['source'], 'predictions_sha256': prediction_hash,
            'code': {name: checksum(source / name) for name in code_names},
            'input_window': [ref_spec.get('primary_since'), ref_spec.get('evaluation_until')],
            'executed_at': started, 'model_refitted': False, 'execution_changed': False, 'production_approved': False}
        dump(out / 'AUDIT_PROTOCOL.json', spec)
        (out / 'code-at-execution').mkdir()
        for name in code_names:
            shutil.copyfile(source / name, out / 'code-at-execution' / name)
        data = load_dataset(args.dataset, 'all'); features = prepare_features(data)
        with np.load(predictions, allow_pickle=False) as loaded:
            scores = {name: loaded[name] for name in study['models']}
        if any(score.shape != data['close'].shape for score in scores.values()):
            raise ValueError('Prediction shape does not match source')
        summary = []; years = []; attributions = []; regressions = []; flags = []
        (out / 'traces').mkdir()
        plans = list(itertools.product(study['capital_levels'], study['cohorts'], study['cost_bps_per_side'],
                                       study['models'], study['allocations']))
        reference_protocols = {}
        for capital in study['capital_levels']:
            path = reference / f'capital-{capital}'
            reference_protocols[capital] = checksum(path / 'PROTOCOL.json')
            completed = json.loads((path / 'COMPLETE.json').read_text())
            if completed['protocol_sha256'] != reference_protocols[capital] or checksum(path / 'PREDICTIONS.npz') != prediction_hash:
                raise ValueError('Reference replay provenance failed')
        for capital, cohort, cost, model, allocation in plans:
            begin = time.monotonic()
            name = f'{cohort}-cost{cost}-{model}-{allocation}'
            original_path = reference / f'capital-{capital}/simulations/{name}.json.gz'
            with gzip.open(original_path, 'rt') as stream:
                original = json.load(stream)
            if original['protocol_sha256'] != reference_protocols[capital]:
                raise ValueError('Reference cashbook receipt mismatch')
            policy = Policy(**original['policy'])
            if policy.family != model or policy.initial_cash != capital or policy.cost_bps != cost:
                raise ValueError('Reference cashbook policy mismatch')
            mask = np.ones(len(data['symbols']), bool) if cohort == 'all' else data['cohort'] == cohort
            if not mask.any():
                raise ValueError('Empty audit cohort')
            scoped = {**features, 'score': {model: np.where(mask[None, :], scores[model], np.nan)}}
            replay = backtest(data, scoped, policy, original['dates'][0], original['dates'][-1],
                              keep_trades=True, keep_fills=True)
            compare_cashbook(replay, original)
            audit = audit_execution(replay)
            trace_file = out / 'traces' / f'{capital}-{name}.json.gz'
            with gzip.open(trace_file, 'xt') as stream:
                json.dump({'reference_sha256': checksum(original_path), 'trace': replay['execution_trace'],
                           'audit': audit}, stream, allow_nan=False)
            identity = {'initial_cash': capital, 'model': model, 'allocation': allocation, 'cohort': cohort,
                        'cost': cost, 'horizon': 20, 'year': '2023-2026'}
            counts = audit['counts']
            summary.append({**identity, **counts, 'share_volume_breach_rate': counts['share_volume_breach'] / max(1, counts['fill_count']),
                'legacy_notional_breach_rate': counts['legacy_notional_breach'] / max(1, counts['fill_count']),
                'net_cagr': original['metrics']['cagr'], 'maximum_drawdown': original['metrics']['maximum_drawdown'],
                'period_win_rate': original['metrics']['period_win_rate'],
                'annual_oneway_turnover': original['metrics']['annual_oneway_turnover'],
                **audit['accounting'], 'legacy_cashbook_identical': True, 'execution_verified': False, 'production_approved': False})
            for year, values in audit['by_year'].items():
                years.append({**identity, 'year': year, **values, 'is_partial_year': year == '2026'})
            for row in audit['asset_attribution']:
                attributions.append({**identity, **row})
            for row in audit['flagged_fills']:
                flags.append({**identity, **row})
            regressions.append({**identity, 'all_legacy_fields_identical': True,
                'reference_sha256': checksum(original_path), 'trace_sha256': checksum(trace_file),
                'fill_rows': counts['fill_count'], 'asset_rows': len(audit['asset_attribution']),
                'nav_reconciliation_error': audit['accounting']['nav_reconciliation_error']})
            state['completed'] += 1
            dump(out / 'RUN_STATE.json', state)
            print(json.dumps({'phase': 'audited', 'completed': state['completed'], 'of': len(plans),
                **identity, 'fills': counts['fill_count'], 'legacy_breaches': counts['legacy_notional_breach'],
                'share_volume_breaches': counts['share_volume_breach'], 'lower_bound_only': counts['lower_bound_only_breach'],
                'seconds': round(time.monotonic() - begin, 2)}), flush=True)
        for name, rows in (('EXECUTION_RESULTS.csv', summary), ('EXECUTION_YEARLY_RESULTS.csv', years),
                            ('ASSET_ATTRIBUTION.csv', attributions), ('FLAGGED_FILLS.csv', flags)):
            pd.DataFrame(rows).to_csv(out / name, index=False)
        dump(out / 'CASHBOOK_REGRESSION.json', {'status': 'passed', 'comparisons': len(regressions), 'checks': regressions})
        state.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat(),
            execution_verified=False, production_approved=False,
            output_sha256={name: checksum(out / name) for name in
                ('EXECUTION_RESULTS.csv', 'EXECUTION_YEARLY_RESULTS.csv', 'ASSET_ATTRIBUTION.csv', 'FLAGGED_FILLS.csv', 'CASHBOOK_REGRESSION.json')})
        dump(out / 'COMPLETE.json', state)
    except BaseException as exc:
        state.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        dump(out / 'RUN_STATE.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True, help='Completed adaptation project with predictions and capital-* accounts')
    parser.add_argument('--output', type=Path, required=True, help='New directory; existing results are never overwritten')
    parser.add_argument('--study', type=Path, default=Path(__file__).resolve().parents[4] / 'config/research/execution_audit.json')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
