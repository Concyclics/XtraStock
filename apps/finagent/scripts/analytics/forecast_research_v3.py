"""Causal, broker-balanced forecast features for offline experiments only.

Fiscal years remain explicit. The forward fallback uses next-year EPS only when
the broker has no usable current-year estimate; it is not a fabricated current
year value. Revision pairs never cross fiscal years or split bases.
"""
from __future__ import annotations
import collections
import datetime as dt
import re
import numpy as np


def public_date(report_date, published_at=None, source_identifier=None):
    days=[dt.date.fromisoformat(report_date)]
    if published_at:
        stamp=dt.datetime.fromisoformat(published_at)
        if stamp.tzinfo is None:
            stamp=stamp.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        days.append(stamp.astimezone(dt.timezone(dt.timedelta(hours=8))).date())
    if source_identifier and re.fullmatch(r'AP\d{8}\d+',source_identifier):
        # The identifier is not proof of first publication. A later encoded
        # date is nevertheless a conservative availability bound, not a reason
        # to pretend an old report was a fresh forecast on its indexing date.
        days.append(dt.datetime.strptime(source_identifier[2:10],'%Y%m%d').date())
    return str(max(days)+dt.timedelta(days=1))


def forecast_features(d, source_rows, aliases=None):
    aliases=aliases or {}
    dates=d['dates'];symbols={str(s):j for j,s in enumerate(d['symbols'])}
    day_ord=np.asarray([dt.date.fromisoformat(str(x)).toordinal() for x in dates])
    years=np.asarray([int(str(x)[:4]) for x in dates])
    grouped=collections.defaultdict(list);audit=collections.Counter()
    for r in source_rows:
        audit['input_rows']+=1
        code=aliases.get(r['stock_code'],r['stock_code']);j=symbols.get(code)
        broker=re.sub(r'\s+','',r.get('institution') or '')
        if j is None or r['eps'] is None or not broker:
            continue
        meta=r['metadata']
        if not (meta.get('semantic_contract')=='earnings-forecast-v2' and meta.get('year_verified') is True
                and meta.get('year_basis')=='explicit_dated_report_text' and meta.get('eps_unit')=='CNY/share'):
            raise ValueError('Untrusted fiscal year or EPS unit')
        day=public_date(r['report_date'],r.get('published_at'),r.get('eastmoney_infocode'));t=int(np.searchsorted(dates,day))
        issued=dt.date.fromisoformat(r['report_date']).toordinal()
        fy=int(r['forecast_year']);ref=int(np.searchsorted(dates,r['report_date'],side='right'))-1
        if t>=len(dates) or ref<0 or not int(day[:4])<=fy<=int(day[:4])+2:
            continue
        eps=float(r['eps'])
        if not np.isfinite(eps):raise ValueError('Non-finite source EPS')
        key=(day,j,broker,fy)
        grouped[key].append((dt.date.fromisoformat(day).toordinal(),j,broker,fy,eps,ref,r['document_id'],issued))
    events=[]
    for key,rows in sorted(grouped.items()):
        if len({r[4] for r in rows})!=1:
            first=sorted(rows,key=lambda r:r[6])[0]
            events.append((*first[:4],None,first[5],'',first[7]))
            audit['conflicting_same_day_broker_year']+=1
        else:
            events.append(sorted(rows,key=lambda r:r[6])[0]);audit['same_day_duplicates']+=len(rows)-1
    events.sort(key=lambda r:(r[0],r[1],r[2],r[3],r[6]))
    shape=d['close'].shape
    value=np.full(shape,np.nan);revision=np.full(shape,np.nan)
    brokers=np.zeros(shape,np.int16);revision_brokers=np.zeros(shape,np.int16)
    state={};changes={};pos=0
    for t,now in enumerate(day_ord):
        year=years[t]
        while pos<len(events) and events[pos][0]<=now:
            row=events[pos];known,j,broker,fy,eps,ref,doc,issued=row;key=(j,broker,fy)
            prior=state.get(key)
            if now-issued>180 or (prior and issued<prior[7]):
                audit['stale_or_out_of_order_on_arrival']+=1;pos+=1;continue
            changes.pop(key,None)
            if eps is None:
                state.pop(key,None);pos+=1;continue
            if prior and 1<=issued-prior[7]<=180 and prior[4]>0 and eps>0:
                s0=d['split_factor'][prior[5],j];s1=d['split_factor'][ref,j]
                if np.isfinite(s0+s1) and s0>0 and s0==s1:
                    changes[key]=(known,eps/prior[4]-1,ref,issued);audit['matched_positive_revision_pairs']+=1
                else:audit['split_crossing_pairs_excluded']+=1
            state[key]=row;pos+=1
        # Bound state by publication age, not by any future return availability.
        expired=[key for key,row in state.items() if now-row[7]>180 or key[2]<year]
        for key in expired:state.pop(key,None);changes.pop(key,None)
        by_broker={}
        for (j,broker,fy),row in state.items():
            if fy not in (year,year+1) or row[4]<=0:
                continue
            split_now=d['split_factor'][t,j];split_ref=d['split_factor'][row[5],j]
            price=d['close'][t,j]
            if not (np.isfinite(split_now+split_ref+price) and split_now>0 and split_now==split_ref and price>0):
                continue
            item=by_broker.setdefault((j,broker),{'value':{},'revision':[]})
            item['value'][fy]=row[4]/price
            ch=changes.get((j,broker,fy))
            if ch and now-ch[3]<=120:
                item['revision'].append(ch[1])
        vals=collections.defaultdict(list);revs=collections.defaultdict(list)
        for (j,broker),items in by_broker.items():
            selected=year if year in items['value'] else year+1
            vals[j].append(items['value'][selected])
            if selected!=year:audit['next_year_value_broker_days']+=1
            if items['revision']:revs[j].append(float(np.mean(items['revision'])))
        for j,items in vals.items():value[t,j]=float(np.median(items));brokers[t,j]=len(items)
        for j,items in revs.items():revision[t,j]=float(np.median(items));revision_brokers[t,j]=len(items)
    audit['feature_stock_days']=int((np.isfinite(value)&np.isfinite(revision)).sum())
    return {'value':value,'revision':revision,'value_brokers':brokers,'revision_brokers':revision_brokers},dict(audit)


def select_fraction(scores, minimum_eligible=20):
    """Predeclared top 20%, 10..40 names. No allocation to tiny universes."""
    selected=np.full_like(scores,np.nan)
    counts=np.zeros(len(scores),np.int16)
    for t,row in enumerate(scores):
        ids=np.flatnonzero(np.isfinite(row));n=len(ids)
        if n<minimum_eligible:
            continue
        k=min(40,max(10,int(np.ceil(.2*n))))
        choice=ids[np.argsort(-row[ids],kind='stable')[:k]]
        selected[t,choice]=row[choice];counts[t]=len(choice)
    return selected,counts


def require_training_support(dates, eligible, windows, minimum_eligible=20):
    """Reject empty causal training windows before optimizing any weights.

    A warmup requirement can make a nominal date range entirely uninvestable.
    All-zero candidate returns must not be presented as learned parameters.
    This is a nonempty-data check, not a claim of adequate statistical power.
    """
    dates=np.asarray(dates)
    eligible=np.asarray(eligible,dtype=bool)
    if eligible.ndim!=2 or len(dates)!=len(eligible):
        raise ValueError('Training dates and eligibility must align')
    counts=eligible.sum(axis=1);audit=[]
    for window in windows:
        mask=(dates>=window['start'])&(dates<=window['end'])
        n=int(np.sum(mask&(counts>=minimum_eligible)))
        audit.append({**window,'usable_sessions':n})
    empty=[r for r in audit if r['usable_sessions']==0]
    if empty:
        raise ValueError('No effective training data after price warmup and forecast coverage: '+str(empty))
    return audit
