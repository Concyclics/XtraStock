"""Causality and split invariants for the optional tree research engine."""
from pathlib import Path
import sys
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from quant_tree_learning import extended_features, daily_rank_targets, purged_splits
from strategy_research import prepare_features


def panel(days=850, assets=3):
    rng = np.random.default_rng(50)
    close = 10 * np.exp(np.cumsum(rng.normal(.0003, .01, (days, assets)), axis=0))
    opened = close * .999
    return {'dates': pd.bdate_range('2020-01-01', periods=days).strftime('%Y-%m-%d').to_numpy(),
            'symbols': np.array([f'{i:06}' for i in range(assets)]),
            'cohort': np.array(['discovery', 'discovery', 'cross_section_reserve'])[:assets],
            'open': opened, 'close': close, 'high': close * 1.02, 'low': opened * .98,
            'volume': np.full_like(close, 100000), 'amount': np.full_like(close, 1e8),
            'quality': np.ones_like(close), 'adjustment_quality': np.ones_like(close),
            'total_return_factor': np.ones_like(close), 'assets': [], 'actions': []}


class TreeContractTests(unittest.TestCase):
    def test_features_unchanged_when_future_rows_change(self):
        source = panel()
        x, names = extended_features(source, prepare_features(source))
        changed = {key: value.copy() if isinstance(value, np.ndarray) else value for key, value in source.items()}
        for key in ('open', 'high', 'low', 'close', 'volume', 'amount', 'total_return_factor'):
            changed[key][700:] *= 7
        other, other_names = extended_features(changed, prepare_features(changed))
        self.assertEqual(names, other_names)
        np.testing.assert_allclose(x[:700], other[:700], equal_nan=True)

    def test_missing_eps_retains_price_features_and_missing_mask(self):
        data = panel()
        features = prepare_features(data)
        forecasts = {k: np.full_like(data['close'], np.nan) for k in ('value', 'revision')}
        x, names = extended_features(data, features, forecasts)
        self.assertTrue(np.isfinite(x[600:, :, names.index('return_20')]).all())
        self.assertTrue(np.isnan(x[:, :, names.index('value_rank')]).all())
        self.assertTrue((x[:, :, names.index('value_available')] == 0).all())

    def test_rank_target_excludes_reserve_outcomes(self):
        target = np.array([[.1, -.1, 20.], [.3, .8, -20.]])
        mask = np.array([True, True, False])
        expected = daily_rank_targets(target, mask)
        target[:, 2] *= -100
        np.testing.assert_allclose(expected, daily_rank_targets(target, mask), equal_nan=True)
        self.assertTrue(np.isnan(expected[:, 2]).all())

    def test_two_purges_use_label_expiry(self):
        data = panel()
        mask = np.ones_like(data['close'], dtype=bool)
        train, inner, validation, first = purged_splits(data['dates'], mask, 2023)
        train_days = np.flatnonzero(train.any(axis=1))
        inner_days = np.flatnonzero(inner.any(axis=1))
        val_days = np.flatnonzero(validation.any(axis=1))
        self.assertLess(train_days[-1] + 20, first)
        self.assertLess(inner_days[-1] + 20, val_days[0])
        self.assertEqual(len(val_days), 63)
        self.assertFalse((inner & validation).any())

    def test_insufficient_history_cannot_silently_train(self):
        data = panel(days=300)
        with self.assertRaises(ValueError):
            purged_splits(data['dates'], np.ones_like(data['close'], bool), 2020)


if __name__ == '__main__':
    unittest.main()
