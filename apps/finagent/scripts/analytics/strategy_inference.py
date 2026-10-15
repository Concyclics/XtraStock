"""Finite-sample uncertainty summaries, preserving temporal/cross-sectional dependence."""
from __future__ import annotations
import numpy as np


def paired_block_interval(result,baseline,*,block=20,draws=2000,seed=140326):
    if result['dates']!=baseline['dates']:raise ValueError('unaligned paths')
    a=np.asarray(result['nav']);b=np.asarray(baseline['nav'])
    r=a[1:]/a[:-1]-1;s=b[1:]/b[:-1]-1;diff=r-s;n=len(diff)
    if n<2*block:raise ValueError('insufficient independent time blocks')
    rng=np.random.default_rng(seed);parts=(n+block-1)//block
    starts=rng.integers(0,n-block+1,size=(draws,parts))
    idx=(starts[...,None]+np.arange(block)).reshape(draws,-1)[:,:n]
    excess=diff[idx].mean(axis=1)*252
    return {'method':'paired moving-block bootstrap of daily net excess, dates are resampled jointly',
            'block_sessions':block,'draws':draws,'seed':seed,'annualized_mean_excess':float(diff.mean()*252),
            'confidence_95':np.quantile(excess,[.025,.975]).tolist(),
            'bootstrap_fraction_above_zero':float(np.mean(excess>0)),
            'independent_block_equivalents':n/block,
            'qualification':'descriptive interval; not corrected for selecting among development trials'}


def wilson(successes,total):
    if not 0<=successes<=total or total<=0:raise ValueError('invalid binomial counts')
    z=1.959963984540054;p=successes/total;den=1+z*z/total
    center=(p+z*z/(2*total))/den
    radius=z*np.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [float(center-radius),float(center+radius)]


def familywise_development_test(results,baselines,*,block=20,draws=2000,seed=381037):
    """Max statistic over *all* trials under a zero-excess centered block null.

    This guards against reporting the luckiest trial as independent evidence.
    It does not cure survivor bias or guarantee power in changing regimes.
    """
    columns=[];names=[];dates=results[0]['dates']
    for r,b in zip(results,baselines):
        if r['dates']!=dates or b['dates']!=dates:raise ValueError('unaligned trial')
        a=np.asarray(r['nav']);v=np.asarray(b['nav'])
        columns.append(a[1:]/a[:-1]-v[1:]/v[:-1]);names.append(r['name'])
    x=np.column_stack(columns);n=len(x);means=x.mean(axis=0)
    # Circular blocks simplify equal inclusion; identical sampled dates for
    # every policy preserve their very strong cross-policy correlation.
    centered=x-means
    extended=np.vstack([centered,centered[:block-1]])
    prefix=np.vstack([np.zeros((1,x.shape[1])),np.cumsum(extended,axis=0)])
    sums=prefix[np.arange(n)+block]-prefix[np.arange(n)]
    rng=np.random.default_rng(seed);parts=(n+block-1)//block
    maxima=[]
    for _ in range(draws):
        sampled=sums[rng.integers(0,n,size=parts)].sum(axis=0)/(parts*block)
        maxima.append(float(np.max(sampled)))
    observed=float(np.max(means));p=(1+int(np.sum(np.asarray(maxima)>=observed)))/(draws+1)
    return {'method':'centered circular moving-block max-mean test over all retained trials',
       'trials':len(results),'block_sessions':block,'draws':draws,'seed':seed,
       'best_daily_mean_excess':observed,'best_by_daily_mean':names[int(np.argmax(means))],
       'familywise_p_value':p,'includes_failed_quality_gates':True,
       'qualification':'development-only selection diagnostic; not production qualification'}
