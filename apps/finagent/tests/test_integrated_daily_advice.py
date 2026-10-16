"""Reader quality and ledger-bound buy/hold/exit decisions must coexist."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/pipeline'))
import daily_ai_hardware_report_v3 as daily
import daily_investment_advice as advice
import finance_pipeline_controller as controller
from scripts.analytics import run_daily_value_plan as plans
from scripts.analytics.value_plan import load_config
from mmclaw.presentation import report_delivery_binding
from mmclaw.value_plan_delivery import deliver_after_report, record_integrated_delivery


class IntegratedDailyTests(unittest.TestCase):
    def snapshot(self):
        return {'report_date': '2026-10-16', 'decision_cutoff': '2026-10-16T08:50:00+08:00',
                'market_date': '2026-10-15', 'report_scope': 'all_a_share', 'hard_gate_passed': True,
                'evidence': [
                    {'evidence_id': 'POOL0001', 'metric_id': 'frozen_research_watchlist',
                     'claim_type': 'model_signal', 'value': {'stock_code': '600100', 'name': '设备甲', 'rank': 1,
                     'research': {'actual_financials': [{'quality_status': 'verified', 'metric_code': 'net_profit_parent',
                         'metric_name': '归母净利润', 'period_end': '2026-06-30', 'yoy': 20}],
                         'risks': ['回款下降需要核对客户付款进度']}}},
                    {'evidence_id': 'CAND1001', 'metric_id': 'daily_value_plan',
                     'entity_id': '600200', 'claim_type': 'interpretation', 'value': {'entry_limit': 9.5}}]}

    def plan(self, **kw):
        return {'stock_code': '600200', 'name': '制造乙', 'plan_id': 'immutable-plan',
                'state': 'eligible_to_buy', 'change': 'added', 'entry_limit': 9.5,
                'entry_expires_on': '2026-10-19', 'paper_only': False, 'simulation_eligible': True,
                'paper_quantity': 0, 'reason': ['盈利改善且同业估值折价'],
                'invalidation': ['盈利预期转为下修'], **kw}

    def ledger(self, plan=None, **kw):
        return {'status': 'available', 'actionable': True, 'mode': 'approved', 'run_id': 'plan-run',
                'plan_date': '2026-10-16', 'sessions': ['2026-10-16', '2026-10-19', '2026-10-20'],
                'plans': [plan or self.plan()], **kw}

    def test_reader_projection_removes_diagnostics_but_preserves_risks_and_permission(self):
        snapshot = self.snapshot()
        snapshot['evidence'] += [
            {'evidence_id': 'CAND0001', 'metric_id': 'candidate_pool', 'value': {
                'component': 'candidate_pool_summary', 'state_counts': {'core': 20},
                'forecast_validation': [{'error_interval': [-.068, .138]}], 'mining': {'folds': [1, 2, 3]}}},
            {'evidence_id': 'VAL0001', 'metric_id': 'valuation_screen', 'value': {
                'supported_discount': False, 'prediction_eligible': False, 'gaps': ['多源核验未完成']}},
            {'evidence_id': 'DQ0001', 'claim_type': 'data_quality', 'metric_id': 'dataset_watermark'}]
        before = copy.deepcopy(snapshot)
        projected = advice.model_snapshot(snapshot)
        self.assertEqual(snapshot, before)
        data = json.dumps(projected, ensure_ascii=False)
        for key in ('forecast_validation', 'mining', 'DQ0001', 'CAND1001', '多源核验'):
            self.assertNotIn(key, data)
        self.assertIn('回款下降', data)
        value = next(e['value'] for e in projected['evidence'] if e['evidence_id'] == 'VAL0001')
        self.assertIs(value['prediction_eligible'], False)
        self.assertIs(value['supported_discount'], False)

    def test_quoted_defensive_paragraphs_cannot_pass_as_report_claims(self):
        texts = ['本期证据未提供全市场涨跌家数或指数走势。',
                 '本期行情未包含当日收盘。', '本期采用单来源未复权价格。',
                 '配对误差差值的置信区间跨零，尚未显示预测误差优势。']
        for text in texts:
            with self.subTest(text=text):
                claim = {'claim_id': 'C1', 'section_name': '市场概览', 'claim_type': 'interpretation',
                         'claim_text': text, 'evidence_ids': ['POOL0001']}
                _, errors = daily.validate_claims([claim], self.snapshot())
                self.assertTrue(any('operational_diagnostic' in e for e in errors))
        self.assertIsNone(advice.reader_claim_error({'claim_text': '利润增长但回款下降，重点跟踪客户付款进度。'}))

    def test_display_rounding_does_not_allow_wrong_sign_scale_or_integer(self):
        allowed = {'26.05882352941176', '-17.887', '12.34'}
        self.assertTrue(daily.supported_display_number('26.06', allowed))
        self.assertTrue(daily.supported_display_number('-17.89%', allowed))
        for unsupported in ('17.89%', '26.05', '26', '2605.88'):
            self.assertFalse(daily.supported_display_number(unsupported, allowed))

    def test_cny_unit_conversion_is_verified_without_authorizing_percent_or_eps(self):
        evidence = [{'unit': 'CNY', 'value': '-413155425.18'}, {'unit': 'CNY/share', 'value': 20}]
        self.assertEqual(daily.strip_supported_amounts('现金流-4.13亿元。', evidence), '现金流亿元。')
        for text in ('现金流4.13亿元。', '增长4.13%。', '收入20亿元。', '收入-41.32亿元。'):
            self.assertEqual(daily.strip_supported_amounts(text, evidence), text)
        from mmclaw.presentation import render_finance_text
        text = render_finance_text('收入34980983379.66元，现金流-413155425.18元，股价25.03元。')
        self.assertIn('349.81亿元', text)
        self.assertIn('-4.13亿元', text)
        self.assertIn('25.03元', text)
        self.assertEqual(render_finance_text(text), text)
        self.assertIn('2026-Q1', daily.date_tokens({'period_end': '2026-03-31'}))
        self.assertNotIn('2026-Q2', daily.date_tokens({'period_end': '2026-06-30'}))

    def test_approved_limit_is_integrated_with_sourced_watch_reasons(self):
        result = advice.build_actions(self.snapshot(), self.ledger())
        text = advice.render_actions(result)
        self.assertIn('条件买入', text)
        self.assertIn('9.50 元', text)
        self.assertIn('2026-10-19', text)
        self.assertIn('归母净利润同比增长20.00%', text)
        self.assertEqual(result['published_plan_ids'], ['immutable-plan'])
        self.assertNotIn('预期扣费收益', text)
        self.assertNotIn('immutable-plan', text)

    def test_shadow_and_expired_and_unqualified_rows_never_become_buys(self):
        for ledger in [self.ledger(actionable=False, mode='shadow'),
                       self.ledger(self.plan(entry_expires_on='2026-10-15')),
                       self.ledger(self.plan(simulation_eligible=False)),
                       self.ledger(self.plan(paper_only=True)),
                       self.ledger(plan_date='2026-10-15'), self.ledger(matching_market=False)]:
            with self.subTest(ledger=ledger):
                result = advice.build_actions(self.snapshot(), ledger)
                self.assertNotIn('buy', [a['kind'] for a in result['actions']])
                self.assertEqual(result['published_plan_ids'], [])
                self.assertNotIn('9.50', advice.render_actions(result))

    def test_candidate_removal_does_not_assert_sold_holding(self):
        for qty, kind in [(0, 'remove'), (100, 'review_holding')]:
            result = advice.build_actions(self.snapshot(), self.ledger(self.plan(state='removed', paper_quantity=qty)))
            self.assertEqual(result['actions'][0]['kind'], kind)
            self.assertNotIn('卖出限价', advice.render_actions(result))
        empty = advice.build_actions(self.snapshot(), self.ledger(self.plan(state='held', paper_quantity=0)))
        self.assertNotIn('hold', [a['kind'] for a in empty['actions']])

    def test_sell_requires_known_position_and_explicit_valid_exit_price(self):
        plan = self.plan(state='exit', paper_quantity=100, exit_limit=12.8, exit_expires_on='2026-10-16')
        text = advice.render_actions(advice.build_actions(self.snapshot(), self.ledger(plan)))
        self.assertIn('卖出限价不低于 12.80 元', text)
        self.assertNotIn('成本平衡价', text)
        for invalid in ({**plan, 'paper_quantity': 0}, {**plan, 'exit_limit': None}, {**plan, 'exit_expires_on': '2026-10-15'}):
            result = advice.build_actions(self.snapshot(), self.ledger(invalid))
            self.assertNotIn('sell', [a['kind'] for a in result['actions']])

    def test_relative_valuation_is_not_rendered_as_target_return(self):
        plan = self.plan(valuation={'base': 25}, target_price=25, expected_net_return=.2,
                         expected_sell_date='2026-11-16', prediction_eligible=False)
        text = advice.render_actions(advice.build_actions(self.snapshot(), self.ledger(plan)))
        self.assertNotIn('目标价', text)
        self.assertNotIn('预期扣费收益', text)
        self.assertNotIn('预计退出窗口', text)
        plan['prediction_eligible'] = True
        text = advice.render_actions(advice.build_actions(self.snapshot(), self.ledger(plan)))
        self.assertIn('目标价 25.00 元', text)
        self.assertIn('预期扣费收益 20.00%', text)

    def test_one_report_has_actions_and_no_diagnostic_or_empty_sections(self):
        claims = [{'section_name': '今日核心判断', 'claim_text': '盈利和回款同向改善。',
                   'evidence_ids': ['POOL0001'], 'validation_status': 'verified'},
                  {'section_name': '数据质量与限制', 'claim_text': '内部诊断。',
                   'evidence_ids': ['DQ0001'], 'validation_status': 'verified'}]
        text = daily.render_report(dt.date(2026, 10, 16), dt.datetime(2026, 10, 16, 0, 50, tzinfo=dt.timezone.utc),
            dt.date(2026, 10, 15), claims, 'published', ['failed:dataset'],
            advice.build_actions(self.snapshot(), self.ledger()))
        self.assertIn('08:50（北京时间）', text)
        self.assertIn('## 每日操作清单', text)
        self.assertNotIn('数据质量与限制', text)
        self.assertNotIn('failed:dataset', text)
        self.assertNotIn('减仓与卖出', text)

    def test_plan_preparation_precedes_report_in_both_schedules(self):
        for stages in (controller.ACTIVE_STAGES, controller.SHADOW_STAGES):
            lookup = {s.name: s for s in stages}
            self.assertEqual(lookup['daily_report_v3'].dependencies, ('daily_value_plan',))
            self.assertEqual(lookup['daily_value_plan'].dependencies, ('morning_quality_gate',))
            self.assertLess(lookup['daily_value_plan'].earliest, lookup['daily_report_v3'].earliest)
            self.assertIn('--prepare-for-report', lookup['daily_value_plan'].commands(dt.date(2026, 10, 16))[0])

    def test_missing_dataset_does_not_erase_analysis_but_disables_orders(self):
        snapshot = self.snapshot()
        snapshot['hard_gate_passed'] = False
        snapshot['hard_gate_blockers'] = ['failed:factor.daily_scores']
        result = advice.build_actions(snapshot, self.ledger())
        self.assertEqual([a['kind'] for a in result['actions']], ['watch'])
        claims = [{'claim_id': f'C{i}', 'section_name': '今日核心判断', 'claim_type': 'interpretation',
                   'claim_text': '公司归母净利润同比增长20%。', 'evidence_ids': ['POOL0001']} for i in range(2)]
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'response.json'
            path.write_text(json.dumps({'claims': claims}))
            args = SimpleNamespace(skip_model=False, provider='openai_compatible', model='test', timeout=10,
                                   base_url='https://provider.invalid', api_key_env='TEST_KEY', reasoning_effort='high')
            with mock.patch.object(daily, 'run_openai_compatible', return_value=path):
                _, status, errors, _, _ = daily.generate_report_claims(snapshot, snapshot, Path(directory), args)
        self.assertFalse(errors)
        self.assertEqual(status, 'published')

    def test_prepared_ledger_is_unpublished_and_does_not_require_prior_report(self):
        conn = mock.Mock()
        def execute(sql, params=None):
            self.assertNotIn('FROM agent.report_runs', sql)
            cursor = mock.Mock()
            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
            return cursor
        conn.execute.side_effect = execute
        with mock.patch.object(plans, 'load_inputs', return_value=([], [], dt.date(2026, 10, 15), {})):
            result = plans.run(conn, dt.date(2026, 10, 16), dt.datetime(2026, 10, 16, 0, 49, tzinfo=dt.timezone.utc),
                               load_config(), apply=True, require_report=False)
        self.assertEqual(result['status'], 'frozen')
        self.assertEqual(result['summary']['report_status'], 'prepared_for_report')
        insert = next(c for c in conn.execute.call_args_list if 'INSERT INTO quant.daily_plan_runs' in c.args[0])
        self.assertIsNone(insert.args[1][6])
        self.assertEqual(insert.args[1][7], 'shadow')

    def manifest(self, directory, **kw):
        path = Path(directory) / '2026-10-16.md'
        path.write_text('daily report')
        data = {'format': 'integrated-daily-advice-v1', 'shadow': False, 'report_date': '2026-10-16',
                'report_run_id': 'report-run', 'plan_run_id': 'plan-run', 'published_plan_ids': [],
                'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **kw}
        path.with_suffix('.delivery.json').write_text(json.dumps(data))
        return path, data

    def test_integrated_delivery_does_not_call_sender(self):
        forbidden = mock.Mock(side_effect=AssertionError('unexpected database or network IO'))
        with tempfile.TemporaryDirectory() as directory:
            path, meta = self.manifest(directory)
            binding = report_delivery_binding(path)
            with mock.patch('mmclaw.value_plan_delivery.load_config', return_value={'publish_enabled': True, 'delivery_mode': 'integrated'}), \
                 mock.patch('mmclaw.value_plan_delivery.publication_approval', return_value=(True, {})):
                result = deliver_after_report('group', '2026-10-16', path, '2026-10-16T09:05:00+08:00',
                                               forbidden, forbidden, forbidden, **binding)
            self.assertEqual(result['status'], 'integrated_no_orders')
            forbidden.assert_not_called()
            path.write_text('a different report')
            self.assertEqual(record_integrated_delivery('g', '2026-10-16', path, '2026-10-16T09:05:00+08:00',
                {}, forbidden, **binding)['status'], 'report_mismatch')

    def test_preview_or_regenerated_report_cannot_record_delivery_for_old_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.manifest(directory, shadow=True)
            with self.assertRaises(ValueError):
                report_delivery_binding(path)
            path, _ = self.manifest(directory)
            old = report_delivery_binding(path)
            self.manifest(directory, report_run_id='replacement-report')
            result = record_integrated_delivery('g', '2026-10-16', path, '2026-10-16T09:05:00+08:00',
                {}, mock.Mock(side_effect=AssertionError('unexpected IO')), **old)
            self.assertEqual(result['status'], 'report_mismatch')

    def test_complete_delivery_records_exact_plan_once_and_expiry_never_publishes_order(self):
        from scripts.analytics.value_plan import digest
        for sent_at, event in [('2026-10-16T09:05:00+08:00', 'plan_published'),
                               ('2026-10-16T17:05:00+08:00', 'delivery_after_expiry')]:
            with self.subTest(sent_at=sent_at), tempfile.TemporaryDirectory() as directory:
                path, _ = self.manifest(directory, published_plan_ids=['immutable-plan'])
                binding = report_delivery_binding(path)
                config = {'mode': 'approved', 'publish_enabled': True, 'delivery_mode': 'integrated'}
                row = {'run_id': 'plan-run', 'report_status': 'published', 'output_path': str(path),
                       'completed_at': dt.datetime(2026, 10, 16, 0, 59, tzinfo=dt.timezone.utc),
                       'report_validation': {'shadow': False}, 'config_digest': digest(config),
                       'advice': {'actionable': True, 'plan_run_id': 'plan-run', 'published_plan_ids': ['immutable-plan']}}
                conn = mock.MagicMock()
                conn.__enter__.return_value = conn
                events = {}
                def execute(sql, params=None):
                    cursor = mock.MagicMock()
                    if 'SELECT p.*' in sql:
                        cursor.fetchone.return_value = row
                    elif 'SELECT payload' in sql:
                        cursor.__iter__.return_value = iter([{'payload': self.plan(entry_expires_on='2026-10-16')}])
                    elif 'SELECT trade_date' in sql:
                        cursor.__iter__.return_value = iter([{'trade_date': '2026-10-16'}, {'trade_date': '2026-10-19'}])
                    elif 'INSERT INTO quant.daily_plan_events' in sql:
                        self.assertIn('ON CONFLICT DO NOTHING', sql)
                        events.setdefault(params[0], params[3].obj)
                    return cursor
                conn.execute.side_effect = execute
                for _ in range(2):
                    result = record_integrated_delivery('g', '2026-10-16', path, sent_at, config,
                                                        mock.Mock(return_value=conn), **binding)
                    self.assertEqual(result['status'], 'integrated_recorded')
                self.assertEqual(len(events), 1)
                saved = next(iter(events.values()))
                self.assertEqual(saved['event'], event)
                self.assertEqual(saved['report_run_id'], 'report-run')
                self.assertEqual(saved['publication_completed_at'], dt.datetime.fromisoformat(sent_at).isoformat())


if __name__ == '__main__':
    unittest.main()
