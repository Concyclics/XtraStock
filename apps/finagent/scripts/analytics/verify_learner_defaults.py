"""Check that adding explicit seeds preserves the recorded default learner."""
from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

import quant_adaptation
import quant_tree_learning
from strategy_dataset import checksum, dump

CORE_FILES = ('quant_learning.py', 'quant_tree_learning.py', 'quant_adaptation.py', 'strategy_research.py')


def read_compatibility(path, reference):
    path, reference = Path(path), Path(reference)
    report = json.loads(path.read_text())
    code = Path(__file__).parent
    expected = json.loads((reference/'PROTOCOL.json').read_text())
    if (report['status'] != 'default_learner_compatible'
            or report['reference_protocol_sha256'] != checksum(reference/'PROTOCOL.json')
            or report['reference_code'] != {name: expected['code'][name] for name in CORE_FILES}
            or report['current_code'] != {name: checksum(code/name) for name in CORE_FILES}
            or not report['unrelated_feature_label_split_code_unchanged']
            or not report['all_default_probe_predictions_identical']
            or not report['all_default_fitted_parameters_identical']
            or len(report['serialized_default_trees_identical']) != 2
            or report['maximum_validation_reduction_roundoff'] > 1e-14):
        raise ValueError('Default learner compatibility receipt differs from the selected reference or code')
    return {'sha256': checksum(path), 'reference_protocol_sha256': report['reference_protocol_sha256'],
            'current_code': report['current_code']}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function(path, name):
    return ast.dump(next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == name))


def verify(reference):
    reference = Path(reference)
    code = Path(__file__).parent
    spec = json.loads((reference/'PROTOCOL.json').read_text())
    old = reference/'code-at-execution'
    for name in CORE_FILES:
        if checksum(old/name) != spec['code'][name]:
            raise ValueError('Reference learner snapshot changed')
    for name in ('quant_learning.py', 'strategy_research.py'):
        if checksum(code/name) != spec['code'][name]:
            raise ValueError('Core labels, ridge or market implementation changed')
    for file, names in {
        'quant_tree_learning.py': ('divide', 'extended_features', 'daily_rank_targets', 'purged_splits', 'day_weights', 'walk_forward_trees'),
        'quant_adaptation.py': ('prediction_periods', 'rolling_splits', '_predict_block'),
    }.items():
        for name in names:
            if _function(code/file, name) != _function(old/file, name):
                raise ValueError('Unrelated feature, split or prediction code changed: '+name)
    old_tree = _load('reference_tree_defaults', old/'quant_tree_learning.py')
    old_adaptation = _load('reference_adaptation_defaults', old/'quant_adaptation.py')
    # Bind the previously recorded fitter as well as the recorded walk-forward
    # loop. Their unchanged label/ridge helpers were checked byte for byte.
    old_adaptation.fit_tree = old_tree.fit_tree
    if old_tree.TREE_PARAMS != quant_tree_learning.TREE_PARAMS or spec['tree_parameters'] != old_tree.TREE_PARAMS:
        raise ValueError('Default tree parameters changed')
    rng = np.random.default_rng(731)
    dates = pd.bdate_range('2021-10-15', '2023-02-28').strftime('%Y-%m-%d').to_numpy()
    close = 10*np.exp(np.cumsum(rng.normal(0, .01, (len(dates), 48)), axis=0))
    data = {'dates': dates, 'close': close, 'open': close*.999, 'volume': np.full_like(close, 1e6),
        'total_return_factor': np.ones_like(close), 'adjustment_quality': np.ones_like(close),
        'primary_since': '2021-10-15', 'cohort': np.array(['discovery']*40+['cross_section_reserve']*8)}
    features = {'valid': np.ones_like(close, bool), 'eligible': np.ones_like(close, bool)}
    matrix = rng.normal(size=(*close.shape, 6)).astype(np.float32)
    matrix[::7, ::3, 1] = np.nan
    variants = {'default': {'family': 'tree_blend', 'cadence': 'annual', 'half_life_sessions': None}}
    with tempfile.TemporaryDirectory(prefix='learner-defaults-') as folder:
        old_scores, old_folds = old_adaptation.walk_forward_adaptation(data, features, matrix, [f'x{i}' for i in range(6)],
            [2023], variants, Path(folder)/'old', 'compatibility')
        new_scores, new_folds = quant_adaptation.walk_forward_adaptation(data, features, matrix, [f'x{i}' for i in range(6)],
            [2023], variants, Path(folder)/'new', 'compatibility')
        compared = copy.deepcopy(new_folds)
        validation_roundoff = 0.
        for original, current in zip(old_folds, compared):
            for name in ('tree_return', 'tree_rank'):
                a, b = original['models'][name]['validation_loss'], current['models'][name]['validation_loss']
                validation_roundoff = max(validation_roundoff, abs(a-b))
                if abs(a-b) > 1e-14:
                    raise ValueError('Default validation metric changed beyond reduction roundoff')
                current['models'][name]['validation_loss'] = a
        if old_folds != compared or not np.array_equal(old_scores['default'], new_scores['default'], equal_nan=True):
            raise ValueError('Default fitted parameters, predictions or fold metadata changed')
        model_checks = []
        for file in sorted((Path(folder)/'old/folds/default').glob('*.txt')):
            if checksum(file) != checksum(Path(folder)/'new/folds/default'/file.name):
                raise ValueError('Default serialized tree changed')
            model_checks.append(file.name)
    return {'status': 'default_learner_compatible', 'reference_protocol_sha256': checksum(reference/'PROTOCOL.json'),
        'reference_code': {name: spec['code'][name] for name in CORE_FILES},
        'current_code': {name: checksum(code/name) for name in CORE_FILES},
        'unrelated_feature_label_split_code_unchanged': True, 'all_default_probe_predictions_identical': True,
        'all_default_fitted_parameters_identical': True, 'serialized_default_trees_identical': model_checks,
        'maximum_validation_reduction_roundoff': validation_roundoff,
        'probe_scope': 'One genuine synthetic annual tree/ridge fold; no claim of a newly trained historical baseline.',
        'production_approved': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-experiment', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.reference_experiment)
    dump(args.report, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
