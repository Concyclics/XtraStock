"""QA must exercise a successful official graph query as well as honest absence."""
import copy
import unittest
from scripts.agent.smoke_finance_v3_tools import (graph_official_evidence_contract,
    graph_original_theme_contract,graph_absence_contract)


def fixture():
    row={'relation_version_id':'version','relation_type':'PRODUCES','source_entity_id':'company',
      'target_entity_id':'product','status':'active','review_status':'approved','source_system':'cninfo_official',
      'claim_class':'observed_fact','source_document_id':'document','evidence_text':'公司公告产品量产，尚未形成规模销售。',
      'valid_from':'2026-09-20','valid_to':None,'public_known_from':'2026-09-20T16:00:00+00:00',
      'public_known_to':None,'evidence_public_at':'2026-09-20T16:00:00+00:00',
      'attributes':{'stock_code':'688825','interpretation_limit':'association only; no sales implication'}}
    evidence={'claim_type':'graph_association','quality_status':'verified','source_document_id':'document',
      'source_id':'cninfo_official','entity_id':'company','metric_id':'PRODUCES',
      'value':{'target_entity_id':'product','evidence_text':row['evidence_text'],'claim_class':'observed_fact','review_status':'approved'}}
    return {'meta':{'status':'ok','quality_status':'warn','row_count':1,'read_only':True,
      'causal_interpretation_forbidden':True,'coverage_pct':None,'data_through':'2026-09-20',
      'decision_cutoff':'2026-10-14T00:20:00+08:00','world_time':'2026-10-14','paths':[],'shared_research':{}},
      'rows':[row],'evidence':[evidence]}


def unavailable():
    p=fixture();p['rows']=[];p['evidence']=[]
    p['meta'].update(status='unavailable',quality_status='fail',row_count=0,data_through=None)
    return p


class GraphSmokeContracts(unittest.TestCase):
    def test_official_positive_requires_actual_supported_evidence(self):
        self.assertTrue(graph_official_evidence_contract(fixture()))
        self.assertFalse(graph_official_evidence_contract(unavailable()))
    def test_no_record_theme_is_an_explicit_separate_boundary(self):
        self.assertTrue(graph_absence_contract(unavailable()))
        self.assertTrue(graph_original_theme_contract(unavailable()))
        self.assertFalse(graph_absence_contract(fixture()))
    def test_empty_error_is_not_accepted_as_absence(self):
        p=unavailable();p['meta']['status']='error';self.assertFalse(graph_original_theme_contract(p))
        p['meta']['status']='ok';self.assertFalse(graph_original_theme_contract(p))
    def test_empty_result_cannot_contain_claims_or_paths(self):
        p=unavailable();p['evidence']=[fixture()['evidence'][0]];self.assertFalse(graph_absence_contract(p))
        p=unavailable();p['meta']['paths']=[{'claim':'invented'}];self.assertFalse(graph_absence_contract(p))
    def test_candidate_unreviewed_and_rejected_rows_fail(self):
        for status,review in [('candidate','unreviewed'),('active','unreviewed'),('rejected','approved')]:
            p=fixture();p['rows'][0].update(status=status,review_status=review)
            self.assertFalse(graph_original_theme_contract(p));self.assertFalse(graph_official_evidence_contract(p))
    def test_failed_attribution_and_superseded_rows_fail(self):
        for attrs in [{'attribution_check':{'supported':False}},{'superseded_by_canonical':True}]:
            p=fixture();p['rows'][0]['attributes'].update(attrs);self.assertFalse(graph_original_theme_contract(p))
    def test_official_label_without_matching_source_document_is_insufficient(self):
        p=fixture();p['evidence'][0]['source_document_id']='unrelated';self.assertFalse(graph_official_evidence_contract(p))
        p=fixture();p['rows'][0]['evidence_text']='';self.assertFalse(graph_official_evidence_contract(p))
    def test_future_or_expired_evidence_is_rejected(self):
        for field,value in [('evidence_public_at','2026-10-15T00:00:00+08:00'),('valid_to','2026-10-14'),
            ('public_known_from','2026-10-15T00:00:00+08:00'),('public_known_to','2026-10-14T00:20:00+08:00')]:
            p=fixture();p['rows'][0][field]=value;self.assertFalse(graph_official_evidence_contract(p))
    def test_original_theme_can_gain_a_genuinely_qualified_relation(self):
        self.assertTrue(graph_original_theme_contract(fixture()))
    def test_complete_coverage_or_causal_claims_are_not_smoke_success(self):
        p=fixture();p['meta']['coverage_pct']=100;self.assertFalse(graph_official_evidence_contract(p))
        p=fixture();p['meta']['causal_interpretation_forbidden']=False;self.assertFalse(graph_official_evidence_contract(p))
    def test_wrong_company_cannot_satisfy_positive_fixture(self):
        p=fixture();p['rows'][0]['attributes']['stock_code']='000333';self.assertFalse(graph_official_evidence_contract(p))
    def test_unofficial_source_cannot_claim_observed_fact(self):
        p=fixture();p['rows'][0]['source_system']='rules:v2';self.assertFalse(graph_original_theme_contract(p))

if __name__=='__main__':unittest.main()
