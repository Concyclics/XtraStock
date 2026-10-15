import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
import finance_pipeline_controller as controller

class ControllerUpgradeTests(unittest.TestCase):
    def test_after_midnight_continues_prior_evening_not_prior_daily_publication(self):
        date=dt.date(2026,10,12);now=dt.datetime(2026,10,13,0,15,tzinfo=controller.TZ)
        stages={s.name:s for s in controller.ACTIVE_STAGES}
        self.assertTrue(controller.stage_due(stages['active_factors'],date,now))
        self.assertTrue(controller.stage_due(stages['active_nightly_verify'],date,now))
        self.assertFalse(controller.stage_due(stages['daily_report_v3'],date,now))
        self.assertFalse(controller.stage_due(stages['active_reports'],date-dt.timedelta(days=2),now))

    def test_weekend_verification_requires_reports_but_not_skipped_market_chain(self):
        date=dt.date(2026,10,11);stage=next(s for s in controller.ACTIVE_STAGES if s.name=='active_nightly_verify')
        self.assertEqual(controller.missing_dependencies(stage,date,set(),controller.ACTIVE_STAGES,set()),['active_reports'])
        self.assertEqual(controller.missing_dependencies(stage,date,{'active_reports'},controller.ACTIVE_STAGES,set()),[])

    def test_only_exhausted_market_failure_can_release_analytics(self):
        date=dt.date(2026,10,12);stage=next(s for s in controller.ACTIVE_STAGES if s.name=='active_analytics')
        self.assertEqual(controller.missing_dependencies(stage,date,{'active_reports'},controller.ACTIVE_STAGES,{'active_market_close'}),[])
        self.assertIn('active_reports',controller.missing_dependencies(stage,date,set(),controller.ACTIVE_STAGES,{'active_market_close','active_reports'}))

    def test_delayed_reports_keep_original_end_date(self):
        stage=next(s for s in controller.ACTIVE_STAGES if s.name=='active_reports')
        command=stage.commands(dt.date(2026,10,12))[0]
        self.assertEqual(command[command.index('--end-date')+1],'2026-10-12')

    def test_file_publishing_has_no_outbox_duplicate(self):
        from unittest.mock import patch
        with patch.dict('os.environ',{'FINANCE_DAILY_DELIVERY':'file','FINANCE_V3_PUBLISH':'1'}):
            self.assertEqual(controller.outbox_command(dt.date(2026,10,13)),[])

    def test_targeted_retry_does_not_relaunch_full_market_and_is_single_owned(self):
        stage=next(s for s in controller.ACTIVE_STAGES if s.name=='active_targeted_retry')
        command=stage.commands(dt.date(2026,10,13))[0]
        self.assertIn('--skip-market',command)
        self.assertNotIn('--include-market',command)
        news=next(s for s in controller.ACTIVE_STAGES if s.name=='active_news')
        self.assertIn(stage.name,news.dependencies)

    def test_attention_runs_independently_and_evening_learning_waits_for_rebuild(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        stages={s.name:s for s in controller.ACTIVE_STAGES}
        self.assertEqual(stages['morning_research_focus'].dependencies,('active_news',))
        command=stages['morning_research_focus'].commands(dt.date(2026,10,13))[0]
        self.assertEqual(command[command.index('--scope')+1],'focus')
        self.assertTrue(controller.stage_due(stages['research_intelligence'],dt.date(2026,10,13),
            dt.datetime(2026,10,14,0,15,tzinfo=controller.TZ)))
        for state,ready in [('active',False),('activating',False),('failed',False),('',False),('inactive',True)]:
            with patch.object(controller.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=state)):
                self.assertEqual(controller.research_rebuild_idle(None)[0],ready)

    def test_after_midnight_learning_uses_closed_session_while_focus_uses_today(self):
        import research_intelligence_refresh as refresh
        commands=refresh.stage_commands(dt.date(2026,10,14),learning_date=dt.date(2026,10,13))
        self.assertEqual([name for name,_ in commands],['learning','focus'])
        stages=dict(commands)
        self.assertEqual(stages['focus'][-1],'2026-10-14')
        self.assertEqual(stages['learning'][-1],'2026-10-13')

    def test_market_rebuild_wait_does_not_consume_daily_stage_attempts(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        stages={s.name:s for s in controller.ACTIVE_STAGES}
        with patch.object(controller.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='active')):
            for name in ('active_market_close','active_analytics','active_graph','active_factors','research_snapshot'):
                with self.subTest(stage=name):
                    self.assertIsNotNone(stages[name].external_ready)
                    ready,reason=stages[name].external_ready(dt.date(2026,10,13))
                    self.assertFalse(ready);self.assertIn('active',reason)
