from __future__ import annotations
import copy
import datetime as dt
import json
import os
import unittest
from unittest.mock import Mock
from scripts.analytics.research_focus import build_focus,clean_evidence,_public_row,load_focus,VERSION

NOW=dt.datetime(2026,10,13,10,tzinfo=dt.timezone.utc)

def event(**overrides):
    return {'kind':'official_disclosure','stock_code':'000001','stock_name':'示例公司','title':'公司经营公告','source_name':'cninfo',
        'url':'https://example.invalid/announcement','document_id':'document-a','content_version':'version-a',
        'published_at':NOW.isoformat(),'available_at':NOW.isoformat(),'quality_status':'verified','status':'active','source_quality':1.,**overrides}

class ResearchFocusTests(unittest.TestCase):
    def test_research_available_without_approved_forecast(self):
        rows,summary=build_focus([event()],{},[],NOW)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'active')
        self.assertFalse(rows[0]['prediction_eligible']);self.assertTrue(rows[0]['next_checks'])
        self.assertEqual(summary['state_counts']['active'],1)

    def test_candidate_rejected_and_unsupported_graph_never_become_facts(self):
        raw=[event(kind='graph_lead',quality_status='accepted',review_status='classified',attribution_supported=True,status=s) for s in ('candidate','rejected')]
        raw.append(event(kind='graph_lead',quality_status='accepted',review_status='classified',attribution_supported=False))
        accepted,counts=clean_evidence(raw,NOW);self.assertFalse(accepted);self.assertEqual(sum(counts.values()),3)
        good=event(kind='graph_lead',quality_status='accepted',review_status='classified',attribution_supported=True)
        accepted,_=clean_evidence([good],NOW);self.assertIn('线索',accepted[0]['claim_class'])
        self.assertNotIn('已核实',accepted[0]['claim_class'])

    def test_future_publication_or_ingest_is_not_usable(self):
        future=(NOW+dt.timedelta(seconds=1)).isoformat()
        accepted,_=clean_evidence([event(published_at=future),event(available_at=future)],NOW)
        self.assertEqual(accepted,[])

    def test_syndicated_title_or_content_duplicates_count_once(self):
        raw=[event(),event(document_id='duplicate-b',source_name='syndicated'),
             event(document_id='duplicate-c',title='另一个转载标题')]
        accepted,count=clean_evidence(raw,NOW)
        self.assertEqual(len(accepted),1);self.assertEqual(count['duplicate_observations'],2)
        self.assertEqual(accepted[0]['deduplicated_sources'],3)

    def test_revision_has_explicit_reason_and_followup(self):
        before,_=build_focus([event()],{},[],NOW)
        after,_=build_focus([event(content_version='corrected-content')],{},before,NOW+dt.timedelta(hours=1))
        self.assertEqual(after[0]['revised_evidence_count'],1)
        self.assertTrue(any('内容修订' in x for x in after[0]['reasons']))
        self.assertEqual(after[0]['change'],'evidence_updated')

    def test_historical_backfill_does_not_look_like_breaking_news(self):
        old=event(published_at=(NOW-dt.timedelta(days=90)).isoformat())
        rows,_=build_focus([old],{},[],NOW)
        self.assertEqual(rows,[])

    def test_two_separate_days_of_low_signal_before_demotion(self):
        first,_=build_focus([event()],{},[],NOW)
        day15=NOW+dt.timedelta(days=15)
        second,_=build_focus([event()],{},first,day15)
        self.assertEqual(second[0]['state'],'active')
        same_day,_=build_focus([event()],{},second,day15+dt.timedelta(hours=1))
        self.assertEqual(same_day[0]['low_signal_days'],1);self.assertEqual(same_day[0]['state'],'active')
        third,_=build_focus([event()],{},same_day,day15+dt.timedelta(days=1))
        self.assertEqual(third[0]['state'],'cooling')

    def test_stronger_evidence_can_replace_incumbent_after_minimum_hold(self):
        first,_=build_focus([event()],{},[],NOW,max_active=1)
        more=[event(),event(stock_code='000002',document_id='b',content_version='b',title='另一家公司公告'),
              event(stock_code='000002',document_id='c',content_version='c',title='另一家公司新合同')]
        early,_=build_focus(more,{},first,NOW+dt.timedelta(hours=1),max_active=1)
        self.assertEqual(next(p for p in early if p['state']=='active')['focus_key'],'stock:000001')
        later,_=build_focus(more,{},early,NOW+dt.timedelta(days=3),max_active=1)
        self.assertEqual(next(p for p in later if p['state']=='active')['focus_key'],'stock:000002')

    def test_watch_capacity_is_global_and_displaced_history_is_retained(self):
        first,_=build_focus([event(kind='broker_opinion',quality_status='accepted')],{},[],NOW,max_active=0,max_watching=1)
        stronger=event(stock_code='000002',document_id='b',content_version='b')
        second,_=build_focus([event(kind='broker_opinion',quality_status='accepted'),stronger],{},first,NOW,max_active=0,max_watching=1)
        self.assertEqual(sum(p['state']=='watching' for p in second),1)
        self.assertEqual(next(p for p in second if p['focus_key']=='stock:000001')['state'],'cooling')

    def test_retained_reviews_are_not_postponed_by_unrelated_refreshes(self):
        first,_=build_focus([event()],{},[],NOW)
        next_review=first[0]['next_review_at']
        second,_=build_focus([event()],{},first,NOW+dt.timedelta(hours=1))
        self.assertEqual(second[0]['change'],'retained');self.assertEqual(second[0]['next_review_at'],next_review)

    def test_theme_aggregation_does_not_claim_sector_benefit(self):
        rows,_=build_focus([event(themes=[{'id':'concept-a','name':'算力'}])],{},[],NOW)
        theme=next(x for x in rows if x['entity_type']=='theme')
        self.assertTrue(any('尚不能据此认定' in r for r in theme['reasons']))

    def test_invalid_float_quality_rejected(self):
        accepted,_=clean_evidence([event(source_quality=float('nan'))],NOW)
        self.assertEqual(accepted,[])

    def test_public_rows_keep_evidence_links_without_internal_hashes(self):
        rows,_=build_focus([event(content_hash='a'*64)],{},[],NOW)
        public=_public_row({'payload':rows[0],'revision_id':'internal-revision'})
        body={k:v for k,v in public.items() if k!='references'}
        text=json.dumps(body,ensure_ascii=False)
        self.assertNotIn('a'*64,text);self.assertNotIn('version-a',text);self.assertNotIn('document-a',text)
        self.assertEqual(public['evidence'][0]['url'],'https://example.invalid/announcement')
        self.assertEqual(public['references']['document_ids'],['document-a'])

    def test_absent_schema_is_explicitly_unavailable(self):
        conn=Mock();conn.execute.return_value.fetchone.return_value={'present':None}
        self.assertEqual(load_focus(conn,NOW)['status'],'unavailable')

@unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires installed schema; transaction is rolled back')
class ResearchFocusDatabaseTests(unittest.TestCase):
    def test_cutoff_visibility_and_app_history_immutability(self):
        import uuid
        from scripts.analytics.common import connect,jsonb
        import psycopg
        with connect(autocommit=False,attempts=1) as conn:
            conn.execute("SET LOCAL statement_timeout='8s'")
            cutoff=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=1)
            run=conn.execute('''INSERT INTO analytics.research_focus_runs(version,as_of_date,decision_cutoff,input_fingerprint,summary,created_at)
                VALUES(%s,%s,%s,%s,'{}',%s) RETURNING run_id''',(VERSION,cutoff.date(),cutoff,uuid.uuid4().hex,cutoff)).fetchone()['run_id']
            rows,_=build_focus([event(published_at=cutoff.isoformat(),available_at=cutoff.isoformat())],{},[],cutoff)
            p=rows[0]
            conn.execute('''INSERT INTO analytics.research_focus_revisions(run_id,focus_key,entity_type,label,stock_codes,state,priority,next_review_at,payload,created_at)
                VALUES(%s,%s,'stock',%s,%s,'active',%s,%s,%s,%s)''',(run,p['focus_key'],p['label'],p['stock_codes'],p['priority'],p['next_review_at'],jsonb(p),cutoff))
            visible=load_focus(conn,cutoff)
            self.assertEqual(visible['references']['run_id'],str(run));self.assertEqual(len(visible['rows']),1)
            theme={**p,'entity_type':'theme','focus_key':'theme:test','label':'更高分主题','priority':99}
            conn.execute('''INSERT INTO analytics.research_focus_revisions(run_id,focus_key,entity_type,label,stock_codes,state,priority,next_review_at,payload,created_at)
                VALUES(%s,'theme:test','theme','更高分主题',%s,'active',99,%s,%s,%s)''',
                (run,p['stock_codes'],p['next_review_at'],jsonb(theme),cutoff))
            targeted=load_focus(conn,cutoff,stock_codes=['000001'],limit=1)
            self.assertEqual(targeted['rows'][0]['entity_type'],'stock')
            earlier=load_focus(conn,cutoff-dt.timedelta(microseconds=1))
            self.assertNotEqual(earlier.get('references',{}).get('run_id'),str(run))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():conn.execute('UPDATE analytics.research_focus_runs SET version=version WHERE run_id=%s',(run,))
            conn.rollback()

if __name__=='__main__':unittest.main()
