import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from strategy_inference import paired_block_interval,wilson,familywise_development_test

def result(returns,name='test'):
    return {'dates':[str(i) for i in range(len(returns))],'nav':np.r_[1.,np.cumprod(1+np.asarray(returns))].tolist(),'name':name}

class InferenceTests(unittest.TestCase):
    def test_identical_paths_have_zero_paired_interval(self):
        r=result(np.random.default_rng(1).normal(0,.01,200))
        v=paired_block_interval(r,r,draws=50)
        self.assertEqual(v['confidence_95'],[0.,0.])
    def test_temporal_alignment_is_required(self):
        r=result([0.]*100);b=result([0.]*100);b['dates'][0]='wrong'
        with self.assertRaises(ValueError):paired_block_interval(r,b)
    def test_small_samples_do_not_imply_certain_win_rate(self):
        lo,hi=wilson(8,10);self.assertLess(lo,.5);self.assertGreater(hi,.9)
    def test_max_test_retains_duplicate_trial_dependence(self):
        r=result(np.random.default_rng(7).normal(.0002,.01,200));b=result([0.]*200)
        one=familywise_development_test([r],[b],draws=100)
        duplicate=familywise_development_test([r,r],[b,b],draws=100)
        self.assertEqual(one['familywise_p_value'],duplicate['familywise_p_value'])

if __name__=='__main__':unittest.main()
