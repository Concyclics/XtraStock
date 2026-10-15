"""Bounded input reads preserve one snapshot and fail without partial training."""
import contextlib
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'analytics'))
import run_forecast_learning_cycle as reader

DAY = dt.date(2026, 10, 13)
CUTOFF = dt.datetime(2026, 10, 13, 16, 50, tzinfo=dt.timezone.utc)


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class ReadConnection:
    def __init__(self, fail_batch=None):
        self.calls = []
        self.transactions = 0
        self.in_transaction = False
        self.rolled_back = False
        self.committed = False
        self.fail_batch = fail_batch
        self.batch_count = 0
        self.clock = 0
        self.advance_by = 0

    @contextlib.contextmanager
    def transaction(self):
        self.transactions += 1
        self.in_transaction = True
        try:
            yield
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params, self.in_transaction))
        if sql == reader.CALENDAR_SQL:
            self.clock += self.advance_by
            return Result([{'trade_date': DAY}])
        if sql == reader.PRICE_SQL:
            self.batch_count += 1
            self.clock += self.advance_by
            if self.batch_count == self.fail_batch:
                raise RuntimeError('secret connection and SQL must not escape')
            # Deliberately return reverse order to check exact global merge order.
            return Result([{'stock_code': code, 'dt': day, 'close': code + str(day),
                            'source_version': 'keep', 'quality_status': 'unverified'}
                           for code in reversed(params[0]) for day in (DAY, DAY-dt.timedelta(days=1))])
        return Result([])


class LearningInputReaderTests(unittest.TestCase):
    def test_rotating_pool_settles_old_assets_in_short_serial_batches(self):
        codes=tuple(f'{n:06}' for n in range(20))
        pending=[{'stock_code':f'{n:06}','entry_date':DAY-dt.timedelta(days=n)} for n in range(20,75)]
        calls=[]
        def read(conn,batch,since,as_of,cutoff,**options):
            calls.append((list(batch),since,options['budget_seconds']))
            return ([{'stock_code':c,'dt':DAY} for c in batch],[{'trade_date':DAY}])
        metrics={}
        with patch.object(reader,'read_inputs',side_effect=read):
            prices,calendar=reader.read_cycle_inputs(None,codes,pending,DAY-dt.timedelta(days=1500),DAY,CUTOFF,metrics)
        self.assertEqual([len(c[0]) for c in calls],[20,40,15])
        self.assertEqual([c[1] for c in calls],[DAY-dt.timedelta(days=1500),DAY-dt.timedelta(days=59),DAY-dt.timedelta(days=74)])
        self.assertEqual(len({p['stock_code'] for p in prices}),75)
        self.assertEqual(calendar,[{'trade_date':DAY}])
        self.assertEqual(metrics['status'],'complete')
        self.assertTrue(all(0<c[2]<=120 for c in calls))

    def test_outcome_batch_failure_does_not_return_training_only_success(self):
        pending=[{'stock_code':f'{n:06}','entry_date':DAY} for n in range(41)]
        metrics={}
        with patch.object(reader,'read_inputs',side_effect=[([],[]),RuntimeError('private detail')]):
            with self.assertRaises(reader.LearningInputReadError):
                reader.read_cycle_inputs(None,('600036',),pending,DAY,DAY,CUTOFF,metrics)
        self.assertEqual(metrics['status'],'failed')
        self.assertFalse(metrics['partial_inputs_returned'])

    def test_batches_share_snapshot_and_preserve_exact_rows(self):
        conn = ReadConnection()
        codes = tuple(f'{n:06}' for n in range(12, 0, -1))
        metrics = {}
        prices, calendar = reader.read_inputs(conn, codes, DAY-dt.timedelta(days=1500), DAY, CUTOFF, metrics=metrics)
        batches = [p[0] for sql,p,_ in conn.calls if sql == reader.PRICE_SQL]
        self.assertEqual(list(map(len,batches)), [5,5,2])
        self.assertEqual([code for batch in batches for code in batch], list(codes))
        self.assertEqual(conn.transactions, 1)
        self.assertTrue(conn.committed)
        self.assertTrue(all(in_tx for _,_,in_tx in conn.calls))
        self.assertEqual(conn.calls[0][0], 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.assertEqual([(p['stock_code'],p['dt']) for p in prices],
                         [(code,day) for code in sorted(codes) for day in (DAY-dt.timedelta(days=1),DAY)])
        self.assertTrue(all(p['close'] == p['stock_code']+str(p['dt']) and p['source_version']=='keep'
                            and p['quality_status']=='unverified' for p in prices))
        self.assertEqual(calendar,[{'trade_date':DAY}])
        self.assertEqual(metrics['status'],'complete')
        self.assertEqual(metrics['price_rows'],24)
        self.assertTrue(all(int(p[0])<=25000 for sql,p,_ in conn.calls if 'set_config' in sql))

    def test_duplicate_codes_do_not_duplicate_rows_or_consume_batch_quota(self):
        conn = ReadConnection()
        prices,_ = reader.read_inputs(conn, ('600036',)*9+('600000',), DAY, DAY, CUTOFF)
        self.assertEqual(len(prices),4)
        self.assertEqual([p[0] for sql,p,_ in conn.calls if sql==reader.PRICE_SQL],[['600036','600000']])

    def test_second_batch_failure_rolls_back_without_returning_first_batch(self):
        conn = ReadConnection(fail_batch=2)
        metrics={}
        with self.assertRaises(reader.LearningInputReadError) as caught:
            reader.read_inputs(conn,tuple(f'{n:06}' for n in range(12)),DAY,DAY,CUTOFF,metrics=metrics)
        self.assertEqual(caught.exception.phase,'price_batch_2')
        self.assertIsInstance(caught.exception.__cause__,RuntimeError)
        self.assertNotIn('secret',str(caught.exception))
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.batch_count,2)
        self.assertFalse(metrics['partial_inputs_returned'])
        self.assertEqual(metrics['phases'][-1]['error_type'],'RuntimeError')

    def test_calendar_failure_is_identified_before_any_price_read(self):
        conn=ReadConnection()
        original=conn.execute
        def execute(sql,params=None):
            if sql==reader.CALENDAR_SQL: raise RuntimeError('calendar broke')
            return original(sql,params)
        conn.execute=execute
        with self.assertRaises(reader.LearningInputReadError) as caught:
            reader.read_inputs(conn,('600036',),DAY,DAY,CUTOFF)
        self.assertEqual(caught.exception.phase,'calendar')
        self.assertEqual(conn.batch_count,0)

    def test_total_budget_shrinks_statement_timeout_and_stops_without_partial_result(self):
        conn=ReadConnection();conn.advance_by=4
        with patch.object(reader.time,'monotonic',side_effect=lambda:conn.clock):
            with self.assertRaises(reader.LearningInputReadError) as caught:
                reader.read_inputs(conn,tuple(f'{n:06}' for n in range(12)),DAY,DAY,CUTOFF,budget_seconds=10)
        self.assertEqual(caught.exception.phase,'price_batch_2')
        self.assertEqual(caught.exception.original_error_type,'TimeoutError')
        self.assertEqual([int(p[0]) for sql,p,_ in conn.calls if 'set_config' in sql],[10000,6000,2000])
        self.assertTrue(conn.rolled_back)
        self.assertEqual(conn.batch_count,2)

    def test_rejects_unbounded_universe_or_budget_before_query(self):
        for codes,budget in [((),120),(tuple(f'{n:06}' for n in range(41)),120),(('600036',),121)]:
            conn=ReadConnection()
            with self.assertRaises(ValueError):reader.read_inputs(conn,codes,DAY,DAY,CUTOFF,budget_seconds=budget)
            self.assertEqual(conn.calls,[])


class CycleConnection:
    def __init__(self,fail_record=False,fail_unlock=False):
        self.calls=[];self.fail_record=fail_record;self.fail_unlock=fail_unlock
    def execute(self,sql,params=None):
        self.calls.append((sql,params))
        if 'pg_try_advisory_lock' in sql:return Result([{'ok':True}])
        if 'max(trade_date)' in sql:return Result([{'day':DAY}])
        if 'to_regclass' in sql:return Result([{'t':'analytics.learning_models_v1'}])
        if 'learning_predictions_v1 p' in sql:return Result([])
        if 'INSERT INTO analytics.learning_cycles_v1' in sql:
            if self.fail_record:raise RuntimeError('private insert SQL')
            return Result([{'completed_at':CUTOFF}])
        if 'pg_advisory_unlock' in sql and self.fail_unlock:raise RuntimeError('private unlock SQL')
        return Result([])


class LearningFailurePersistenceTests(unittest.TestCase):
    def test_failure_uses_actual_db_clock_and_contains_no_private_error(self):
        conn=CycleConnection()
        source=RuntimeError('SQL SELECT password FROM secret at private host')
        error=reader.LearningInputReadError('price_batch_2',{'status':'failed'},source)
        summary=reader.record_failed_cycle(conn,CUTOFF-dt.timedelta(minutes=2),DAY,('600036',),
                                           'input_read',error,error.read_metrics)
        sql,params=conn.calls[-1]
        self.assertIn('clock_timestamp()',sql)
        self.assertEqual(params[1],CUTOFF-dt.timedelta(minutes=2))
        self.assertEqual(params[2],DAY)
        self.assertTrue(summary['failure_recorded'])
        self.assertEqual(summary['completed_at'],str(CUTOFF))
        self.assertEqual(summary['failure_phase'],'price_batch_2')
        self.assertEqual(summary['models'],[])
        self.assertNotIn('password',str(summary))
        self.assertNotIn('private host',str(summary))

    def test_unresolved_asof_does_not_invent_past_date(self):
        conn=CycleConnection()
        result=reader.record_failed_cycle(conn,CUTOFF,None,('600036',),'preflight',RuntimeError(),{})
        self.assertFalse(result['failure_recorded'])
        self.assertEqual(result['failure_recording_error_type'],'AsOfUnresolved')
        self.assertEqual(conn.calls,[])

    def test_read_failure_never_trains_or_settles_and_preserves_original_exception(self):
        conn=CycleConnection()
        error=reader.LearningInputReadError('price_batch_2',{},TimeoutError())
        with patch.object(reader,'read_inputs',side_effect=error), \
             patch.object(reader,'build_examples') as examples, \
             patch.object(reader,'settle_prediction') as settle, \
             patch.object(reader,'train_research') as train:
            with self.assertRaises(reader.LearningInputReadError) as caught:reader.run_cycle(conn,DAY)
        self.assertIs(caught.exception,error)
        examples.assert_not_called();settle.assert_not_called();train.assert_not_called()
        self.assertTrue(error.learning_failure['failure_recorded'])
        writes=[sql for sql,_ in conn.calls if sql.lstrip().startswith('INSERT')]
        self.assertEqual(len(writes),1)
        self.assertIn('learning_cycles_v1',writes[0])
        self.assertIn('pg_advisory_unlock',conn.calls[-1][0])

    def test_persistence_or_cleanup_failure_does_not_mask_read_error(self):
        conn=CycleConnection(fail_record=True,fail_unlock=True)
        error=reader.LearningInputReadError('calendar',{},TimeoutError())
        with patch.object(reader,'read_inputs',side_effect=error):
            with self.assertRaises(reader.LearningInputReadError) as caught:reader.run_cycle(conn,DAY)
        self.assertIs(caught.exception,error)
        self.assertFalse(error.learning_failure['failure_recorded'])
        self.assertEqual(error.learning_failure['failure_recording_error_type'],'RuntimeError')
        self.assertEqual(error.learning_failure['unlock_error_type'],'RuntimeError')

    def test_dry_run_failure_does_not_write_failure_metadata(self):
        conn=CycleConnection()
        with patch.object(reader,'read_inputs',side_effect=RuntimeError('read')):
            with self.assertRaises(RuntimeError):reader.run_cycle(conn,DAY,dry_run=True)
        self.assertFalse(any('INSERT' in sql for sql,_ in conn.calls))

    def test_committed_progress_survives_later_horizon_failure(self):
        conn=CycleConnection()
        prices=[{'stock_code':'600036','quality_status':'unverified'}]
        rows=[{'stock_code':'600036','signal_date':'2026-09-01','exit_date':'2026-09-08'}]
        trained={'parameters':{'trained_outcome_through':'2026-09-08','residual_interval_80':[-.1,.1]},
                 'evaluation':{'research_validation_passed':False}}
        error=RuntimeError('private training failure')
        with patch.object(reader,'read_inputs',return_value=(prices,[{'trade_date':DAY}])), \
             patch.object(reader,'build_examples',return_value=(rows,[],{})), \
             patch.object(reader,'train_research',side_effect=[trained,error]), \
             patch.object(reader,'save_dataset',return_value='test-only-dataset'):
            with self.assertRaises(RuntimeError) as caught:reader.run_cycle(conn,DAY,codes=('600036',),horizons=(5,20))
        self.assertIs(caught.exception,error)
        report=error.learning_failure
        self.assertTrue(report['partial_cycle_writes'])
        self.assertEqual(report['committed_progress']['model_revisions_added'],1)
        self.assertEqual(report['models'],[{'horizon':5,'status':'trained'}])
        self.assertEqual(report['failure_phase'],'model_horizon_20_training')
        self.assertEqual(report['input_gap_scope'],'unknown_on_failed_cycle')
        self.assertNotIn('private',str(report))

    def test_successful_dryrun_has_no_writes_and_unlock_warning_preserves_success(self):
        conn=CycleConnection(fail_unlock=True)
        execute=conn.execute
        def with_pending(sql,params=None):
            if 'learning_predictions_v1 p' in sql:
                conn.calls.append((sql,params))
                return Result([{'stock_code':'600036','prediction_id':'test-prediction'}])
            return execute(sql,params)
        conn.execute=with_pending
        prices=[{'stock_code':'600036','quality_status':'unverified'}]
        rows=[{'stock_code':'600036','signal_date':'2026-09-01','exit_date':'2026-09-08'}]
        current=[{'entry_date':'2026-10-14','exit_date':'2026-10-20',
                  'entry_at':'2026-10-14T01:30:00+00:00','stock_code':'600036','name':'Test','x':[0,0]}]
        trained={'parameters':{'trained_outcome_through':'2026-09-08','residual_interval_80':[-.1,.1]},
                 'evaluation':{'research_validation_passed':False}}
        with patch.object(reader,'read_inputs',return_value=(prices,[{'trade_date':DAY}])), \
             patch.object(reader,'build_examples',return_value=(rows,current,{})), \
             patch.object(reader,'settle_prediction',return_value={'source_digest':'test'}), \
             patch.object(reader,'train_research',return_value=trained), \
             patch.object(reader,'predict',return_value=[.01]), \
             patch.object(reader,'can_record_forward',return_value=True), \
             patch.object(reader,'save_dataset') as save:
            report=reader.run_cycle(conn,DAY,codes=('600036',),horizons=(5,),dry_run=True)
        self.assertEqual(report['status'],'run')
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['predictions_added'],1)
        self.assertEqual(report['outcome_revisions_added'],1)
        self.assertEqual(report['committed_progress']['predictions_added'],0)
        self.assertEqual(report['committed_progress']['outcome_revisions_added'],0)
        self.assertEqual(report['committed_progress']['model_revisions_added'],0)
        self.assertEqual(report['cleanup_warnings'][0]['phase'],'session_unlock')
        self.assertFalse(any('INSERT' in sql for sql,_ in conn.calls))
        save.assert_not_called()


if __name__=='__main__':unittest.main()
