"""Causal long-only research simulator on frozen local data, not a broker.

Raw-price cash/share accounting, prior-session signals, next-open fills, capped
participation, explicit missing marks and dividend receivables. Historical ST
flags/order books are unavailable: a conservative universal 4.8% opening-gap
guard is a proxy, not a claim that exact exchange limits were reconstructed.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import numpy as np
import pandas as pd

VERSION = 'balanced-strategy-research-v3'
FAMILIES = ('medium_momentum','long_momentum','short_reversal','low_volatility',
            'trend_pullback','momentum_low_vol','reversal_low_vol','defensive_trend')


@dataclass(frozen=True)
class Policy:
    family: str
    rebalance: int = 20
    size: int = 20
    regime: str = 'none'
    cost_bps: float = 15.0
    initial_cash: float = 10_000_000.0
    participation: float = .01
    dividend_tax: float = .20
    signal_lag: int = 1
    round_lots: bool = True
    target_vol: float | None = None
    weighting: str = 'equal'
    max_weight: float = 1.0
    holding_buffer: float = 1.0
    exposure_floor: float = 0.0
    risk_rebalance: int | None = None

    @property
    def name(self):
        return f'{self.family}-h{self.rebalance}-k{self.size}-{self.regime}' + (f'-v{self.target_vol}' if self.target_vol else '')


def capped_weights(raw, cap):
    """Water-fill positive weights; infeasible concentration limits leave cash."""
    raw=np.asarray(raw,float)
    if not 0<cap<=1 or np.any(~np.isfinite(raw)) or np.any(raw<=0):
        raise ValueError('invalid allocation weights')
    weights=np.zeros_like(raw);active=np.ones(len(raw),bool);budget=1.
    while active.any() and budget>1e-12:
        proposal=raw[active]/raw[active].sum()*budget
        hit=proposal>cap
        indices=np.flatnonzero(active)
        if not hit.any():weights[indices]=proposal;break
        capped=indices[hit];weights[capped]=cap;active[capped]=False
        budget=1.-weights.sum()
    return weights


def buffered_selection(scores, quantities, size, buffer=1.0):
    """Retain incumbents within a fixed rank band, then fill with top candidates."""
    candidates=np.flatnonzero(np.isfinite(scores))
    order=candidates[np.argsort(-scores[candidates],kind='stable')]
    if buffer==1.:return order[:size]
    retain=order[:int(np.ceil(size*buffer))]
    retain=retain[quantities[retain]>1e-7][:size]
    fresh=order[~np.isin(order,retain)][:max(0,size-len(retain))]
    return np.concatenate([retain,fresh])


def load_dataset(path, cohort='discovery', until=None):
    path=Path(path)
    with np.load(path/'market.npz',allow_pickle=False) as z:
        cols=np.ones(len(z['symbols']),dtype=bool) if cohort=='all' else z['cohort']==cohort
        rows=np.ones(len(z['dates']),dtype=bool) if until is None else z['dates']<=until
        d={k:(z[k][rows][:,cols] if z[k].ndim==2 else z[k][rows] if k=='dates' else z[k][cols]) for k in z.files}
    meta=json.loads((path/'assets.json').read_text());meta=[a for a,keep in zip(meta,cols) if keep]
    by_id={a['asset_id']:i for i,a in enumerate(meta)}
    actions=[]
    for a in json.loads((path/'actions.json').read_text()):
        if a['asset_id'] in by_id and (until is None or a['ex_date']<=until):
            actions.append({**a,'column':by_id[a['asset_id']]})
    d.update(assets=meta,actions=actions,cohort_name=cohort)
    return d


def rolling(a, n, operation='mean', minimum=None):
    r=pd.DataFrame(a).rolling(n,min_periods=n if minimum is None else minimum)
    return getattr(r,operation)().to_numpy()


def lag(a,n):
    out=np.full_like(a,np.nan,dtype=float)
    out[n:]=a[:-n]
    return out


def rank(a,eligible):
    return pd.DataFrame(np.where(eligible,a,np.nan)).rank(axis=1,pct=True,method='average').to_numpy()


def liquidity_amount(d):
    """Observed amount or a strict lower bound, never a typical-price estimate.

    Canonical volume is in lots of 100 shares for both sources. Every traded
    share costs at least the daily low, so low * shares cannot overstate daily
    notional when OHLC/volume units are valid. The bound is research-only and
    is not written back as an observed amount into the source database.
    """
    amount=d['amount'];lower=d['low']*d['volume']*100.0
    usable=np.isfinite(lower)&(lower>0)&(d['low']>0)
    observed=np.isfinite(amount)&(amount>0)
    return np.where(observed,amount,np.where(usable,lower,0.)),~observed&usable


def prepare_features(d):
    o,h,l,c,v=(d[k] for k in ('open','high','low','close','volume'))
    valid=(d['quality']>0)&(d['quality']<3)&np.isfinite(o+h+l+c)&(l>0)&(h>=np.maximum(o,c))&(l<=np.minimum(o,c))
    af=d['total_return_factor']
    feature_valid=valid&(d['adjustment_quality']>0)&(d['adjustment_quality']<3)&np.isfinite(af)&(af>0)
    adj=np.where(feature_valid,c*af,np.nan)
    with np.errstate(divide='ignore',invalid='ignore'):
        ret=adj/lag(adj,1)-1
        r5=adj/lag(adj,5)-1
        m60=lag(adj,5)/lag(adj,60)-1
        m120=lag(adj,5)/lag(adj,120)-1
    # Causal quarantine of implausible daily moves. Does not delete the security
    # before the anomalous observation or erase an already-held loss.
    suspicious=np.isfinite(ret)&(np.abs(ret)>.35)
    v20=rolling(np.where(suspicious,np.nan,ret),20,'std',15)
    v60=rolling(np.where(suspicious,np.nan,ret),60,'std',45)
    amount,amount_bound=liquidity_amount(d)
    adv=rolling(np.where(valid&(v>0),amount,np.nan),20,'mean',15)
    history=np.cumsum(feature_valid,axis=0)
    recent=rolling(feature_valid.astype(float),20,'sum',20)
    quarantine=rolling(suspicious.astype(float),20,'sum',1)>0
    eligible=feature_valid&(history>=252)&(recent>=18)&(v>0)&(c>=2)&(adv>=20_000_000)&(v20>0)&np.isfinite(m120)&~quarantine
    ranks={'m60':rank(m60,eligible),'m120':rank(m120,eligible),'rev':rank(-r5,eligible),
           'lv20':rank(-v20,eligible),'lv60':rank(-v60,eligible)}
    score={
        'medium_momentum':ranks['m60'], 'long_momentum':ranks['m120'],
        'short_reversal':ranks['rev'], 'low_volatility':ranks['lv60'],
        'trend_pullback':(ranks['m120']+ranks['rev'])/2,
        'momentum_low_vol':(ranks['m60']+ranks['lv60'])/2,
        'reversal_low_vol':(ranks['rev']+ranks['lv20'])/2,
        'defensive_trend':np.where(m120>0,ranks['lv60'],np.nan),
        'equal_weight':np.where(eligible,1.0,np.nan),
    }
    prior_eligible=np.vstack([np.zeros((1,eligible.shape[1]),bool),eligible[:-1]])
    mask=prior_eligible&np.isfinite(ret)&~suspicious
    count=mask.sum(axis=1)
    market_return=np.divide(np.where(mask,ret,0).sum(axis=1),count,out=np.zeros(len(count)),where=count>0)
    index=np.cumprod(1+market_return)
    ma120=rolling(index[:,None],120,'mean')[:,0]
    market_on=(index>ma120)&(count>=20)
    market_vol=rolling(market_return[:,None],20,'std')[:,0]*np.sqrt(252)
    anomaly_indices=np.argwhere(suspicious)
    return {'valid':valid,'eligible':eligible,'score':score,'vol20':v20,'adv':adv,
            'amount_lower_bound_observations':int(np.sum(valid&amount_bound)),'market_on':market_on,
            'market_vol':market_vol,'returns':ret,'anomalies':[
                {'date':str(d['dates'][i]),'symbol':str(d['symbols'][j]),'adjusted_return':float(ret[i,j])}
                for i,j in anomaly_indices], 'version':VERSION}


def metrics(nav, dates, period=20):
    nav=np.asarray(nav,float)
    if len(nav)!=len(dates)+1 or len(nav)<2 or np.any(nav<=0) or not np.all(np.isfinite(nav)):
        raise ValueError('invalid daily NAV path')
    r=nav[1:]/nav[:-1]-1
    blocks=[float(nav[i+period]/nav[i]-1) for i in range(0,len(r)-period+1,period)]
    b=np.asarray(blocks)
    annual={}
    for year in sorted({str(day)[:4] for day in dates}):
        yr=r[np.asarray([str(day).startswith(year) for day in dates])]
        annual[year]={'return':float(np.prod(1+yr)-1),'sessions':len(yr)}
    std=float(np.std(r,ddof=1)) if len(r)>1 else 0
    days=int((np.datetime64(str(dates[-1]))-np.datetime64(str(dates[0])))/np.timedelta64(1,'D'))+1
    return {'net_return':float(nav[-1]/nav[0]-1),'cagr':float((nav[-1]/nav[0])**(365.25/days)-1),
        'cagr_convention':'ACT/365.25, first session date through last session date inclusive',
        'trading_252_cagr':float((nav[-1]/nav[0])**(252/len(r))-1),
        'maximum_drawdown':float(-np.min(nav/np.maximum.accumulate(nav)-1)),
        'annual_volatility':std*np.sqrt(252),'sharpe_zero_cash':float(np.mean(r)/std*np.sqrt(252)) if std else 0.0,
        'nonoverlap_period_sessions':period,'period_count':len(blocks),'winning_periods':int(np.sum(b>0)),
        'period_win_rate':float(np.mean(b>0)) if len(b) else None,
        'period_profit_factor':float(b[b>0].sum()/(-b[b<0].sum())) if np.any(b<0) else None,
        'annual':annual,'sessions':len(r),'first_date':str(dates[0]),'last_date':str(dates[-1])}


def action_schedule(d):
    dates=d['dates'];by_day={};gaps=[]
    for a in d['actions']:
        t=int(np.searchsorted(dates,a['ex_date']))
        if t>=len(dates) or dates[t]!=a['ex_date']:
            gaps.append({'symbol':str(d['symbols'][a['column']]),'ex_date':a['ex_date'],'reason':'not_in_calendar'});continue
        if a['action_type'] not in ('split','cash_dividend','stock_dividend'):
            gaps.append({'symbol':str(d['symbols'][a['column']]),'ex_date':a['ex_date'],'reason':'unsupported_action'});continue
        extra=float(a.get('split_ratio') or 0)+float(a.get('stock_dividend_ratio') or 0)
        cash=float(a.get('cash_dividend') or 0)
        if extra<0 or extra>3 or cash<0:raise ValueError('unresolved corporate-action terms')
        # Differential records often store market-reference rather than exact
        # per-holder ratios. Preserve that documented approximation unless a
        # verified declared ratio was separately parsed into metadata.
        meta=a.get('metadata') or {}
        if meta.get('parser') in ('cninfo_equity_distribution:v3','cninfo_equity_distribution:v4','cninfo_equity_distribution:v5'):
            extra=float(meta.get('declared_split_ratio') or 0)+float(meta.get('declared_stock_dividend_ratio') or 0)
            cash=float(meta.get('declared_cash_dividend') or cash)
        pay=int(np.searchsorted(dates,a['pay_date'])) if a.get('pay_date') else len(dates)
        by_day.setdefault(t,[]).append((a['column'],extra,cash,max(t,pay),a['quality_status']))
    return by_day,gaps


def backtest(d,f,policy,start,end,*,keep_trades=False):
    if policy.family not in f['score'] or policy.rebalance not in (5,10,20,40,60):raise ValueError('unknown policy')
    if policy.signal_lag<1 or policy.cost_bps<0 or not 0<policy.participation<=.05:raise ValueError('invalid execution policy')
    if policy.regime not in ('none','lagged_market_trend'):raise ValueError('unknown market regime')
    if policy.weighting not in ('equal','inverse_volatility') or not 0<policy.max_weight<=1:
        raise ValueError('invalid allocation policy')
    if policy.holding_buffer<1 or not 0<=policy.exposure_floor<=1 or policy.size<1:
        raise ValueError('invalid selection policy')
    if policy.target_vol is not None and policy.target_vol<=0:raise ValueError('invalid volatility budget')
    if policy.risk_rebalance is not None and policy.risk_rebalance<1:raise ValueError('invalid risk cadence')
    dates=d['dates'];indices=np.flatnonzero((dates>=start)&(dates<=end))
    if not len(indices) or indices[0]<policy.signal_lag:raise ValueError('insufficient warmup/calendar')
    n=len(d['symbols']);q=np.zeros(n);cash=policy.initial_cash;receivables=0.;payables={}
    last=np.nan_to_num(d['close'][indices[0]-1],nan=0.0);stale=np.zeros(n,int)
    cost=np.zeros(n);receipts=np.zeros(n);entry=np.full(n,-1,int)
    fee=policy.cost_bps/10000;nav=[cash];exposure=[];daily_turnover=[];trades=[];fill_count=0;fees=0.
    blocked={'buy':0,'sell':0,'capacity_limited':0,'unverified_action_held':0,
             'ex_post_capacity_breach':0,'ex_post_no_volume_fill':0}
    largest_stale=0;worst_stale_weight=0.;minimum_cash=cash;held_anomalies=[];step_log=[]
    events,action_gaps=action_schedule(d)
    amount,amount_bound=liquidity_amount(d);bound_fills=0
    lots=np.where(np.char.startswith(d['symbols'],'688'),200.,100.) if policy.round_lots else np.full(n,1e-8)
    order=np.empty(0,dtype=int)
    for offset,t in enumerate(indices):
        previous_q=q.copy();reference=last.copy()
        for j,extra,dividend,pay,quality in events.get(int(t),[]):
            reference[j]=(reference[j]-dividend)/(1+extra) if reference[j]>0 else 0.
            last[j]=reference[j]
            if q[j]<=1e-7:continue
            entitlement=q[j]*dividend*(1-policy.dividend_tax)
            q[j]*=1+extra
            if entitlement:
                receivables+=entitlement;receipts[j]+=entitlement
                payables[pay]=payables.get(pay,0.)+entitlement
            if quality!='verified':blocked['unverified_action_held']+=1
        received=payables.pop(int(t),0.);cash+=received;receivables-=received
        valid=f['valid'][t]
        # The opening decision must not observe the same day's close, range,
        # total turnover or retrospective OHLC quality. A printed open and
        # prior-session liquidity are only a research execution approximation;
        # exact auction volume, ST/suspension and limit queues remain required
        # for a production-quality execution reconstruction.
        opening_observed=np.isfinite(d['open'][t])&(d['open'][t]>0)
        tradable=opening_observed&f['valid'][t-1]&(d['volume'][t-1]>0)&(amount[t-1]>0)
        open_price=np.where(opening_observed,d['open'][t],last)
        if np.any((q>1e-7)&(~np.isfinite(open_price)|(open_price<=0))):raise ValueError('held security has no valuation')
        selection_due=offset%policy.rebalance==0
        risk_due=policy.risk_rebalance is not None and offset%policy.risk_rebalance==0
        if selection_due or risk_due:
            s=t-policy.signal_lag
            if selection_due:
                scores=f['score'][policy.family][s]
                order=(np.flatnonzero(np.isfinite(scores)) if policy.family=='equal_weight'
                       else buffered_selection(scores,q,policy.size,policy.holding_buffer))
            allocation=1.0
            if policy.regime=='lagged_market_trend' and not f['market_on'][s]:allocation=policy.exposure_floor
            if policy.target_vol is not None:
                vol=f['market_vol'][s]
                allocation*=min(1.,policy.target_vol/max(float(vol),.05)) if np.isfinite(vol) else 0.
            equity=cash+float(q@np.nan_to_num(open_price))+receivables
            target=np.zeros(n)
            if len(order) and allocation>0:
                raw=np.ones(len(order))
                if policy.weighting=='inverse_volatility':
                    volatility=f['vol20'][s,order]
                    # Missing risk estimates do not silently become tiny risk.
                    usable=np.isfinite(volatility)&(volatility>0)
                    order=order[usable];raw=1/np.maximum(volatility[usable],.005)
                weights=capped_weights(raw,policy.max_weight)
                good=order[np.isfinite(open_price[order])&(open_price[order]>0)]
                good_weights=weights[np.isfinite(open_price[order])&(open_price[order]>0)]
                target[good]=np.floor(equity*allocation*good_weights/open_price[good]/lots[good])*lots[good]
            with np.errstate(divide='ignore',invalid='ignore'):
                gap=open_price/reference-1
            cap=np.minimum(np.nan_to_num(f['adv'][s]),amount[t-1])*policy.participation
            can_sell=tradable&(gap>-.048)&(reference>0)
            can_buy=tradable&(gap<.048)&(reference>0)
            desired_sell=np.maximum(0,q-target)
            blocked['sell']+=int(np.sum((desired_sell>1e-7)&~can_sell))
            sells=np.where(can_sell,np.minimum(desired_sell,np.divide(cap,open_price,out=np.zeros(n),where=open_price>0)),0)
            partial=(sells<q-1e-7)
            sells=np.where(partial,np.floor(sells/lots)*lots,sells)
            sells=np.minimum(sells,q)
            sold=sells*open_price;sold=np.nan_to_num(sold)
            cash+=float(sold.sum()*(1-fee));fees+=float(sold.sum()*fee)
            receipts+=sold*(1-fee);q-=sells;cap-=sold
            closed=(q<=1e-7)&(entry>=0)
            for j in np.flatnonzero(closed):
                trades.append({'symbol':str(d['symbols'][j]),'entry_date':str(dates[entry[j]]),'exit_date':str(dates[t]),
                    'net_pnl':float(receipts[j]-cost[j]),'invested_cash':float(cost[j]),'holding_sessions':int(t-entry[j])})
                q[j]=0.;entry[j]=-1;cost[j]=0.;receipts[j]=0.
            wanted=np.maximum(0,target-q)
            blocked['buy']+=int(np.sum((wanted>1e-7)&~can_buy))
            max_buy=np.divide(np.maximum(cap,0),open_price,out=np.zeros(n),where=open_price>0)
            buys=np.where(can_buy,np.minimum(wanted,max_buy),0.)
            blocked['capacity_limited']+=int(np.sum((wanted>max_buy+1e-7)&can_buy))
            buys=np.floor(buys/lots)*lots
            amounts=np.nan_to_num(buys*open_price)*(1+fee)
            if amounts.sum()>cash:
                buys=np.floor(buys*(max(cash,0)/amounts.sum())/lots)*lots
                amounts=np.nan_to_num(buys*open_price)*(1+fee)
            new=(buys>1e-7)&(entry<0);entry[new]=t
            q+=buys;cost+=amounts;cash-=float(amounts.sum())
            fees+=float(amounts.sum()*fee/(1+fee));fill_count+=int(np.sum(sells>1e-7)+np.sum(buys>1e-7))
            filled=(sells>1e-7)|(buys>1e-7)
            bound_fills+=int(np.sum(filled&amount_bound[t-1]))
            traded_notional=np.nan_to_num((sells+buys)*open_price)
            blocked['ex_post_capacity_breach']+=int(np.sum(filled&(traded_notional>amount[t]*policy.participation+1e-7)))
            blocked['ex_post_no_volume_fill']+=int(np.sum(filled&(~np.isfinite(d['volume'][t])|(d['volume'][t]<=0))))
            turnover=float(sold.sum()+amounts.sum()/(1+fee))/equity
        else:turnover=0.
        minimum_cash=min(minimum_cash,cash)
        if cash<-.001 or np.any(q<-.000001):raise ValueError('cash/position accounting violation')
        last=np.where(valid,d['close'][t],last)
        stale=np.where(valid,0,stale+1)
        held=q>1e-7
        largest_stale=max(largest_stale,int(np.max(stale[held],initial=0)))
        marked=float(q@last);wealth=cash+marked+receivables
        stale_value=float((q*np.where(stale>0,last,0)).sum())
        worst_stale_weight=max(worst_stale_weight,stale_value/wealth)
        if np.any(held&(np.abs(f['returns'][t])>.35)):
            held_anomalies.extend({'date':str(dates[t]),'symbol':str(d['symbols'][j]),'return':float(f['returns'][t,j])}
                for j in np.flatnonzero(held&(np.abs(f['returns'][t])>.35)))
        if not np.isfinite(wealth) or wealth<=0:raise ValueError('invalid marked NAV')
        nav.append(wealth);exposure.append(marked/wealth);daily_turnover.append(turnover)
        if keep_trades and (selection_due or risk_due):
            step_log.append({'date':str(dates[t]),'signal_date':str(dates[t-policy.signal_lag]),'cash':cash,'nav':wealth,
                             'positions':int(held.sum()),'exposure':marked/wealth,'turnover':turnover,
                             'selection_due':selection_due,'target_allocation':allocation,
                             'buy_notional':float(np.nan_to_num(buys*open_price).sum()),
                             'sell_notional':float(sold.sum()),'selected_symbols':d['symbols'][order].tolist()})
    # Mark open positions and charge a liquidation fee provision, without
    # fabricating final fills or classifying open episodes as winning trades.
    liquidation_fee=float(q@last)*fee
    nav[-1]-=liquidation_fee
    m=metrics(nav,dates[indices],20)
    ntrades=len(trades)
    m.update(average_exposure=float(np.mean(exposure)),annual_oneway_turnover=float(np.mean(daily_turnover)*252),
        closed_trade_count=ntrades,closed_trade_win_rate=float(np.mean([x['net_pnl']>0 for x in trades])) if ntrades else None,
        open_position_count=int(np.sum(q>1e-7)),fees_paid=fees,liquidation_fee_provision=liquidation_fee,
        fill_count=fill_count,amount_lower_bound_fill_days=bound_fills,
        minimum_cash=minimum_cash,maximum_held_stale_sessions=largest_stale,
        maximum_stale_weight=worst_stale_weight,terminal_stale_value=float((q*np.where(stale>0,last,0)).sum()),
        terminal_receivables=receivables,held_anomalies=held_anomalies,blocked=blocked,action_calendar_gaps=action_gaps)
    m['terminal_stale_positions']=[{'symbol':str(d['symbols'][j]),'missing_sessions':int(stale[j]),
        'marked_value':float(q[j]*last[j]),'last_observed_date':str(dates[indices[-1]-stale[j]])}
        for j in np.flatnonzero((q>1e-7)&(stale>0))]
    out={'policy':asdict(policy),'name':policy.name,'metrics':m,'dates':dates[indices].tolist(),'nav':nav,
         'exposure':exposure,'turnover':daily_turnover,'version':VERSION,'production_approved':False,
         'execution_assumption':'printed next open; only prior-session sizing/liquidity; gap-limit proxy; no auction/order-book verification'}
    if keep_trades:out.update(trades=trades,rebalance_log=step_log)
    return out


def paired_comparison(result,baseline):
    a=np.asarray(result['nav']);b=np.asarray(baseline['nav'])
    if result['dates']!=baseline['dates']:raise ValueError('benchmark dates mismatch')
    ar=a[1:]/a[:-1]-1;br=b[1:]/b[:-1]-1
    m=result['metrics'];bm=baseline['metrics']
    return {'cagr_excess':m['cagr']-bm['cagr'],
            'period_win_rate_difference':None if m['period_win_rate'] is None else m['period_win_rate']-bm['period_win_rate'],
            'drawdown_difference':m['maximum_drawdown']-bm['maximum_drawdown'],
            'daily_mean_excess':float(np.mean(ar-br)),
            'years_beating_baseline':sum(v['return']>bm['annual'][y]['return'] for y,v in m['annual'].items()),
            'annual_excess':{y:v['return']-bm['annual'][y]['return'] for y,v in m['annual'].items()}}


def balanced_score(result, comparison):
    m=result['metrics'];win=m['period_win_rate'] or 0
    yearly=list(comparison['annual_excess'].values())
    # Fixed, readable utility for development selection. These constants are
    # frozen before any strategy metrics; no reward for hidden/open losses.
    return (comparison['cagr_excess']+.5*(win-.5)-.5*m['maximum_drawdown']
            +.25*float(np.median(yearly))-.25*float(np.std(yearly)))


def research_gate(result,comparison):
    m=result['metrics']
    checks={'positive_net_cagr':m['cagr']>0,'positive_cagr_excess':comparison['cagr_excess']>0,
        'period_win_rate_at_least_55pct':(m['period_win_rate'] or 0)>=.55,
        'win_rate_better_than_baseline':(comparison['period_win_rate_difference'] or 0)>0,
        'drawdown_at_most_25pct':m['maximum_drawdown']<=.25,
        'no_held_price_anomalies':not m['held_anomalies'],
        'no_terminal_stale_marks':m['terminal_stale_value']<.01,
        'no_long_held_data_gap':m['maximum_held_stale_sessions']<=20}
    return {'checks':checks,'passed':all(checks.values()),'production_approved':False}
