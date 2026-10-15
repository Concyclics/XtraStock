from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from company_evidence_scope import scope_evidence

class CompanyScope(unittest.TestCase):
    def test_adjacent_company_story_cannot_borrow_concept(self):
        text='蓝思科技(300433)与Intel合作，重点关注TGV先进封装。双方讨论玻璃基板。亿纬锂能(300014)认为不存在侵犯专利权。'
        self.assertFalse(scope_evidence(text,'300014','亿纬锂能',['先进封装','玻璃基板'])['supported'])
        self.assertTrue(scope_evidence(text,'300433','蓝思科技',['先进封装'])['supported'])
    def test_negative_product_is_not_positive_exposure(self):
        self.assertFalse(scope_evidence('亿纬锂能推出无液冷钠离子电池系统。','300014','亿纬锂能',['液冷'])['supported'])
    def test_issuer_pronoun_requires_document_owner(self):
        self.assertTrue(scope_evidence('公司已量产液冷产品。','300014','亿纬锂能',['液冷'],'300014')['supported'])
        self.assertFalse(scope_evidence('公司已量产液冷产品。','300014','亿纬锂能',['液冷'])['supported'])
    def test_ascii_boundary_and_direct_relationship(self):
        self.assertFalse(scope_evidence('中际旭创Important业务。','300308','中际旭创',['MPO'])['supported'])
        self.assertTrue(scope_evidence('中际旭创供应光模块。','300308','中际旭创',['光模块'])['supported'])

if __name__=='__main__':unittest.main()
